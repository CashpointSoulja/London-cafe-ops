#!/bin/sh
set -eu

export HERMES_HOME=/opt/data
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
chown -R hermes:hermes "$profile_dir" /opt/data/auth.json

# Keep the revenue slash command enabled without replacing the user's profile config.
python - "$profile_dir/config.yaml" <<'PY'
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
plugins = config.setdefault("plugins", {})
enabled = plugins.setdefault("enabled", [])
if isinstance(enabled, list) and "corgi-revenue" not in enabled:
    enabled.append("corgi-revenue")
path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
PY
chown hermes:hermes "$profile_dir/config.yaml"

python -m http.server "${PORT:-8080}" --directory /opt/hermes-cloud >/tmp/health.log 2>&1 &
exec /opt/hermes/docker/entrypoint-dispatch.sh --profile corgitasksops gateway run
