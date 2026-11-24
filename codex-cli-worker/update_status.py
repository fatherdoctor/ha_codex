"""Tell the app page when Home Assistant needs a restart and when the app has an update.

Both facts are read, never guessed:

- Restart needed: the integration on disk (/homeassistant/custom_components/codex_cli) has a version that Home Assistant
  has not loaded. Home Assistant reads a custom integration's manifest once, when it first loads the integration,
  and keeps it until the next restart. Its WebSocket command manifest/get answers with that loaded manifest, or
  with not_found when the integration was not there at the last start (the first install). So after the app writes
  a new version, the two versions differ until Home Assistant restarts.
- Update available: Supervisor's /addons/self/info says update_available and names version_latest. The app's
  default Supervisor role allows this read; the app installs nothing and restarts nothing.

A read that fails gives no banner: the page says nothing rather than something that may be false.
"""
from __future__ import annotations

import json
import re
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import requests
import websocket

from core_address import get_json
from integration_sync import DISCOVERY_SERVICE, OPTIONS_PATH, TARGET_DIR, install_option_on, manifest_version

CORE_WEBSOCKET_URL = "ws://supervisor/core/websocket"
SELF_INFO_URL = "http://supervisor/addons/self/info"
# Both replies are a few kilobytes; the limits stop a broken reply from filling memory.
MAX_REPLY_BYTES = 256 * 1024
WEBSOCKET_SECONDS = 10
# A version goes to the page as text. Anything outside this shape is treated as unknown.
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,39}")


class ReadFailed(Exception):
    """Home Assistant or Supervisor did not give a usable answer."""


@dataclass(frozen=True)
class UpdateStatus:
    """What the page shows: each field is a version, or None for no banner."""

    restart_version: str | None
    update_version: str | None

    def as_json(self) -> dict[str, str | None]:
        """Return the reply fields of the /update-status route."""
        return {"restart_version": self.restart_version, "update_version": self.update_version}


def clean_version(value: object) -> str | None:
    """Return the value when it has the shape of a version, else None."""
    return value if isinstance(value, str) and VERSION_RE.fullmatch(value) else None


def loaded_integration_version(token: str) -> str | None:
    """Return the integration version Home Assistant has loaded, or None when it has not loaded the integration.

    Raises ReadFailed when Home Assistant cannot be asked or gives an answer that is neither a manifest nor
    not_found.
    """
    if not token:
        raise ReadFailed("no Supervisor token")
    deadline = time.monotonic() + WEBSOCKET_SECONDS
    try:
        with closing(websocket.create_connection(CORE_WEBSOCKET_URL, timeout=WEBSOCKET_SECONDS)) as ws:
            def receive() -> dict:
                ws.settimeout(max(0.1, deadline - time.monotonic()))
                raw = ws.recv()
                if len(raw) > MAX_REPLY_BYTES:
                    raise ReadFailed("reply too large")
                reply = json.loads(raw)
                if not isinstance(reply, dict):
                    raise ReadFailed("reply is not an object")
                return reply

            if receive().get("type") != "auth_required":
                raise ReadFailed("no authentication prompt")
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            if receive().get("type") != "auth_ok":
                raise ReadFailed("authentication refused")
            ws.send(json.dumps({"id": 1, "type": "manifest/get", "integration": DISCOVERY_SERVICE}))
            reply = receive()
    except (OSError, ValueError, websocket.WebSocketException) as exc:
        raise ReadFailed(str(exc)) from exc
    return manifest_reply_version(reply)


def manifest_reply_version(reply: dict) -> str | None:
    """Return the version in a manifest/get reply, None for not_found; raise ReadFailed for anything else."""
    if reply.get("id") != 1 or reply.get("type") != "result":
        raise ReadFailed("unexpected reply")
    if not reply.get("success"):
        error = reply.get("error")
        if isinstance(error, dict) and error.get("code") == "not_found":
            return None
        raise ReadFailed("manifest/get failed")
    result = reply.get("result")
    version = clean_version(result.get("version")) if isinstance(result, dict) else None
    if version is None:
        raise ReadFailed("the loaded manifest has no version")
    return version


def restart_version(token: str, target: Path = TARGET_DIR, options: Path = OPTIONS_PATH) -> str | None:
    """Return the integration version on disk when Home Assistant runs another one (or none), else None.

    With the option Install the Codex integration off, the owner installs the integration and the app reads nothing
    in custom_components (DOCS.md, Integration install), so there is no restart banner.
    """
    if not install_option_on(options):
        return None
    on_disk = clean_version(manifest_version(target))
    if on_disk is None:
        return None
    try:
        loaded = loaded_integration_version(token)
    except ReadFailed:
        return None
    return on_disk if loaded != on_disk else None


def available_update(token: str) -> str | None:
    """Return the newer app version Supervisor offers, or None when there is none or Supervisor cannot say."""
    try:
        data = get_json(SELF_INFO_URL, token, MAX_REPLY_BYTES)["data"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("update_available") is not True:
        return None
    latest = clean_version(data.get("version_latest"))
    return latest if latest and latest != data.get("version") else None


def read(token: str) -> UpdateStatus:
    """Read both facts for the page."""
    return UpdateStatus(restart_version(token), available_update(token))
