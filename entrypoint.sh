#!/bin/sh
set -eu

export HERMES_HOME=/opt/data
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

write_secret HERMES_CONFIG_B64 "$profile_dir/config.yaml"
write_secret HERMES_PROFILE_B64 "$profile_dir/profile.yaml"
write_secret HERMES_SOUL_B64 "$profile_dir/SOUL.md"
write_secret HERMES_ENV_B64 "$profile_dir/.env"
write_secret HERMES_AUTH_B64 /opt/data/auth.json

if [ -f "$profile_dir/.env" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  set -a
  . "$profile_dir/.env"
  set +a
fi

chown -R hermes:hermes "$profile_dir" 2>/dev/null || true
if [ -f /opt/data/auth.json ]; then
  chown hermes:hermes /opt/data/auth.json
fi

exec python /opt/hermes-cloud/deterministic_bot.py
