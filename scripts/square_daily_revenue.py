#!/usr/bin/env python3
"""Send a daily gross-revenue summary from Square to Telegram."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

SQUARE_VERSION = os.getenv("SQUARE_VERSION", "2025-10-16")


def fail(message: str) -> None:
    raise RuntimeError(message)


def square_get(path: str, token: str, params: dict[str, str]) -> dict:
    base = os.getenv("SQUARE_BASE_URL", "https://connect.squareup.com")
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{base}{path}?{query}",
        headers={
            "Authorization": f"Bearer {token}",
            "Square-Version": SQUARE_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        detail = detail.replace(token, "[redacted]")
        fail(f"Square API HTTP {exc.code}: {detail[:300]}")
    except urllib.error.URLError as exc:
        fail(f"Square API connection failed: {exc.reason}")


def list_completed_payments(token: str, location_id: str, start: datetime, end: datetime) -> tuple[int, int, str]:
    params = {
        "location_id": location_id,
        "begin_time": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "end_time": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "limit": "100",
        "sort_field": "CREATED_AT",
        "sort_order": "ASC",
    }
    total_minor = 0
    completed = 0
    currency = "GBP"
    while True:
        payload = square_get("/v2/payments", token, params)
        for payment in payload.get("payments", []):
            if payment.get("status") != "COMPLETED":
                continue
            completed += 1
            money = payment.get("total_money") or {}
            total_minor += int(money.get("amount") or 0)
            currency = money.get("currency") or currency
        cursor = payload.get("cursor")
        if not cursor:
            return total_minor, completed, currency
        params["cursor"] = cursor


def money(amount_minor: int, currency: str) -> str:
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, f"{currency} ")
    return f"{symbol}{Decimal(amount_minor) / Decimal(100):,.2f}"


def telegram_send(token: str, chat_id: str, text: str) -> None:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        detail = detail.replace(token, "[redacted]")
        fail(f"Telegram API HTTP {exc.code}: {detail[:300]}")
    except urllib.error.URLError as exc:
        fail(f"Telegram API connection failed: {exc.reason}")
    if not payload.get("ok"):
        fail(f"Telegram rejected the message: {payload.get('description', 'unknown error')}")
    print(f"Telegram message sent (message_id={payload.get('result', {}).get('message_id', 'unknown')})")


def main() -> int:
    square_token = os.getenv("SQUARE_ACCESS_TOKEN")
    location_id = os.getenv("SQUARE_LOCATION_ID")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not all((square_token, location_id, telegram_token, chat_id)):
        fail("Missing one or more required secrets: SQUARE_ACCESS_TOKEN, SQUARE_LOCATION_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

    timezone_name = os.getenv("SQUARE_TIMEZONE", "Europe/London")
    local_now = datetime.now(ZoneInfo(timezone_name))
    report_date = os.getenv("REPORT_DATE", "").strip()
    if report_date:
        try:
            start = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=local_now.tzinfo)
        except ValueError:
            fail("REPORT_DATE must be YYYY-MM-DD")
        end = start + timedelta(days=1)
    else:
        guard_hour = os.getenv("SCHEDULE_GUARD", "").strip()
        if guard_hour and local_now.hour != int(guard_hour):
            print(f"Skipping outside the local broadcast hour ({local_now.strftime('%H:%M')} {timezone_name})")
            return 0
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = local_now

    total_minor, completed, currency = list_completed_payments(square_token, location_id, start, end)
    text = "\n".join(
        (
            "📊 Corgi Cafe — daily revenue",
            f"Date: {start.date().isoformat()}",
            f"Gross collected: {money(total_minor, currency)}",
            f"Completed payments: {completed}",
            f"Recorded: {local_now.strftime('%H:%M')} {timezone_name}",
        )
    )
    if os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}:
        print("DRY_RUN")
        print(text)
        return 0
    telegram_send(telegram_token, chat_id, text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
