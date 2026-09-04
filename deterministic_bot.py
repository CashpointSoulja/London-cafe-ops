#!/usr/bin/env python3
"""One Telegram poller, SQLite work/outbox, no AI dependency."""
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT = str(Path(__file__).parent / "scripts/revenue_summary.py")
DB_PATH = Path(os.getenv("BOT_DB_PATH", "/opt/data/corgi.sqlite3"))
UK = ZoneInfo("Europe/London")
LAST_POLL = 0.0
BOT_USERNAME = "londoncafeopsbot"
CACHE = ("", 0.0, "")
WORKERS = []


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                done INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, chat TEXT, text TEXT NOT NULL,
                sent INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0);
        """)


def api(token, method, payload=None, timeout=40):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Telegram request rejected")
    return result


def parse_command(text):
    parts = text.strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return None
    command, _, target = parts[0][1:].partition("@")
    if target and target.lower() != BOT_USERNAME.lower():
        return None
    command = command.lower()
    if command not in {"revenue", "task", "wins"}:
        return None
    return command, parts[1].strip() if len(parts) > 1 else ""


def ingest(updates):
    # Persist work and offset together before acknowledging Telegram updates.
    with database() as db:
        for update in updates:
            message = update.get("message") or {}
            parsed = parse_command(str(message.get("text", "")))
            if parsed and message.get("chat", {}).get("id") is not None:
                db.execute("INSERT OR IGNORE INTO jobs(id,kind,payload) VALUES(?,?,?)",
                           (str(update["update_id"]), parsed[0], json.dumps(message)))
        if updates:
            offset = max(int(update["update_id"]) for update in updates) + 1
            db.execute("INSERT OR REPLACE INTO state VALUES('offset',?)", (str(offset),))


def revenue_text(day=None):
    global CACHE
    today = datetime.now(UK).date().isoformat()
    if day is None and CACHE[0] == today and time.monotonic() - CACHE[1] < 60:
        return CACHE[2]
    env = os.environ.copy()
    env["SQUARE_TIMEZONE"] = "Europe/London"
    env["REPORT_DATE"] = day or today
    result = subprocess.run([sys.executable, SCRIPT, "--text-only"],
                            capture_output=True, text=True, timeout=180, env=env)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Revenue source unavailable")
    text = result.stdout.strip()
    if day is None:
        CACHE = (today, time.monotonic(), text)
    return text


def due_day(now):
    local = now.astimezone(UK)
    if (local.hour, local.minute) < (0, 5):
        return None
    return (local.date() - timedelta(days=1)).isoformat()


def schedule(now):
    day = due_day(now)
    if day and os.getenv("REPORT_CHAT_ID", "").strip():
        with database() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,kind,payload) VALUES(?,?,?)",
                       ("daily:" + day, "daily", json.dumps(day)))


def complete(job, messages):
    with database() as db:
        for suffix, chat, text in messages:
            db.execute("INSERT OR IGNORE INTO outbox(id,chat,text) VALUES(?,?,?)",
                       (job["id"] + suffix, chat, text))
        db.execute("UPDATE jobs SET done=1 WHERE id=?", (job["id"],))


def process(job):
    payload = json.loads(job["payload"])
    if job["kind"] == "daily":
        complete(job, [(":report", None, revenue_text(payload))])
        return
    command, argument = parse_command(payload["text"])
    chat = str(payload["chat"]["id"])
    messages = []
    if command == "revenue":
        reply = revenue_text()
    elif not argument:
        reply = f"Usage: /{command} <description>"
    elif len(argument) > 3000:
        reply = "Please keep the description to 3,000 characters."
    else:
        label = "Task" if command == "task" else "Win"
        reply = f"✅ {label} recorded: {argument}"
        if command == "wins":
            user = payload.get("from") or {}
            name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
            name = name or user.get("username") or "Team member"
            messages.append((":win", None, f"🏆 Corgi Cafe — team win\n{name[:200]}\n{argument}"))
            reply += "\nQueued for the reporting chat."
    messages.append((":reply", chat, reply))
    complete(job, messages)


def retry(table, row, delay=None):
    assert table in {"jobs", "outbox"}
    delay = delay if delay is not None else min(300, 2 ** min(row["attempts"] + 1, 8))
    with database() as db:
        db.execute(f"UPDATE {table} SET attempts=attempts+1,next_at=? WHERE id=?",
                   (time.time() + delay, row["id"]))


def work(reports):
    # ponytail: one worker per lane; add lanes only if cafe traffic needs them.
    while True:
        with database() as db:
            job = db.execute("SELECT * FROM jobs WHERE done=0 AND next_at<=? "
                "AND (kind IN ('revenue','daily'))=? ORDER BY (kind='daily') DESC,rowid LIMIT 1",
                (time.time(), int(reports))).fetchone()
        if not job:
            time.sleep(1)
            continue
        try:
            process(job)
        except Exception as exc:
            print(f"job failed: {type(exc).__name__}", flush=True)
            if job["kind"] == "revenue":
                chat = str(json.loads(job["payload"])["chat"]["id"])
                complete(job, [(":reply", chat, "Revenue is temporarily unavailable. Please try again shortly.")])
            else:
                retry("jobs", job)


def send_pending(token):
    destination = os.getenv("REPORT_CHAT_ID", "").strip()
    with database() as db:
        row = db.execute("SELECT * FROM outbox WHERE sent=0 AND next_at<=? "
                         "AND (chat IS NOT NULL OR ?!='') ORDER BY rowid LIMIT 1",
                         (time.time(), destination)).fetchone()
    if row is None:
        return False
    payload = {"chat_id": row["chat"] or destination, "text": row["text"]}
    if row["chat"] is None and os.getenv("REPORT_THREAD_ID", "").strip():
        payload["message_thread_id"] = int(os.environ["REPORT_THREAD_ID"])
    try:
        api(token, "sendMessage", payload)
    except urllib.error.HTTPError as exc:
        delay = 300
        if exc.code == 429:
            try:
                delay = int(json.load(exc).get("parameters", {}).get("retry_after", 30))
            except (ValueError, TypeError):
                pass
        print(f"Telegram delivery HTTP {exc.code}", flush=True)
        retry("outbox", row, max(1, delay))
    except Exception as exc:
        print(f"delivery failed: {type(exc).__name__}", flush=True)
        retry("outbox", row)
    else:
        with database() as db:
            db.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))
    return True


def sender(token):
    while True:
        send_pending(token)
        time.sleep(0.1)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        healthy = LAST_POLL > 0 and time.monotonic() - LAST_POLL < 90 and len(WORKERS) == 3 and all(t.is_alive() for t in WORKERS)
        body = json.dumps({"ok": healthy, "service": "corgi-revenue",
                           "broadcast_configured": bool(os.getenv("REPORT_CHAT_ID", "").strip())}).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def run():
    global BOT_USERNAME, LAST_POLL
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    os.umask(0o077)
    initialize()
    lock = DB_PATH.with_suffix(".lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            BOT_USERNAME = api(token, "getMe")["result"]["username"]
            api(token, "setMyCommands", {"commands": [
                {"command": "revenue", "description": "Daily and trailing 30-day revenue"},
                {"command": "task", "description": "Record a task"},
                {"command": "wins", "description": "Share a team win"}]})
            break
        except Exception as exc:
            print(f"startup failed: {type(exc).__name__}", flush=True)
            time.sleep(10)
    for target, args in [(work, (False,)), (work, (True,)), (sender, (token,))]:
        thread = threading.Thread(target=target, args=args, daemon=True)
        WORKERS.append(thread)
        thread.start()
    while True:
        if not all(thread.is_alive() for thread in WORKERS):
            raise SystemExit("Worker stopped; restarting the single gateway is required")
        try:
            schedule(datetime.now(UK))
            with database() as db:
                row = db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
            updates = api(token, "getUpdates", {"offset": int(row[0]) if row else 0,
                          "timeout": 30, "allowed_updates": ["message"]})["result"]
            ingest(updates)
            LAST_POLL = time.monotonic()
        except urllib.error.HTTPError as exc:
            print(f"Telegram polling HTTP {exc.code}", flush=True)
            if exc.code == 409:
                raise SystemExit("Conflicting Telegram receiver: stop the other gateway")
            time.sleep(10)
        except Exception as exc:
            print(f"polling failed: {type(exc).__name__}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    run()
