#!/usr/bin/with-contenv bashio
set -euo pipefail

mkdir -p /data/codex-home /data/codex_tasks
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

# Versions before 0.4 shipped model_pin as gpt-6.1-sol. Clear exactly that value once, on the first start
# after the update; the marker keeps a value the owner sets later. Write the marker first so a failed
# call is not retried on every start.
pin_marker=/data/model_pin_default_cleared
if [ ! -e "${pin_marker}" ]; then
    : > "${pin_marker}"
    old_pin="$(python3 -c 'import json, pathlib; p=pathlib.Path("/data/options.json"); data=json.loads(p.read_text()) if p.exists() else {}; print(data.get("model_pin") or "")')"
    if [ "${old_pin}" = "gpt-6.1-sol" ]; then
        if bashio::addon.option 'model_pin'; then
            export MODEL_PIN_CLEARED=1
            bashio::log.info "Cleared the old default model pin gpt-6.1-sol; runs use the account's default model unless you set the model_pin option."
        else
            bashio::log.warning "Could not clear the old default model pin gpt-6.1-sol."
        fi
    fi
fi

# Install or update the bundled Codex integration in /homeassistant (integration_sync.py); never blocks the start.
python3 /integration_sync.py || bashio::log.warning "The integration install step failed; the worker starts anyway."
# Announce the app through Supervisor discovery once Home Assistant has loaded the integration. It runs in the
# background because, after a fresh install, that happens only when the owner restarts Home Assistant.
python3 /integration_sync.py --announce &

echo "Codex CLI Worker starting"
python3 /server.py
