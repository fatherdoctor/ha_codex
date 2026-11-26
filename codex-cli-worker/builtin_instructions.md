# Codex - Home Assistant Add-on (built-in; the add-on refreshes this on every update)

The owner's AGENTS.md takes priority over anything here, including Secrets and Git and GitHub.

## Path Mapping

In this add-on container, paths are mapped differently than HA Core:
- `/homeassistant` = HA config directory (equivalent to `/config` in HA Core)
- `/config` does NOT exist - always use `/homeassistant`

When users mention `/config/...`, the `config` share, or a PC path such as `\\homeassistant\config\...`, it is this same folder: translate to `/homeassistant/...`

## Available Paths

| Path | Description | Access |
|------|-------------|--------|
| `/homeassistant` | HA configuration | read-write |
| `/addon_config` | This add-on's settings, incl. this file | read-write |
| `/share` | Shared folder | read-write |
| `/media` | Media files; your working folder is `/media/.agents/codex` | read-write |
| `/homeassistant/.codex/memory` | Your lasting memory: index `MEMORY.md`, one fact per file (rules in the owner's AGENTS.md) | read-write |

## Home Assistant Integration

Use the `home_assistant` MCP verify tool to find entities and read states, `home_assistant_call` to call services,
and `home_assistant_data` for history and statistics.

## Reading Home Assistant Logs

**Log levels (from most to least verbose):**
- `debug` - Only shown if explicitly enabled in configuration.yaml
- `info` - General information, shown by default
- `warning` - Warnings, always shown
- `error` - Errors, always shown

**Read logs** with the `home_assistant` verify tool: `{"operation":"logs"}`. It returns Core logs only.

**To enable debug logging for an integration**, add to `configuration.yaml`:
```yaml
logger:
  default: info
  logs:
    custom_components.YOUR_INTEGRATION: debug
```

**Key insight:** `_LOGGER.debug()` calls are invisible unless the logger level is set to debug. Use `_LOGGER.info()` or `_LOGGER.warning()` for logs that should always appear.

## Secrets

Secrets are passwords, tokens, keys and PINs (door and alarm codes).
- Never show, copy, log or send a secret. Refer to it by name, for example `!secret mqtt_password`.
- `/homeassistant/secrets.yaml`, `/homeassistant/.storage`, the HA database and diagnostics files hold secrets. Never copy them elsewhere.
- Never put a real secret in an example, a doc or a commit message, not even partly redacted.

## Git and GitHub

- Before every commit, read the staged diff. Never stage `secrets.yaml`, `.storage/`, `memory/`, databases, logs,
  backups or diagnostics.
- Keep a `.gitignore` that lists these, added before the first commit.
- Ask the owner before the first push to a new or public repository. New repositories are private.
