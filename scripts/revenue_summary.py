#!/usr/bin/env python3
"""Print or send a combined daily revenue summary.

Square is queried live. Deliveroo and Uber Eats are read from the externally
maintained JSON ledger in REVENUE_LEDGER_JSON so records remain available
after a marketplace API's historical window expires.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from square_daily_revenue import (
    list_completed_payments,
    money,
    telegram_send,
)


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


def summary(day: str) -> str:
    day_start = datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=ZoneInfo(os.getenv("SQUARE_TIMEZONE", "Europe/London"))
    )
    square_minor = 0
    square_orders = 0
    currency = "GBP"
    square_status = "not connected"
    token = os.getenv("SQUARE_ACCESS_TOKEN")
    location_id = os.getenv("SQUARE_LOCATION_ID")
    if token and location_id:
        square_minor, square_orders, currency = list_completed_payments(
            token, location_id, day_start, day_start.replace(hour=23, minute=59, second=59)
        )
        square_status = f"{money(square_minor, currency)} ({square_orders} payments)"

    external = external_totals(day)
    lines = [
        "📊 Corgi Cafe — revenue",
        f"Date: {day}",
        f"Square: {square_status}",
    ]
    known_minor = square_minor
    known_channels = 1 if token and location_id else 0
    for channel, label in (("deliveroo", "Deliveroo"), ("uber_eats", "Uber Eats")):
        if channel not in external:
            lines.append(f"{label}: not recorded")
            continue
        amount, orders = external[channel]
        order_text = f" ({orders} orders)" if orders is not None else ""
        lines.append(f"{label}: {money(amount, currency)}{order_text}")
        known_minor += amount
        known_channels += 1
    if known_channels == len(CHANNELS) + 1:
        lines.append(f"FULL TOTAL: {money(known_minor, currency)}")
    else:
        lines.append(f"KNOWN TOTAL: {money(known_minor, currency)}")
        lines.append("Coverage: incomplete — add the missing channel totals to the external ledger.")
    return "\n".join(lines)


def self_test() -> None:
    os.environ["REVENUE_LEDGER_JSON"] = json.dumps(
        {"2026-09-04": {"deliveroo": {"gross_minor": 1234, "orders": 2}}}
    )
    assert external_totals("2026-09-04")["deliveroo"] == (1234, 2)
    assert "Deliveroo: £12.34 (2 orders)" in summary("2026-09-04")
    assert "FULL TOTAL" not in summary("2026-09-04")
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
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        fail("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when sending")
    telegram_send(token, chat_id, text)
    print(f"Recorded: {now.isoformat()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
