# Corgi Cafe bot — deployment record

The live bot is Supabase Edge Function `cafe-bot`, version 4, in Corgi Relay project `pvrmzqxtmhewyrluuqka`. It uses one Telegram webhook for `@londoncafeopsbot`; no Telegram poller is required. Render is excluded from the Telegram path. Its old process cannot be shut down from this workspace, so it must not be started alongside the webhook.

## Live behaviour

- `/revenue` returns one gross daily USD figure and trailing-30-day USD figure. Refunds are not mentioned or subtracted.
- `/task` records the submitted text.
- `/wins` records the submitter and achievement and broadcasts that winner information to the configured reporting chat.
- Anyone who can reach the bot may use the commands.
- `pg_cron` runs `cafe-bot-worker` every minute through `pg_net`. At 00:05 Europe/London it queues the previous UK calendar day and its trailing 30-day report. Daily reports and wins use the same configured destination.
- Jobs, outbox, and cached reports persist in PostgreSQL. RLS is locked down; only the service role accesses these tables.

## Verified state

Private tests completed: `/revenue`, `/task`, and `/wins` sent successfully 4 times; a real incoming win was processed successfully. `cafe-bot-worker` completed successfully after `pg_net` was installed. Automated resolver supplied only `-2394851554` and `-1002394851554`; neither is a safe assumption from a Telegram URL. No other fallback is used, and forum topics are skipped until clarified.

The destination remains unset until the bot is added to the Corgi group and Telegram `getChat` succeeds. The user must add `@londoncafeopsbot` and grant posting permission. Tests remain private; no group test was sent.

## Revenue coverage

Square is connected. Deliveroo is verified only in sandbox, so marketplace coverage remains partial. Uber Eats is not connected. Monthly subsidiary email reporting to Emily and Derrick is not built because its format and recipients are unconfirmed. Per-drink notifications, four-location rankings, and bonus calculations are not built; the requested Nico-facing version is concise daily revenue, L30D revenue, and submitted wins—not invented bonus awards.

## Deployment procedure

Use the Composio account alias `corgi` for Supabase operations. Tools used are `SUPABASE_BETA_RUN_SQL_QUERY`, `SUPABASE_UPDATE_A_FUNCTION`, `SUPABASE_GET_FUNCTION`, and `SUPABASE_GET_SECURITY_ADVISORS`. Bundle locally when required:

```sh
npx esbuild supabase/functions/cafe-bot/index.ts --bundle --format=esm --platform=neutral --outfile=/tmp/cafe-bot.js
```

Deploy through `SUPABASE_UPDATE_A_FUNCTION` to project `pvrmzqxtmhewyrluuqka` with `verify_jwt: false` because the function validates webhook and worker secrets itself. Store only `CAFEBOT_*` secrets (`CAFEBOT_TELEGRAM_TOKEN`, `CAFEBOT_WEBHOOK_SECRET`, `CAFEBOT_WORKER_SECRET`, `CAFEBOT_SQUARE_TOKEN`, `CAFEBOT_SQUARE_LOCATION`) and Supabase secrets; never commit or paste values.

GitHub branch `codex/cafe-reliable-reporting` contains the deployment source. The current checks pass: 13 Python tests and 10 TypeScript tests. Two real incoming private revenue requests also completed and were delivered. Silent minute-by-minute refreshes keep reports warm; older-day Square totals refresh at least every 15 minutes, while current-day payments are fetched on refresh. Reports display their data timestamp.

Before a new deployment, provision the migration, function secrets, and Vault secret named `cafe_bot_worker_secret`. The existing `cafe-bot-worker` cron job invokes `/tick` every minute using that Vault secret in `X-Worker-Secret`; both `pg_cron` and `pg_net` must be installed. Call `/install` with the worker secret only after deployment verification to register the three commands and webhook. Never copy production secret values into repository files.

Rollback must remove the webhook or deploy a corrected webhook version; never run the old polling gateway at the same time.

The local app heartbeat health monitor runs every 15 minutes and is only supplementary. Cloud scheduling is independent.
