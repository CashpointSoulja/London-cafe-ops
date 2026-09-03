#!/bin/sh
set -eu

profile_dir=/opt/data/profiles/corgitasksops
mkdir -p "$profile_dir"

write_secret() {
  value=$(printenv "$1")
  printf '%s' "$value" | base64 -d > "$2"
  chmod 600 "$2"
  unset value
}

write_secret HERMES_CONFIG_B64 "$profile_dir/config.yaml"
write_secret HERMES_PROFILE_B64 "$profile_dir/profile.yaml"
write_secret HERMES_SOUL_B64 "$profile_dir/SOUL.md"
write_secret HERMES_ENV_B64 "$profile_dir/.env"
write_secret HERMES_AUTH_B64 /opt/data/auth.json

python -m http.server "${PORT:-8080}" --directory /opt/hermes-cloud >/tmp/health.log 2>&1 &
exec /opt/hermes/docker/entrypoint-dispatch.sh --profile corgitasksops gateway run
