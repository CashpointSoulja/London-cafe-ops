#!/bin/sh
set -eu

# Keep the legacy Telegram environment fallback during migration only.
profile_dir=/opt/data/profiles/corgitasksops
mkdir -p "$profile_dir"

write_secret() {
  value=$(printenv "$1" 2>/dev/null || true)
  if [ -z "$value" ]; then
    return 0
  fi
  printf '%s' "$value" | base64 -d > "$2"
  chmod 600 "$2"
  unset value
}

write_secret HERMES_ENV_B64 "$profile_dir/.env"

if [ -f "$profile_dir/.env" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  set -a
  . "$profile_dir/.env"
  set +a
fi

exec python /opt/hermes-cloud/deterministic_bot.py
