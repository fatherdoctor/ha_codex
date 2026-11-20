"""The bundled Home Assistant MCP server (hass-mcp), as Codex starts it when the option home_assistant_mcp is on.

hass-mcp (https://github.com/voska/hass-mcp, MIT) is the server the Claude Code app for Home Assistant
bundles. The image installs one pinned version in its own virtual environment, /opt/hass-mcp, from
hass-mcp.requirements.txt (every package pinned with its SHA-256 for amd64 and aarch64).

What Codex is offered:

- The tools in READ_TOOLS. They read states, history, statistics, the error log, automations and
  dashboards, and change nothing, so each is approved ahead ("approve"), like the app's own read tools.
- The tools in WRITE_TOOLS, each with the approval mode "prompt": Codex asks before every call and keeps no
  remembered answer, the same as home_assistant_call. The question becomes a card on the chat page with the
  tool's arguments in full and plain words for what the call does (approvals.py, TOOL_CARDS). The permission
  mode of the chat decides who answers it, as for home_assistant_call.
- A tool that a later hass-mcp adds is in neither list, so enabled_tools leaves it out, and the test of the
  pinned tool list fails until someone sorts it.

The server needs a Home Assistant token. It never reaches a command Codex runs:

- A chat: the launcher (hass_mcp_launch.py) reads the Supervisor token from s6's container environment,
  /run/s6/container_environment, which codex-bwrap hides from every sandboxed command. Codex starts the
  launcher outside the sandbox, so the chat's own launch holds no key.
- Remote control: the relay runs as another user in its own user namespace, so the worker hands the token
  to it as HA_MCP_CAPABILITY, which only this server gets (env_vars). The name matches the exclude
  HA_*_CAPABILITY, so no shell command of the relay sees it (shell_exclude_args and
  /etc/codex/managed_config.toml).

Every chat gets the server: a page chat, a codex_cli.start_task task, a scheduled run (a normal chat that a
schedule wakes up) and the relay.
"""
from __future__ import annotations

import json
from typing import Any

SERVER_NAME = "home_assistant_mcp"
OPTION = "home_assistant_mcp"
# Must match ARG HASS_MCP_VERSION in the Dockerfile and the hass-mcp line of hass-mcp.requirements.txt.
VERSION = "0.6.0"
LAUNCHER = "/usr/share/ha_codex/hass_mcp_launch.py"
KEY_ENV = "HA_MCP_CAPABILITY"

# Every tool hass-mcp 0.6.0 has (app/server.py), in two lists. A new version is a pull request that
# checks each new tool and puts it in one of them.
READ_TOOLS = (
    "get_version",
    "get_entity",
    "list_entities",
    "get_entities_by_area",
    "search_entities_tool",
    "domain_summary_tool",
    "system_overview",
    "list_automations",
    "get_history",
    "get_history_range",
    "get_statistics",
    "get_statistics_range",
    "get_error_log",
    "list_dashboards",
    "get_dashboard_config",
    "list_view_sections",
    "list_dashboard_backups",
)
WRITE_TOOLS = (
    "entity_action",
    "call_service_tool",
    "restart_ha",
    "set_dashboard_config",
    "add_card",
    "update_card",
    "remove_card",
    "move_card",
    "add_view",
    "remove_view",
    "update_view",
    "restore_dashboard",
)


def enabled(options: dict[str, Any]) -> bool:
    """Return whether the option is on; an install that has no value yet gets the default, on."""
    value = options.get(OPTION, True)
    return value is not False


def settings(python: str, relay: bool = False) -> dict[str, Any]:
    """Return the server's Codex settings: the launcher, the read tools approved ahead, the write tools on prompt."""
    result: dict[str, Any] = {
        "command": python,
        "args": [LAUNCHER],
        **({"env_vars": [KEY_ENV]} if relay else {}),
        "enabled": True,
        # A Home Assistant that does not answer must never stop a chat from starting.
        "required": False,
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 90,
        # An allow list: a tool in neither list is never offered.
        "enabled_tools": [*READ_TOOLS, *WRITE_TOOLS],
    }
    for tool in READ_TOOLS:
        result[f"tools.{tool}.approval_mode"] = "approve"
    # "prompt", never "auto": with "auto" Codex's question offers "Allow for this session" (server.call_mcp_args).
    for tool in WRITE_TOOLS:
        result[f"tools.{tool}.approval_mode"] = "prompt"
    return result


def mcp_args(options: dict[str, Any], python: str, relay: bool = False) -> list[str]:
    """Return the --config arguments that register the server, or none when the option is off."""
    if not enabled(options):
        return []
    return [argument for name, value in settings(python, relay).items()
            for argument in ("--config", f"mcp_servers.{SERVER_NAME}.{name}={json.dumps(value)}")]
