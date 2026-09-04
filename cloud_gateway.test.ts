import assert from "node:assert/strict";
import test from "node:test";

const values: Record<string, string> = {
  SUPABASE_URL: "https://example.supabase.co",
  SUPABASE_SERVICE_ROLE_KEY: "service-key",
  CAFEBOT_WEBHOOK_SECRET: "webhook-secret",
  CAFEBOT_WORKER_SECRET: "worker-secret",
  CAFEBOT_TELEGRAM_TOKEN: "telegram-secret",
};
const calls: string[] = [];
const background: Promise<unknown>[] = [];

// Set runtime globals before the dynamic import; no Deno.serve means importing is side-effect free.
(globalThis as any).Deno = { env: { get: (name: string) => values[name] }, serve: undefined };
(globalThis as any).EdgeRuntime = { waitUntil: (promise: Promise<unknown>) => background.push(promise) };
(globalThis as any).fetch = async (input: string | URL, init?: RequestInit) => {
  const target = String(input);
  calls.push(`${init?.method ?? "GET"} ${target}`);
  if (target.includes("/rest/v1/cafe_bot_jobs")) return new Response("", { status: 201 });
  if (target.includes("/rest/v1/rpc/")) return new Response("[]", { status: 200 });
  if (target.includes("/rest/v1/cafe_bot_config")) return new Response("[]", { status: 200 });
  return new Response("unexpected network", { status: 500 });
};

const gateway = await import("./supabase/functions/cafe-bot/index.ts");
const request = (path: string, init: RequestInit = {}) => new Request(`https://example.supabase.co/functions/v1/cafe-bot${path}`, init);
const auth = { "X-Telegram-Bot-Api-Secret-Token": values.CAFEBOT_WEBHOOK_SECRET };
const workerAuth = { "X-Worker-Secret": values.CAFEBOT_WORKER_SECRET };

test("unauthenticated webhook, tick, and install are rejected without network", async () => {
  calls.length = 0;
  for (const path of ["/webhook", "/tick", "/install"]) {
    const response = await gateway.handler(request(path, { method: "POST", body: "{}" }));
    assert.equal(response.status, 401);
  }
  assert.equal(calls.length, 0);
});

test("webhook requires POST", async () => {
  const response = await gateway.handler(request("/webhook", { method: "GET", headers: auth }));
  assert.equal(response.status, 405);
});

test("invalid updates and unknown commands are ignored", async () => {
  calls.length = 0;
  const badId = await gateway.handler(request("/webhook", { method: "POST", headers: auth, body: JSON.stringify({ update_id: "1", message: { chat: { id: 3 }, text: "/task x" } }) }));
  const badChat = await gateway.handler(request("/webhook", { method: "POST", headers: auth, body: JSON.stringify({ update_id: 2, message: { chat: { id: "3" }, text: "/task x" } }) }));
  const unknown = await gateway.handler(request("/webhook", { method: "POST", headers: auth, body: JSON.stringify({ update_id: 3, message: { chat: { id: 3 }, text: "/help" } }) }));
  assert.deepEqual(await badId.json(), { ok: true, accepted: false });
  assert.deepEqual(await badChat.json(), { ok: true, accepted: false });
  assert.deepEqual(await unknown.json(), { ok: true, accepted: false });
  await Promise.all(background.splice(0));
  assert.equal(calls.filter((call) => call.includes("cafe_bot_jobs")).length, 0);
});

test("valid task is durably queued before webhook response and worker is awaited by test", async () => {
  calls.length = 0;
  const update = { update_id: 77, message: { chat: { id: 123 }, from: { first_name: "Ayo" }, text: "/task Order milk" } };
  const response = await gateway.handler(request("/webhook", { method: "POST", headers: auth, body: JSON.stringify(update) }));
  assert.deepEqual(await response.json(), { ok: true, accepted: true });
  const jobCall = calls.findIndex((call) => call.includes("cafe_bot_jobs"));
  assert.ok(jobCall >= 0);
  await Promise.all(background.splice(0));
  assert.ok(calls.some((call) => call.includes("rpc/cafe_bot_claim_jobs")));
});

test("command suffix and UK schedule boundaries are deterministic", () => {
  assert.deepEqual(gateway.command("/wins@londoncafeopsbot coffee"), { name: "wins", argument: "coffee" });
  assert.equal(gateway.command("/wins@otherbot coffee"), null);
  assert.equal(gateway.schedule(new Date("2026-09-05T00:04:00+01:00")), null);
  assert.equal(gateway.schedule(new Date("2026-09-05T00:05:00+01:00")), "2026-09-04");
  assert.equal(gateway.schedule(new Date("2026-01-05T00:05:00Z")), "2026-01-04");
});
