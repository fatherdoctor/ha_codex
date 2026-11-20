#!/usr/bin/env python3
"""Start the bundled hass-mcp with a Home Assistant token that no sandboxed command can read.

Codex starts this file as the command of the home_assistant_mcp server, outside its sandbox (see
hass_mcp.py). It takes the token from HA_MCP_CAPABILITY (remote control) or from s6's container
environment (a chat), sets the three variables hass-mcp reads, and replaces itself with hass-mcp.
It never prints the token. With no token it exits with a message, and Codex reports the server as
failed to start; the chat itself still starts.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

KEY_ENV = "HA_MCP_CAPABILITY"
TOKEN_FILE = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")
SERVER = "/opt/hass-mcp/bin/hass-mcp"
CORE_URL = "http://supervisor/core"
BACKUP_DIR = "/data/hass-mcp-backups"


def token(environ: dict[str, str], token_file: Path = TOKEN_FILE) -> str:
    """Return the token from the environment key, else from the file; empty when neither has one."""
    value = environ.get(KEY_ENV, "").strip()
    if value:
        return value
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def server_env(environ: dict[str, str], value: str) -> dict[str, str]:
    """Return hass-mcp's environment: Codex's own, without the key, plus the address and the token."""
    env = {key: item for key, item in environ.items() if key != KEY_ENV}
    env.update(HA_URL=CORE_URL, HA_TOKEN=value, HASS_MCP_BACKUP_DIR=BACKUP_DIR)
    return env


def main() -> int:
    value = token(dict(os.environ))
    if not value:
        print("Home Assistant MCP: no Supervisor token is available, so the server does not start.",
              file=sys.stderr, flush=True)
        return 1
    os.execve(SERVER, [SERVER], server_env(dict(os.environ), value))
    return 1  # Not reached: execve replaces this process or raises.


if __name__ == "__main__":
    sys.exit(main())
