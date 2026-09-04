const UK = "Europe/London";
const SQUARE_URL = "https://connect.squareup.com/v2/payments";
const ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml";

type DayTotals = { minor: number; count: number };
type Rational = { num: bigint; den: bigint };

function env(name: string): string {
  const deno = (globalThis as { Deno?: { env?: { get: (key: string) => string | undefined } } }).Deno;
  return deno?.env?.get(name) ?? ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.[name] ?? "");
}

function fail(message: string): never { throw new Error(`Revenue unavailable: ${message}`); }

function validDay(day: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) fail("day must be YYYY-MM-DD");
  const [year, month, date] = day.split("-").map(Number);
  const check = new Date(Date.UTC(year, month - 1, date));
  if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== date) fail("day must be YYYY-MM-DD");
  return day;
}

function localMidnight(day: string): Date {
  const [year, month, date] = day.split("-").map(Number);
  let utc = Date.UTC(year, month - 1, date);
  const formatter = new Intl.DateTimeFormat("en-CA", { timeZone: UK, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  for (let i = 0; i < 3; i++) {
    const parts = Object.fromEntries(formatter.formatToParts(new Date(utc)).map((part) => [part.type, part.value]));
    const represented = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute), Number(parts.second));
    const wanted = Date.UTC(year, month - 1, date);
    utc += wanted - represented;
  }
  return new Date(utc);
}

function iso(date: Date): string { return date.toISOString().replace(".000Z", "Z"); }
function addDays(day: string, amount: number): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, date + amount)).toISOString().slice(0, 10);
}
function safeAdd(a: number, b: number, label: string): number {
  const value = a + b;
  if (!Number.isSafeInteger(value)) fail(`${label} exceeds safe integer range`);
  return value;
}
function reusableHistory(history: { days: Record<string, { minor: number; count: number }>; fetchedAt: string } | undefined, priorDates: string[], target: string, now: Date): boolean {
  if (!history || !history.days || typeof history.days !== "object" || Array.isArray(history.days) || typeof history.fetchedAt !== "string") return false;
  const keys = Object.keys(history.days).filter((date) => date !== target).sort();
  if (keys.length !== priorDates.length || keys.join("\n") !== [...priorDates].sort().join("\n")) return false;
  const fetched = Date.parse(history.fetchedAt);
  if (!Number.isFinite(fetched) || fetched > now.getTime() || now.getTime() - fetched > 900_000) return false;
  return priorDates.every((date) => {
    const value = history.days[date];
    return value && Number.isSafeInteger(value.minor) && value.minor >= 0 && Number.isSafeInteger(value.count) && value.count >= 0;
  });
}

async function get(url: string, init: RequestInit = {}, deadline: number): Promise<Response> {
  const remaining = Math.min(15_000, deadline - Date.now());
  if (remaining <= 0) fail("overall 90-second timeout");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), remaining);
  try { return await fetch(url, { ...init, signal: controller.signal }); }
  catch (error) { fail(`${url.includes("squareup") ? "Square" : "ECB"} request failed: ${error instanceof Error ? error.message : "network error"}`); }
  finally { clearTimeout(timer); }
}

