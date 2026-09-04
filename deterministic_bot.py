#!/usr/bin/env python3
"""Single-process Telegram worker for deterministic revenue commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SCRIPT = "/opt/hermes-cloud/scripts/revenue_summary.py"


def api(token: str, method: str, payload: dict | None = None, timeout: int = 40) -> dict:
    data = None
    url = f"https://api.telegram.org/bot{token}/{method}"
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", f"Telegram {method} failed"))
    return result


def allowed(message: dict) -> bool:
    configured = {
        item.strip().lower()
        for item in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
        if item.strip()
    }
    if not configured:
        return True
    user = message.get("from") or {}
    username = str(user.get("username", "")).lower()
    return (
        str(user.get("id", "")) in configured
        or username in configured
        or f"@{username}" in configured
    )


def parse_command(text: str) -> tuple[str, str] | None:
    first, _, rest = text.strip().partition(" ")
    if not first.startswith("/"):
        return None
    command = first[1:].split("@", 1)[0].lower()
    return command, rest.strip()


def revenue_text() -> str:
    env = os.environ.copy()
    env.pop("REPORT_DATE", None)
    try:
        result = subprocess.run(
            [sys.executable, SCRIPT, "--text-only"],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Revenue unavailable right now: {exc}"
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        return f"Revenue unavailable right now: {detail[0] if detail else 'source error'}"
    return result.stdout.strip() or "Revenue unavailable right now: empty source response"


def response_for(command: str, argument: str) -> str | None:
    if command == "revenue":
        return revenue_text()
    if command in {"start", "help", "commands"}:
        return "Corgi Cafe bot\n\n/revenue — current day's live revenue\n/help — show available commands"
    return None


class Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(
            {"ok": True, "service": "corgi-revenue", "last_update": LAST_UPDATE}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


LAST_UPDATE = "never"


def self_test() -> None:
    assert parse_command("/revenue") == ("revenue", "")
    assert parse_command("/revenue@londoncafeopsbot now") == ("revenue", "now")
    assert parse_command("hello") is None
    assert response_for("help", "")
    assert response_for("unknown", "") is None
    print("self-test passed")


def run() -> None:
    global LAST_UPDATE
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api(
        token,
        "setMyCommands",
        {
            "commands": json.dumps(
                [
                    {"command": "revenue", "description": "Today's live revenue"},
                    {"command": "help", "description": "Show available commands"},
                ]
            )
        },
    )
    offset = 0
    while True:
        try:
            updates = api(
                token,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": '["message"]',
                },
                timeout=40,
            ).get("result", [])
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message") or {}
                if not allowed(message):
                    continue
                parsed = parse_command(str(message.get("text", "")))
                if not parsed:
                    continue
                reply = response_for(*parsed)
                if reply is not None:
                    api(
                        token,
                        "sendMessage",
                        {"chat_id": str(message["chat"]["id"]), "text": reply},
                    )
                LAST_UPDATE = datetime.now(timezone.utc).isoformat()
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                time.sleep(5)
            else:
                time.sleep(10)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            print(f"worker warning: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        run()
