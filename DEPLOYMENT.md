# Corgi Cafe bot: pre-deployment gate

This revision is local and tested offline, not live-verified or deployed.

## Behaviour

- `/revenue`: today's gross completed payments and trailing 30 calendar days in USD; current daily ECB reference applied to both. No deductions. Timestamped, cached up to 60 seconds. Incomplete marketplace coverage remains labelled partial.
- `/task <description>`: records the submitted task, privately or in the originating chat. No assignment/reminder engine.
- `/wins <description>`: records a win and queues the submitter's name and description for the reporting destination. No automatic bonus ranking or invented winners.
- All users can use the commands. Telegram's own membership and bot visibility permissions still apply.
- The one running worker schedules yesterday's completed day after 00:05 Europe/London, including DST. L30D ends on that same day. Polling checks every 30 seconds; provider/network latency can delay delivery.
- Wins and daily reporting share `REPORT_CHAT_ID` and optional `REPORT_THREAD_ID`. Tasks are never forwarded there. Blank destination disables broadcasts and retains submitted wins for later delivery.

## Destination awaiting access

User supplied `https://t.me/c/2394851554/203272`.
The candidate chat ID is `-1002394851554`; `203272` is a message ID, not a verified topic ID.
On 2026-09-04, Telegram `getChat` returned `Bad Request: chat not found` for this bot.
Do not activate or assume a topic. Add `@londoncafeopsbot`, verify membership and posting permissions, then set the destination. Only test in Ayo's private chat.

## Deployment requirements (not yet satisfied)

1. An always-running host with restart-on-failure and a persistent volume mounted at `/opt/data`. Free Render sleep/ephemeral storage does **not** meet this requirement. The keepalive workflow is best effort, not an uptime guarantee.
2. Exactly one deployed Telegram poller. Stop the old poller before starting this one; never test by launching a second production poller locally.
3. Retain/back up the database and any existing legacy records before migration. SQLite state survives process restarts only while its underlying storage survives. Existing JSONL files are left untouched, not imported.
4. Configure `TELEGRAM_BOT_TOKEN`, `SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID` securely. `SQUARE_TIMEZONE=Europe/London`. No credentials in Git, logs, or messages.
5. Disable the old GitHub daily schedule at cutover; the replacement workflow is preview-only. The bot owns nightly scheduling, avoiding multiple broadcasters.
6. Verify `/revenue`, `/task`, `/wins` privately, restart recovery, actual private delivery, live Square totals, and the chosen group's permissions before announcing completion.

## Reliability scope

SQLite persists received work before acknowledging it. Tasks/wins and revenue have separate worker lanes. Outbound sends retry; confirmed sends are recorded. Duplicate incoming updates and daily jobs are deduplicated.

Telegram has no send-message idempotency key: if it accepts a message but the response is lost, a retry can duplicate it. This implementation favours delivery over silent loss; it does not promise exactly-once delivery or zero downtime. An outage spanning multiple whole days is not automatically backfilled by the scheduler.

Deliveroo/Uber Eats automated ingestion, four-location comparisons, bonus rules, and monthly subsidiary reports are not implemented by this change. Marketplace ledger inputs must not duplicate Square payments.

## Checks

Run `python3 -m unittest -v test_bot` and `python3 scripts/revenue_summary.py --self-test`.
These tests never contact Telegram. They cover routing, open access, persisted replay, retry, UK DST, previous-day scheduling, gross-payment pagination, partial coverage, and caching. They are not evidence of live deployment.