async function squareTotals(start: Date, end: Date, deadline: number): Promise<{ days: Record<string, DayTotals>; count: number }> {
  const token = env("CAFEBOT_SQUARE_TOKEN").trim();
  const location = env("CAFEBOT_SQUARE_LOCATION").trim();
  if (!token || !location) fail("CAFEBOT_SQUARE_TOKEN and CAFEBOT_SQUARE_LOCATION are required");
  const days: Record<string, DayTotals> = {};
  const seen = new Set<string>();
  const cursors = new Set<string>();
  let cursor = "";
  let count = 0;
  while (true) {
    const params = new URLSearchParams({ location_id: location, begin_time: iso(start), end_time: iso(end), limit: "100", sort_field: "CREATED_AT", sort_order: "ASC" });
    if (cursor) params.set("cursor", cursor);
    const response = await get(`${env("CAFEBOT_SQUARE_BASE_URL").trim() || SQUARE_URL}?${params}`, { headers: { Authorization: `Bearer ${token}`, "Square-Version": env("CAFEBOT_SQUARE_VERSION").trim() || "2025-10-16", "Content-Type": "application/json" } }, deadline);
    if (!response.ok) fail(`Square HTTP ${response.status}`);
    let payload: any;
    try { payload = await response.json(); } catch { fail("Square returned invalid JSON"); }
    for (const payment of payload.payments ?? []) {
      if (payment?.status !== "COMPLETED") continue;
      if (typeof payment.id !== "string" || !payment.id) fail("Square completed payment has no valid id");
      if (seen.has(payment.id)) continue;
      const created = new Date(payment.created_at);
      const amount = payment.total_money?.amount;
      if (!Number.isFinite(created.getTime())) fail("Square completed payment has invalid created_at");
      if (payment.total_money?.currency !== "GBP") fail("Square payment currency is not GBP");
      if (!Number.isSafeInteger(amount) || amount < 0) fail("Square payment amount is not a safe integer");
      if (created < start || created >= end) continue;
      seen.add(payment.id);
      count++;
      const day = new Intl.DateTimeFormat("en-CA", { timeZone: UK, year: "numeric", month: "2-digit", day: "2-digit" }).format(created);
      days[day] = { minor: safeAdd(days[day]?.minor ?? 0, amount, "Square total"), count: safeAdd(days[day]?.count ?? 0, 1, "Square count") };
    }
    cursor = typeof payload.cursor === "string" ? payload.cursor : "";
    if (!cursor) return { days, count };
    if (cursors.has(cursor)) fail("Square pagination repeated a cursor");
    cursors.add(cursor);
  }
}

function ledgerData(): Record<string, any> {
  const raw = env("CAFEBOT_LEDGER_JSON").trim();
  if (!raw) return {};
  try { const data = JSON.parse(raw); if (!data || Array.isArray(data) || typeof data !== "object") fail("CAFEBOT_LEDGER_JSON must be an object"); return data; }
  catch (error) { if (error instanceof Error && error.message.startsWith("Revenue unavailable:")) throw error; fail("CAFEBOT_LEDGER_JSON is invalid JSON"); }
}

function ledgerTotals(ledger: Record<string, any>, dates: string[]): { minor: number; covered: number } {
  let minor = 0, covered = 0;
  for (const day of dates) {
    const record = ledger[day];
    let complete = true;
    for (const channel of ["deliveroo", "uber_eats"]) {
      const value = record?.[channel]?.gross_minor;
      if (value === undefined) { complete = false; continue; }
      if (!Number.isSafeInteger(value) || value < 0) fail(`ledger ${day}.${channel}.gross_minor is not a safe integer`);
      minor = safeAdd(minor, value, "ledger total");
    }
    if (complete) covered++;
  }
  return { minor, covered };
}

