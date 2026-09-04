// Deterministic Telegram webhook/worker. Secrets belong in Supabase Function secrets.
import { revenue as fetchRevenue } from "../../../cloud_revenue.ts";
const env = (key: string) => Deno.env.get(key)?.trim() ?? "";
const url = () => env("SUPABASE_URL").replace(/\/$/, "");
const key = () => env("SUPABASE_SERVICE_ROLE_KEY");
const headers = () => ({ apikey: key(), Authorization: `Bearer ${key()}`, "Content-Type": "application/json" });

async function db(path: string, init: RequestInit = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`${url()}/rest/v1/${path}`, {
      ...init, signal: controller.signal, headers: { ...headers(), ...(init.headers ?? {}) },
    });
    if (!response.ok) throw new Error(`database HTTP ${response.status}`);
    const body = await response.text();
    return body ? JSON.parse(body) : null;
  } finally { clearTimeout(timer); }
}

async function rpc(name: string, body: unknown) {
  return db(`rpc/${name}`, { method: "POST", body: JSON.stringify(body) });
}

function json(value: unknown) { return JSON.stringify(value); }
function response(body: unknown, status = 200) {
  return new Response(json(body), { status, headers: { "content-type": "application/json" } });
}
function text(body: string, status = 200) {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}
function secret(request: Request, header: string, expected: string) {
  return Boolean(expected) && request.headers.get(header) === expected;
}

export function command(raw: unknown) {
  const match = String(raw ?? "").trim().match(/^\/(revenue|task|wins)(?:@([A-Za-z0-9_]+))?(?:\s+([\s\S]*))?$/i);
  if (!match || (match[2] && match[2].toLowerCase() !== "londoncafeopsbot")) return null;
  return { name: match[1].toLowerCase(), argument: (match[3] ?? "").trim() };
}

function displayName(user: Record<string, unknown>) {
  return String([user.first_name, user.last_name].filter(Boolean).join(" ") || user.username || "Team member").slice(0, 200);
}

async function config(name: string) {
  const rows = await db(`cafe_bot_config?select=value&key=eq.${encodeURIComponent(name)}&limit=1`);
  return rows?.[0]?.value ?? null;
}

export async function revenue(day?: string): Promise<string> {
  const target = day ?? new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London" }).format(new Date());
  const rows = await db(`cafe_bot_reports?day=eq.${encodeURIComponent(target)}&select=body,source,updated_at&limit=1`);
  if (rows?.[0] && Date.now() - Date.parse(rows[0].updated_at) < 60000) return rows[0].body;
  const report = await fetchRevenue(target, rows?.[0]?.source?.square?.history);
  await db("cafe_bot_reports", { method: "POST", headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
    body: json({ day: target, body: report.body, source: report.source, updated_at: new Date().toISOString() }) });
  return report.body;
}

async function enqueue(update: Record<string, unknown>) {
  const message = (update.message ?? {}) as Record<string, unknown>;
  const parsed = command(message.text);
  const chat = (message.chat ?? {}) as Record<string, unknown>;
  if (!parsed || typeof update.update_id !== "number" || !Number.isSafeInteger(update.update_id) || !Number.isSafeInteger(chat.id)) return false;
  const payload = { ...message, update_id: update.update_id };
  await db("cafe_bot_jobs", {
    method: "POST",
    headers: { Prefer: "resolution=ignore-duplicates,return=minimal" },
    body: json({ id: String(update.update_id), kind: parsed.name, payload }),
  });
  return true;
}

