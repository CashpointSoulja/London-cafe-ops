#!/usr/bin/env python3
"""Print or send combined daily and trailing-30-day revenue in USD.

Square is queried live. Deliveroo and Uber Eats are read from the externally
maintained JSON ledger so marketplace history can be retained beyond API limits.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from square_daily_revenue import daily_completed_payments, telegram_send


CHANNELS = ("deliveroo", "uber_eats")


def fail(message: str) -> None:
    raise RuntimeError(message)


def report_date() -> tuple[str, datetime]:
    timezone_name = os.getenv("SQUARE_TIMEZONE", "Europe/London")
    now = datetime.now(ZoneInfo(timezone_name))
    value = os.getenv("REPORT_DATE", "").strip()
    if value:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            fail("REPORT_DATE must be YYYY-MM-DD")
        return value, now
    return now.date().isoformat(), now


def ledger() -> dict:
    raw = os.getenv("REVENUE_LEDGER_JSON", "").strip()
    path = os.getenv("REVENUE_LEDGER_PATH", "").strip()
    if not raw and path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"REVENUE_LEDGER_JSON is not valid JSON: {exc.msg}")
    if not isinstance(data, dict):
        fail("REVENUE_LEDGER_JSON must be an object keyed by YYYY-MM-DD")
    return data


def external_totals(day: str) -> dict[str, tuple[int, int | None]]:
    record = ledger().get(day) or {}
    if not isinstance(record, dict):
        fail(f"Revenue ledger entry for {day} must be an object")
    result: dict[str, tuple[int, int | None]] = {}
    for channel in CHANNELS:
        item = record.get(channel)
        if item is None:
            continue
        if not isinstance(item, dict):
            fail(f"Revenue ledger entry {day}.{channel} must be an object")
        amount = item.get("gross_minor")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            fail(f"Revenue ledger entry {day}.{channel}.gross_minor must be a non-negative integer")
        orders = item.get("orders")
        if orders is not None and (isinstance(orders, bool) or not isinstance(orders, int) or orders < 0):
            fail(f"Revenue ledger entry {day}.{channel}.orders must be a non-negative integer")
        result[channel] = (amount, orders)
    return result


def external_range(end_day: date, days: int) -> tuple[int, bool]:
    total = 0
    complete = True
    entries = ledger()
    for offset in range(days):
        key = (end_day - timedelta(days=offset)).isoformat()
        record = entries.get(key)
        if not isinstance(record, dict):
            complete = False
            continue
        for channel in CHANNELS:
            item = record.get(channel)
            if not isinstance(item, dict):
                complete = False
                continue
            amount = item.get("gross_minor")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                fail(f"Revenue ledger entry {key}.{channel}.gross_minor must be a non-negative integer")
            total += amount
    return total, complete


def fx_rate(base: str, quote: str) -> Decimal:
    if base == quote:
        return Decimal("1")
    request = urllib.request.Request(
        "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        headers={"User-Agent": "corgi-revenue/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            root = ET.fromstring(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
        fail(f"FX rate unavailable: {exc}")
    rates = {
        node.attrib["currency"]: Decimal(node.attrib["rate"])
        for node in root.iter()
        if "currency" in node.attrib and "rate" in node.attrib
    }
    rate_dates = [node.attrib["time"] for node in root.iter() if "time" in node.attrib]
    if not rate_dates or not 0 <= (datetime.now(ZoneInfo("Europe/London")).date() - date.fromisoformat(max(rate_dates))).days <= 7:
        fail("FX daily reference is missing or stale")
    rates["EUR"] = Decimal("1")
    try:
        if base == "EUR":
            rate = rates[quote]
        elif quote == "EUR":
            rate = Decimal("1") / rates[base]
        else:
            rate = rates[quote] / rates[base]
    except (KeyError, ZeroDivisionError) as exc:
        fail(f"FX rate unavailable for {base}/{quote}: {exc}")
    if not rate.is_finite() or rate <= 0:
        fail("FX rate must be positive")
    return rate


def convert_minor(amount_minor: int, base: str, rate: Decimal) -> int:
    if base == "USD":
        return amount_minor
    return int((Decimal(amount_minor) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def summary(day: str) -> str:
    timezone_name = os.getenv("SQUARE_TIMEZONE", "Europe/London")
    local_day = datetime.strptime(day, "%Y-%m-%d").date()
    day_start = datetime.combine(local_day, datetime.min.time(), ZoneInfo(timezone_name))
    day_end = day_start + timedelta(days=1)
    trailing_start = day_start - timedelta(days=29)

    token = os.getenv("SQUARE_ACCESS_TOKEN")
    location_id = os.getenv("SQUARE_LOCATION_ID")
    source_currency = os.getenv("REVENUE_SOURCE_CURRENCY", "GBP").upper()
    square_connected = bool(token and location_id)
    if not square_connected:
        fail("Square is not configured")
    days, source_currency = daily_completed_payments(token, location_id, trailing_start, day_end)
    square_today = days.get(day, (0, 0))[0]
    square_trailing = sum(value[0] for value in days.values())

    external_today = sum(amount for amount, _ in external_totals(day).values())
    external_trailing, external_complete = external_range(local_day, 30)
    rate = fx_rate(source_currency, "USD")
    today_total = convert_minor(square_today + external_today, source_currency, rate)
    trailing_total = convert_minor(square_trailing + external_trailing, source_currency, rate)
    coverage = "complete" if square_connected and external_complete else "partial"

    rate_line = (
        f"1 {source_currency} = {rate:.4f} USD (daily reference)"
        if source_currency != "USD"
        else "Currency: USD"
    )
    return "\n".join(
        (
            "📊 Corgi Cafe — revenue",
            f"Daily revenue ({day}): __USD__{Decimal(today_total) / 100:,.2f}",
            (
                f"Trailing 30 days ({trailing_start.date().isoformat()} to {day}): "
                f"__USD__{Decimal(trailing_total) / 100:,.2f}"
            ),
            f"FX: {rate_line}",
            f"Coverage: {coverage}",
            f"Updated: {datetime.now(ZoneInfo(timezone_name)).strftime('%Y-%m-%d %H:%M')} UK",
        )
    ).replace("__USD__", "$")


def self_test() -> None:
    os.environ["REVENUE_LEDGER_JSON"] = json.dumps(
        {
            "2026-09-04": {
                "deliveroo": {"gross_minor": 1234, "orders": 2},
                "uber_eats": {"gross_minor": 500, "orders": 1},
            }
        }
    )
    assert external_totals("2026-09-04")["deliveroo"] == (1234, 2)
    assert external_range(date(2026, 9, 4), 1) == (1734, True)
    assert convert_minor(10000, "GBP", Decimal("1.35")) == 13500
    print("self-test passed")


def main() -> int:
    if "--self-test" in sys.argv:
        self_test()
        return 0
    day, now = report_date()
    text = summary(day)
    if os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"} or "--text-only" in sys.argv:
        print(text)
        return 0
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("REPORT_CHAT_ID")
    if not token or not chat_id:
        fail("TELEGRAM_BOT_TOKEN and REPORT_CHAT_ID are required when sending")
    telegram_send(token, chat_id, text)
    print(f"Recorded: {now.isoformat()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
