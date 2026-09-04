import assert from "node:assert/strict";
import test from "node:test";
import { revenue } from "./cloud_revenue.ts";

test("rejects malformed report dates", async () => {
  await assert.rejects(() => revenue("2026-02-30"), /day must be YYYY-MM-DD/);
});

test("aggregates completed GBP payments, pagination, ledger and ECB FX", async () => {
  process.env.CAFEBOT_SQUARE_TOKEN = "test-token";
  process.env.CAFEBOT_SQUARE_LOCATION = "loc";
  process.env.CAFEBOT_LEDGER_JSON = JSON.stringify({
    "2026-03-29": { deliveroo: { gross_minor: 100 }, uber_eats: { gross_minor: 50 } },
  });
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London" }).format(new Date());
  const calls: string[] = [];
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("squareup")) {
      return new Response(JSON.stringify(calls.filter((item) => item.includes("squareup")).length === 1
        ? { payments: [{ id: "a", status: "COMPLETED", created_at: "2026-03-29T10:00:00Z", total_money: { amount: 1000, currency: "GBP" } }, { id: "a", status: "COMPLETED", created_at: "2026-03-29T10:00:00Z", total_money: { amount: 1000, currency: "GBP" } }], cursor: "next" }
        : { payments: [{ id: "b", status: "COMPLETED", created_at: "2026-03-28T10:00:00Z", total_money: { amount: 2000, currency: "GBP" } }] }), { status: 200 });
    }
    return new Response(`<Cube time='${today}'><Cube currency='GBP' rate='0.8'/><Cube currency='USD' rate='1.0'/></Cube>`, { status: 200 });
  };
  try {
    const result = await revenue("2026-03-29");
    assert.match(result.body, /^📊 Corgi Cafe — revenue\n2026-03-29\n\nDaily revenue: \$14\.38\nTrailing 30 days: \$[\d,.]+$/);
    assert.equal((result.source as any).coverage, "partial");
    assert.equal((result.source as any).square.dailyMinor, 1000);
    assert.equal((result.source as any).square.trailingMinor, 3000);
    assert.equal((result.source as any).square.completedCount, 2);
    assert.equal((result.source as any).fx.rate, 1.25);
    assert.equal((result.source as any).square.dailyAmountsMinor["2026-03-29"], 1000);
    assert.match(calls.find((url) => url.includes("squareup"))!, /begin_time=2026-02-28T00%3A00%3A00Z/);
    assert.match(calls.find((url) => url.includes("squareup"))!, /end_time=2026-03-29T23%3A00%3A00Z/);
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("rejects a stale ECB reference", async () => {
  process.env.CAFEBOT_SQUARE_TOKEN = "test-token";
  process.env.CAFEBOT_SQUARE_LOCATION = "loc";
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (input) => String(input).includes("squareup")
    ? new Response(JSON.stringify({ payments: [] }), { status: 200 })
    : new Response("<Cube time='2020-01-01'><Cube currency='GBP' rate='0.8'/><Cube currency='USD' rate='1.0'/></Cube>", { status: 200 });
  try { await assert.rejects(() => revenue("2026-03-29"), /stale/); }
  finally { globalThis.fetch = oldFetch; }
});

test("rejects malformed completed payments", async () => {
  process.env.CAFEBOT_SQUARE_TOKEN = "test-token";
  process.env.CAFEBOT_SQUARE_LOCATION = "loc";
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (input) => String(input).includes("squareup")
    ? new Response(JSON.stringify({ payments: [{ status: "COMPLETED", id: "bad", created_at: "not-a-date", total_money: { amount: 1, currency: "GBP" } }] }), { status: 200 })
    : new Response("<Cube time='2026-09-04'><Cube currency='GBP' rate='0.8'/><Cube currency='USD' rate='1.0'/></Cube>", { status: 200 });
  try { await assert.rejects(() => revenue("2026-03-29"), /invalid created_at/); }
  finally { globalThis.fetch = oldFetch; }
});

test("reuses fresh history, fetches today only, and falls back when expired", async () => {
  process.env.CAFEBOT_SQUARE_TOKEN = "test-token";
  process.env.CAFEBOT_SQUARE_LOCATION = "loc";
  process.env.CAFEBOT_LEDGER_JSON = "{}";
  const target = "2026-03-29";
  const prior: Record<string, { minor: number; count: number }> = {};
  for (let i = 29; i >= 1; i--) prior[new Date(Date.UTC(2026, 2, 29 - i)).toISOString().slice(0, 10)] = { minor: 100, count: 1 };
  const oldFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (input) => {
    const url = String(input); calls.push(url);
    if (url.includes("squareup")) return new Response(JSON.stringify({ payments: [{ id: "today", status: "COMPLETED", created_at: "2026-03-29T10:00:00Z", total_money: { amount: 500, currency: "GBP" } }] }), { status: 200 });
    return new Response("<Cube time='2026-09-04'><Cube currency='GBP' rate='0.8'/><Cube currency='USD' rate='1.0'/></Cube>", { status: 200 });
  };
  try {
    const fresh = await revenue(target, { days: prior, fetchedAt: new Date().toISOString() });
    assert.match(calls[0], /begin_time=2026-03-29T00%3A00%3A00Z/);
    assert.equal((fresh.source as any).square.dailyMinor, 500);
    assert.equal((fresh.source as any).square.completedCount, 30);
    calls.length = 0;
    const repeated = await revenue(target, (fresh.source as any).square.history);
    assert.match(calls.find((url) => url.includes("squareup"))!, /begin_time=2026-03-29T00%3A00%3A00Z/);
    assert.equal((repeated.source as any).square.completedCount, 30);
    assert.equal((repeated.source as any).square.trailingMinor, 3400);
    calls.length = 0;
    await revenue(target, { days: prior, fetchedAt: "2020-01-01T00:00:00Z" });
    assert.match(calls.find((url) => url.includes("squareup"))!, /begin_time=2026-02-28T00%3A00%3A00Z/);
  } finally { globalThis.fetch = oldFetch; }
});