async function telegram(method: string, payload: Record<string, unknown>) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const result = await fetch(`https://api.telegram.org/bot${env("CAFEBOT_TELEGRAM_TOKEN")}/${method}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: json(payload), signal: controller.signal,
    });
    const body = await result.json();
    if (!result.ok || !body.ok) {
      const error = new Error(`telegram HTTP ${result.status}`) as Error & { retryAfter?: number };
      if (result.status === 429) error.retryAfter = Number(body.parameters?.retry_after) || 30;
      throw error;
    }
    return body;
  } finally { clearTimeout(timer); }
}

async function resolveDestination() {
  if (await config("report_chat_id")) return;
  const checked = await config("destination_checked_at");
  if (checked && Date.now() - Date.parse(checked) < 300000) return;
  await db("cafe_bot_config", { method: "POST", headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
    body: json({ key: "destination_checked_at", value: new Date().toISOString() }) });
  // Only the two ID representations in Ayo's supplied links are eligible.
  for (const candidate of ["-2394851554", "-1002394851554"]) {
    try {
      const chat = (await telegram("getChat", { chat_id: candidate })).result;
      const member = (await telegram("getChatMember", { chat_id: candidate, user_id: 8882338438 })).result;
      const canPost = member.status === "administrator" || member.status === "creator" ||
        (member.status === "member" && chat.permissions?.can_send_messages !== false);
      if (!canPost || chat.type === "channel" && !member.can_post_messages || chat.is_forum) continue;
      await db("cafe_bot_config", { method: "POST", headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
        body: json({ key: "report_chat_id", value: String(chat.id) }) });
      await db("cafe_bot_outbox?status=eq.pending", { method: "PATCH", body: json({ next_at: new Date().toISOString() }) });
      return;
    } catch { /* No access yet: keep broadcasts queued, never guess another chat. */ }
  }
}

function retryDelay(attempts: number, retryAfter?: number) {
  return retryAfter ? Math.max(1, retryAfter) : Math.min(300, 2 ** Math.min(attempts + 1, 8));
}

async function finish(id: string, messages: Array<{ id: string; payload: Record<string, unknown> }>) {
  await rpc("cafe_bot_finish", { p_id: id, p_messages: messages });
}

async function processJob(job: Record<string, unknown>) {
  const payload = job.payload as Record<string, unknown>;
  if (job.kind === "refresh") {
    if (Date.now() - Date.parse(String(payload.created_at)) < 120000) await revenue();
    await finish(String(job.id), []);
    return;
  }
  if (job.kind === "daily") {
    const body = await revenue(String(payload.day));
    await finish(String(job.id), [{ id: `${job.id}:report`, payload: { chat_id: null, broadcast: true, text: body } }]);
    return;
  }
  const parsed = command(payload.text);
  if (!parsed) { await finish(String(job.id), []); return; }
  const chatId = String((payload.chat as Record<string, unknown>).id);
  const messages: Array<{ id: string; payload: Record<string, unknown> }> = [];
  let reply: string;
  if (parsed.name === "revenue") reply = await revenue();
  else if (!parsed.argument) reply = `Usage: /${parsed.name} <description>`;
  else if (parsed.argument.length > 3000) reply = "Please keep the description to 3,000 characters.";
  else {
    const label = parsed.name === "task" ? "Task" : "Win";
    reply = `✅ ${label} recorded: ${parsed.argument}`;
    if (parsed.name === "wins") {
      const user = (payload.from ?? {}) as Record<string, unknown>;
      messages.push({ id: `${job.id}:win`, payload: {
        chat_id: null, broadcast: true,
        text: `🏆 Corgi Cafe — team win\n${displayName(user)}\n${parsed.argument}`,
      }});
      reply += "\nQueued for the reporting chat.";
    }
  }
  messages.push({ id: `${job.id}:reply`, payload: { chat_id: chatId, text: reply } });
  await finish(String(job.id), messages);
}

async function drainOutbox(deadline: number) {
  while (Date.now() < deadline) {
    const outbox = await rpc("cafe_bot_claim_outbox", { p_limit: 3 }) as Record<string, unknown>[];
    if (!outbox?.length) break;
    for (const row of outbox) {
      const payload = row.payload as Record<string, unknown>;
      const chatId = payload.chat_id ?? (payload.broadcast ? await config("report_chat_id") : null);
      if (!chatId) {
        await db(`cafe_bot_outbox?id=eq.${encodeURIComponent(String(row.id))}`, { method: "PATCH", body: json({ lease_until: null, next_at: new Date(Date.now() + 300000).toISOString() }) });
        continue;
      }
      try {
        const send = { ...payload, chat_id: chatId };
        delete send.broadcast;
        const thread = await config("report_thread_id");
        if (payload.broadcast && thread) send.message_thread_id = Number(thread);
        const receipt = await telegram("sendMessage", send);
        await db(`cafe_bot_outbox?id=eq.${encodeURIComponent(String(row.id))}`, { method: "PATCH", body: json({ status: "sent", lease_until: null, sent_at: new Date().toISOString(), telegram_message_id: receipt.result.message_id }) });
      } catch (error) {
        const retry = error as Error & { retryAfter?: number };
        await db(`cafe_bot_outbox?id=eq.${encodeURIComponent(String(row.id))}`, { method: "PATCH", body: json({ lease_until: null, next_at: new Date(Date.now() + retryDelay(Number(row.attempts) || 0, retry.retryAfter) * 1000).toISOString(), last_error: "delivery failed", attempts: (Number(row.attempts) || 0) + 1 }) });
      }
    }
  }
}

async function worker() {
  const deadline = Date.now() + 95000;
  await drainOutbox(deadline);
  while (Date.now() < deadline) {
    const jobs = await rpc("cafe_bot_claim_jobs", { p_limit: 3 }) as Record<string, unknown>[];
    if (!jobs?.length) break;
    await Promise.all(jobs.map(async (job) => {
      try { await processJob(job); await drainOutbox(deadline); }
      catch (error) {
        const retry = error as Error & { retryAfter?: number };
        await db(`cafe_bot_jobs?id=eq.${encodeURIComponent(String(job.id))}`, {
          method: "PATCH", body: json({ lease_until: null, next_at: new Date(Date.now() + retryDelay(Number(job.attempts) || 0, retry.retryAfter) * 1000).toISOString(), last_error: "processing failed", attempts: (Number(job.attempts) || 0) + 1 }),
        });
      }
    }));
    await drainOutbox(deadline);
  }
  await drainOutbox(deadline);
}

export function yesterdayUk(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(now);
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  if (Number(values.hour) * 60 + Number(values.minute) < 5) return null;
  const day = new Date(Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day) - 1));
  return day.toISOString().slice(0, 10);
}
export const schedule = yesterdayUk;

async function tick() {
  await db("cafe_bot_config", { method: "POST", headers: { Prefer: "resolution=merge-duplicates,return=minimal" }, body: json({ key: "last_worker_at", value: new Date().toISOString() }) });
  const day = yesterdayUk();
  await resolveDestination();
  const firstDay = await config("broadcast_start_day");
  if (day && (!firstDay || day >= firstDay) && await config("report_chat_id")) await db("cafe_bot_jobs", { method: "POST", headers: { Prefer: "resolution=ignore-duplicates,return=minimal" }, body: json({ id: `daily:${day}`, kind: "daily", payload: { day } }) });
  const now = new Date().toISOString();
  await db("cafe_bot_jobs", { method: "POST", headers: { Prefer: "resolution=ignore-duplicates,return=minimal" },
    body: json({ id: `refresh:${now.slice(0, 16)}`, kind: "refresh", payload: { created_at: now } }) });
  await worker();
}

export async function handler(request: Request) {
  try {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path.endsWith("/health")) {
      const lastWorker = await config("last_worker_at");
      const schedulerHealthy = Boolean(lastWorker) && Date.now() - Date.parse(lastWorker) < 180000;
      const failed = await db("cafe_bot_jobs?select=id&status=eq.pending&attempts=gte.3&limit=1");
      const failedSends = await db("cafe_bot_outbox?select=id&status=eq.pending&attempts=gte.3&limit=1");
      const ok = schedulerHealthy && !failed.length && !failedSends.length;
      return response({ ok, service: "corgi-cafe-bot", version: "webhook-v3",
        scheduler_healthy: schedulerHealthy, delivery_healthy: !failed.length && !failedSends.length }, ok ? 200 : 503);
    }
    if (request.method === "POST" && path.endsWith("/install")) {
      if (!secret(request, "X-Worker-Secret", env("CAFEBOT_WORKER_SECRET"))) return text("unauthorized", 401);
      const me = await telegram("getMe", {});
      if (me.result.username !== "londoncafeopsbot") return text("bot identity mismatch", 409);
      await telegram("setMyCommands", { commands: [
        { command: "revenue", description: "Daily and trailing 30-day revenue" },
        { command: "task", description: "Record a task" },
        { command: "wins", description: "Share a team win" }] });
      await telegram("setWebhook", { url: `${url()}/functions/v1/cafe-bot/webhook`,
        secret_token: env("CAFEBOT_WEBHOOK_SECRET"), max_connections: 1, allowed_updates: ["message", "my_chat_member"], drop_pending_updates: false });
      return response({ ok: true, username: me.result.username });
    }
    if (path.endsWith("/webhook")) {
      if (request.method !== "POST") return text("method not allowed", 405);
      if (!secret(request, "X-Telegram-Bot-Api-Secret-Token", env("CAFEBOT_WEBHOOK_SECRET"))) return text("unauthorized", 401);
      const raw = await request.arrayBuffer();
      if (raw.byteLength > 65536) return text("payload too large", 413);
      const accepted = await enqueue(JSON.parse(new TextDecoder().decode(raw)));
      const task = worker();
      (globalThis as { EdgeRuntime?: { waitUntil(promise: Promise<unknown>): void } }).EdgeRuntime?.waitUntil(task);
      return response({ ok: true, accepted });
    }
    if (path.endsWith("/tick")) {
      if (request.method !== "POST") return text("method not allowed", 405);
      if (!secret(request, "X-Worker-Secret", env("CAFEBOT_WORKER_SECRET"))) return text("unauthorized", 401);
      const task = tick();
      (globalThis as { EdgeRuntime?: { waitUntil(promise: Promise<unknown>): void } }).EdgeRuntime?.waitUntil(task);
      if (!(globalThis as { EdgeRuntime?: unknown }).EdgeRuntime) await task;
      return response({ ok: true });
    }
    return text("not found", 404);
  } catch (_error) { return response({ ok: false, error: "temporary failure" }, 503); }
}

if (typeof Deno !== "undefined" && Deno.serve) Deno.serve(handler);