function decimal(value: string): Rational {
  const [whole, fraction = ""] = value.split(".");
  return { num: BigInt(`${whole}${fraction}`), den: 10n ** BigInt(fraction.length) };
}
async function fx(deadline: number): Promise<{ rate: Rational; display: number; date: string }> {
  const response = await get(ECB_URL, { headers: { "User-Agent": "corgi-revenue/1.0" } }, deadline);
  if (!response.ok) fail(`ECB HTTP ${response.status}`);
  const xml = await response.text();
  const rates: Record<string, string> = {};
  let latest = "";
  for (const match of xml.matchAll(/<Cube\s+time=['"](\d{4}-\d{2}-\d{2})['"][^>]*>([\s\S]*?)<\/Cube>/g)) {
    if (match[1] > latest) latest = match[1];
    for (const item of match[2].matchAll(/currency=['"]([A-Z]{3})['"]\s+rate=['"]([0-9.]+)['"]/g)) rates[item[1]] = item[2];
  }
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: UK }).format(new Date());
  const age = (Date.parse(`${today}T00:00:00Z`) - Date.parse(`${latest}T00:00:00Z`)) / 86_400_000;
  const usdValue = Number(rates.USD), gbpValue = Number(rates.GBP);
  if (!latest || !Number.isInteger(age) || age < 0 || age > 7 || !Number.isFinite(usdValue) || !Number.isFinite(gbpValue) || usdValue <= 0 || gbpValue <= 0) fail("ECB GBP/USD reference is missing, stale, or invalid");
  const usd = decimal(rates.USD), gbp = decimal(rates.GBP);
  const rational = { num: usd.num * gbp.den, den: usd.den * gbp.num };
  return { rate: rational, display: Number(rational.num) / Number(rational.den), date: latest };
}

function halfUp(minor: number, rate: Rational): number {
  const scaled = BigInt(minor) * rate.num;
  const rounded = (scaled * 2n + rate.den) / (2n * rate.den);
  if (rounded > BigInt(Number.MAX_SAFE_INTEGER)) fail("converted total exceeds safe integer range");
  return Number(rounded);
}
function money(minor: number): string { return `$${(minor / 100).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }

export async function revenue(day?: string, history?: { days: Record<string, { minor: number; count: number }>; fetchedAt: string }): Promise<{ body: string; source: object }> {
  const now = new Date();
  const target = validDay(day ?? new Intl.DateTimeFormat("en-CA", { timeZone: UK }).format(now));
  const end = localMidnight(addDays(target, 1));
  const dates: string[] = [];
  for (let i = 29; i >= 0; i--) dates.push(addDays(target, -i));
  const deadline = Date.now() + 90_000;
  const reuse = reusableHistory(history, dates.slice(0, 29), target, now);
  const [liveSquare, fxInfo] = await Promise.all([squareTotals(localMidnight(reuse ? target : dates[0]), end, deadline), fx(deadline)]);
  const squareDays: Record<string, DayTotals> = {};
  for (const date of dates.slice(0, 29)) if (reuse) squareDays[date] = { ...history!.days[date] };
  for (const [date, value] of Object.entries(liveSquare.days)) squareDays[date] = { minor: safeAdd(squareDays[date]?.minor ?? 0, value.minor, "Square total"), count: safeAdd(squareDays[date]?.count ?? 0, value.count, "Square count") };
  const square = { days: squareDays, count: Object.values(squareDays).reduce((sum, value) => safeAdd(sum, value.count, "Square count"), 0) };
  const rate = fxInfo.rate;
  const ledger = ledgerTotals(ledgerData(), dates);
  const dailySquare = square.days[target]?.minor ?? 0;
  const dailyLedger = ledgerTotals(ledgerData(), [target]).minor;
  const squareTrailing = Object.values(square.days).reduce((sum, value) => safeAdd(sum, value.minor, "Square trailing total"), 0);
  const dailyUsd = halfUp(safeAdd(dailySquare, dailyLedger, "daily total"), rate);
  const trailingUsd = halfUp(safeAdd(squareTrailing, ledger.minor, "trailing total"), rate);
  const timestamp = new Intl.DateTimeFormat("en-GB", { timeZone: UK, dateStyle: "short", timeStyle: "short" }).format(now);
  const coverage = ledger.covered === dates.length ? "complete" : "partial";
  const body = ["📊 Corgi Cafe — revenue", `Daily revenue (${target}): ${money(dailyUsd)}`, `Trailing 30 days (${dates[0]} to ${target}): ${money(trailingUsd)}`, `FX: 1 GBP = ${fxInfo.display.toFixed(4)} USD (ECB daily reference, ${fxInfo.date})`, `Coverage: ${coverage}`, `Updated: ${timestamp} UK`].join("\n");
  return { body, source: { square: { dailyMinor: dailySquare, trailingMinor: squareTrailing, dailyAmountsMinor: Object.fromEntries(dates.map((date) => [date, square.days[date]?.minor ?? 0])), completedCount: square.count, currency: "GBP", history: { days: Object.fromEntries(dates.map((date) => [date, { minor: square.days[date]?.minor ?? 0, count: square.days[date]?.count ?? 0 }])), fetchedAt: reuse ? history!.fetchedAt : now.toISOString() } }, ledger: { dailyMinor: dailyLedger, trailingMinor: ledger.minor, coveredDates: ledger.covered, expectedDates: dates.length, channels: ["deliveroo", "uber_eats"] }, fx: { rate: fxInfo.display, date: fxInfo.date, base: "GBP", quote: "USD" }, coverage, timestampUK: timestamp } };
}
