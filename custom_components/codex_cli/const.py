"""Constants for the Codex CLI integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "codex_cli"

CONF_BASE_URL: Final = "base_url"
# Not stored: an entry before 0.4 kept the worker token under this key. Diagnostics still redact it.
CONF_API_TOKEN: Final = "api_token"

WORKER_ADDON_NAME: Final = "Codex CLI Worker"
WORKER_ADDON_SLUG: Final = "codex_cli_worker"
DEFAULT_SCAN_INTERVAL_SECONDS: Final = 30

SERVICE_START_TASK: Final = "start_task"
SERVICE_GET_TASK: Final = "get_task"
SERVICE_LIST_TASKS: Final = "list_tasks"
SERVICE_CANCEL_TASK: Final = "cancel_task"
SERVICE_REPLY_TASK: Final = "reply_task"
SERVICE_CONTINUE_TASK: Final = "continue_task"
SERVICE_START_LOGIN: Final = "start_login"
SERVICE_LOGOUT: Final = "logout"
SERVICE_GET_LOGIN_STATUS: Final = "get_login_status"

ATTR_PROMPT: Final = "prompt"
ATTR_TASK_ID: Final = "task_id"
# A task ID as the worker makes and checks it (TASK_ID_RE in codex-cli-worker/server.py). The actions check it, so a
# "/", "?" or "#" in an ID can never send the request to a different worker route.
TASK_ID_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
ATTR_REPLY: Final = "reply"
ATTR_TURN_ID: Final = "turn_id"
ATTR_FORCE: Final = "force"

# The GitHub sign-in (add-on 0.3): the worker reports it under "github" in /status.
ISSUE_GITHUB_ACCESS_LOST: Final = "github_access_lost"
EVENT_GITHUB_ACCESS: Final = "codex_cli_github_access"
GITHUB_ERROR_STATES: Final = ("expired", "revoked", "failed")
GITHUB_WARNING_STATES: Final = ("near_expiry", "access_changed")
