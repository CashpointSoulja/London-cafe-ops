"""Offline checks: python3 -m unittest -v test_bot (never contacts Telegram)."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import sys

import deterministic_bot as bot
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
import revenue_summary as revenue
import square_daily_revenue as square


class BotChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = patch.object(bot, "DB_PATH", Path(self.directory.name) / "test.sqlite3")
        self.db.start()
        self.env = patch.dict(os.environ, {"REPORT_CHAT_ID": "", "REPORT_THREAD_ID": ""})
        self.env.start()
        bot.initialize()

    def tearDown(self):
        self.env.stop()
        self.db.stop()
        self.directory.cleanup()

    def job(self, update=1, text="/wins Sold out of coffee"):
        item = {"update_id": update, "message": {"chat": {"id": 123},
                "from": {"id": 99, "first_name": "Ayo"}, "text": text}}
        bot.ingest([item])
        with bot.database() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (str(update),)).fetchone()

    def test_commands_and_open_access(self):
        self.assertEqual(bot.parse_command(" /task\nOrder milk "), ("task", "Order milk"))
        self.assertEqual(bot.parse_command("/revenue@londoncafeopsbot"), ("revenue", ""))
        self.assertIsNone(bot.parse_command("/revenue@anotherbot"))
        self.assertIsNone(bot.parse_command("/help"))
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "somebody_else"}):
            self.assertIsNotNone(self.job())

    def test_update_replay_and_restart_preserve_work(self):
        self.job()
        self.job()
        bot.initialize()
        with bot.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT value FROM state WHERE key='offset'").fetchone()[0], "2")
            self.assertEqual(db.execute("SELECT done FROM jobs").fetchone()[0], 0)

    def test_wins_wait_for_destination_and_send_once_normally(self):
        job = self.job()
        bot.process(job)
        bot.process(job)  # replay cannot insert duplicate outbound work
        with patch.object(bot, "api", return_value={"ok": True}) as send:
            self.assertTrue(bot.send_pending("not-a-token"))
            self.assertEqual(send.call_args.args[2]["chat_id"], "123")
            self.assertFalse(bot.send_pending("not-a-token"))
            with patch.dict(os.environ, {"REPORT_CHAT_ID": "456", "REPORT_THREAD_ID": "7"}):
                self.assertTrue(bot.send_pending("not-a-token"))
                payload = send.call_args.args[2]
                self.assertEqual(payload["chat_id"], "456")
                self.assertEqual(payload["message_thread_id"], 7)
                self.assertIn("Ayo\nSold out of coffee", payload["text"])
                self.assertFalse(bot.send_pending("not-a-token"))

    def test_send_failure_is_retained_for_retry(self):
        bot.process(self.job(text="/task Order milk"))
        error = urllib.error.HTTPError("redacted", 429, "limited", {},
                                      io.BytesIO(b'{"parameters":{"retry_after":42}}'))
        with patch.object(bot, "api", side_effect=error):
            bot.send_pending("not-a-token")
        with bot.database() as db:
            row = db.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual(row["sent"], 0)
            self.assertEqual(row["attempts"], 1)
            self.assertGreater(row["next_at"], bot.time.time() + 40)
            db.execute("UPDATE outbox SET next_at=0")
        bot.initialize()
        with patch.object(bot, "api", return_value={"ok": True}):
            self.assertTrue(bot.send_pending("not-a-token"))
            self.assertFalse(bot.send_pending("not-a-token"))

    def test_uk_midnight_dst_and_one_daily_job(self):
        for value, expected in [
            ("2026-09-04T23:04:00+00:00", None),
            ("2026-09-04T23:05:00+00:00", "2026-09-04"),
            ("2026-01-05T00:05:00+00:00", "2026-01-04"),
            ("2026-03-29T23:05:00+00:00", "2026-03-29"),
            ("2026-10-26T00:05:00+00:00", "2026-10-25")]:
            self.assertEqual(bot.due_day(datetime.fromisoformat(value)), expected)
        now = datetime.fromisoformat("2026-09-04T23:05:00+00:00")
        bot.schedule(now)
        with bot.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        with patch.dict(os.environ, {"REPORT_CHAT_ID": "456"}):
            bot.schedule(now)
            bot.schedule(now + bot.timedelta(hours=8))
        with bot.database() as db:
            job = db.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        with patch.object(bot, "revenue_text", return_value="Daily report") as report:
            bot.process(job)
            report.assert_called_once_with("2026-09-04")
        with bot.database() as db:
            row = db.execute("SELECT * FROM outbox").fetchone()
            self.assertIsNone(row["chat"])

    def test_task_does_not_call_revenue(self):
        with patch.object(bot, "revenue_text", side_effect=AssertionError("wrong lane")):
            bot.process(self.job(text="/task Order milk"))

    def test_revenue_cache_and_source_failure(self):
        result = type("Result", (), {"returncode": 0, "stdout": "report"})()
        with patch.object(bot, "CACHE", ("", 0, "")), patch.object(bot.subprocess, "run", return_value=result) as run:
            self.assertEqual(bot.revenue_text(), "report")
            self.assertEqual(bot.revenue_text(), "report")
            self.assertEqual(run.call_count, 1)
            bot.revenue_text("2026-09-03")
            self.assertEqual(run.call_count, 2)
        result.returncode = 1
        result.stdout = "secret provider detail"
        with patch.object(bot, "CACHE", ("", 0, "")), patch.object(bot.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "^Revenue source unavailable$"):
                bot.revenue_text()

    def test_payment_pagination_gross_and_exclusive_dates(self):
        start = datetime(2026, 9, 4, tzinfo=bot.UK)
        end = start + bot.timedelta(days=1)
        def payment(id, created, amount=1000, status="COMPLETED"):
            return {"id": id, "created_at": created, "status": status,
                    "total_money": {"amount": amount, "currency": "GBP"},
                    "refunded_money": {"amount": 500, "currency": "GBP"}}
        first = payment("one", "2026-09-03T23:00:00Z")
        pages = [{"payments": [first], "cursor": "next"},
                 {"payments": [first, payment("two", "2026-09-04T22:00:00Z", 234),
                               payment("next-day", "2026-09-04T23:00:00Z"),
                               payment("pending", "2026-09-04T21:00:00Z", status="PENDING")]}]
        with patch.object(square, "square_get", side_effect=pages):
            self.assertEqual(square.list_completed_payments("unused", "location", start, end), (1234, 2, "GBP"))

    def test_summary_totals_single_query_and_partial_label(self):
        with patch.dict(os.environ, {"SQUARE_ACCESS_TOKEN": "unused", "SQUARE_LOCATION_ID": "test",
                    "SQUARE_TIMEZONE": "Europe/London", "REVENUE_LEDGER_JSON": "{}", "REVENUE_LEDGER_PATH": ""}), \
             patch.object(revenue, "daily_completed_payments", return_value=({"2026-09-04": (10000, 2), "2026-08-06": (20000, 3)}, "GBP")) as payments, \
             patch.object(revenue, "fx_rate", return_value=Decimal("1.35")):
            text = revenue.summary("2026-09-04")
        self.assertIn("Daily revenue (2026-09-04): $135.00", text)
        self.assertIn("Trailing 30 days (2026-08-06 to 2026-09-04): $405.00", text)
        self.assertIn("Coverage: partial", text)
        self.assertNotIn("refund", text.lower())
        self.assertEqual(payments.call_count, 1)
        self.assertEqual(payments.call_args.args[2].date().isoformat(), "2026-08-06")

    def test_health_is_503_before_poll_and_when_worker_dies(self):
        from http.client import HTTPConnection

        server = bot.ThreadingHTTPServer(("127.0.0.1", 0), bot.Health)
        thread = bot.threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        bot.LAST_POLL = 0
        bot.WORKERS[:] = []
        connection = HTTPConnection(*server.server_address)
        connection.request("GET", "/health")
        self.assertEqual(connection.getresponse().status, 503)
        connection.close()

        class Live:
            def is_alive(self):
                return True
        bot.LAST_POLL = bot.time.monotonic()
        bot.WORKERS[:] = [Live(), Live(), Live()]
        connection = HTTPConnection(*server.server_address)
        connection.request("GET", "/health")
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()

        class Dead(Live):
            def is_alive(self):
                return False
        bot.WORKERS[-1] = Dead()
        connection = HTTPConnection(*server.server_address)
        connection.request("GET", "/health")
        self.assertEqual(connection.getresponse().status, 503)
        connection.close()
        server.shutdown()
        thread.join(timeout=2)

    def test_square_rejects_currency_mismatch_and_repeated_cursor(self):
        start = datetime(2026, 9, 4, tzinfo=bot.UK)
        end = start + bot.timedelta(days=1)
        payment = {"id": "one", "created_at": "2026-09-04T12:00:00Z",
                   "status": "COMPLETED", "total_money": {"amount": 100,
                   "currency": "EUR"}}
        with patch.object(square, "square_get", return_value={"payments": [payment]}):
            with self.assertRaisesRegex(RuntimeError, "Unexpected Square currency"):
                square.list_completed_payments("unused", "location", start, end)
        with patch.object(square, "square_get", side_effect=[
                {"payments": [], "cursor": "same"}, {"payments": [], "cursor": "same"}]):
            with self.assertRaisesRegex(RuntimeError, "repeated a cursor"):
                square.list_completed_payments("unused", "location", start, end)

    def test_fx_rejects_stale_or_missing_reference_date(self):
        class Response:
            def __init__(self, body):
                self.body = body.encode()
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return self.body

        stale = '<Cube time="2020-01-01"><Cube currency="GBP" rate="0.8"/><Cube currency="USD" rate="1.1"/></Cube>'
        missing = '<Cube><Cube currency="GBP" rate="0.8"/><Cube currency="USD" rate="1.1"/></Cube>'
        for body in (stale, missing):
            with patch.object(urllib.request, "urlopen", return_value=Response(body)):
                with self.assertRaisesRegex(RuntimeError, "FX daily reference is missing or stale"):
                    revenue.fx_rate("GBP", "USD")

    def test_midnight_catchup_queues_previous_uk_day_once(self):
        with patch.dict(os.environ, {"REPORT_CHAT_ID": "-2394851554"}):
            moment = datetime.fromisoformat("2026-09-05T00:05:00+01:00")
            bot.schedule(moment)
            bot.schedule(moment.replace(second=30))
        with bot.database() as db:
            rows = db.execute("SELECT id, kind, payload FROM jobs WHERE kind='daily'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "daily:2026-09-04")


if __name__ == "__main__":
    unittest.main()
