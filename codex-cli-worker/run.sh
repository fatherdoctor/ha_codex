#!/usr/bin/with-contenv bashio
set -euo pipefail

mkdir -p /data/codex-home /config/codex_tasks
export CODEX_HOME=/data/codex-home
export HOME=/data
export PYTHONDONTWRITEBYTECODE=1

token_file=/data/worker_api_token
legacy_token="$(python3 -c 'import json, pathlib; p=pathlib.Path("/data/options.json"); data=json.loads(p.read_text()) if p.exists() else {}; print(data.get("api_token") or "")')"

if [ ! -s "${token_file}" ]; then
    if [ -n "${legacy_token}" ]; then
        printf '%s' "${legacy_token}" > "${token_file}"
        bashio::log.info "Migrated worker API token from app options into private app storage."
    else
        python3 -c 'import pathlib, secrets; pathlib.Path("/data/worker_api_token").write_text(secrets.token_urlsafe(32), encoding="utf-8")'
        bashio::log.info "Generated a random worker API token in private app storage."
    fi
    chmod 600 "${token_file}"
fi

if [ -n "${legacy_token}" ]; then
    bashio::addon.option 'api_token'
    bashio::log.info "Removed legacy worker API token from app options."
fi

# Install or update the bundled Codex integration in /config (integration_sync.py); never blocks the start.
python3 /integration_sync.py || bashio::log.warning "The integration install step failed; the worker starts anyway."
# Announce the app through Supervisor discovery once Home Assistant has loaded the integration. It runs in the
# background because, after a fresh install, that happens only when the owner restarts Home Assistant.
python3 /integration_sync.py --announce &

echo "Codex CLI Worker starting"
if ! python3 -I /protection_switches.py \
    --template /usr/share/ha_codex/requirements-template.toml \
    --write-requirements /etc/codex/requirements.toml; then
    echo "WARNING: Could not prepare protection choices; keeping the installed command requirements." >&2
fi
python3 /server.py
