"""The worker's client for Home Assistant: REST, WebSocket and persistent notifications.

server.py, ha_data.py and verification.py make their Home Assistant calls here, through the Supervisor proxy with
the Supervisor token. Three files still talk to Home Assistant on their own: update_status.py (its own WebSocket
login), relay_reads.py and ha_call.py (their own proxy address); a later step moves them here. The app's `ha_url`
option is only the address Codex gets for its own calls (core_address.py); the worker never uses it.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Callable

import requests
import websocket

API_URL = "http://supervisor/core/api/"
WS_URL = "ws://supervisor/core/websocket"
NO_TOKEN = "No Home Assistant token available"


def ha_token() -> str:
    """Return the Supervisor token the worker uses for Home Assistant, or an empty string when there is none."""
    return str(os.environ.get("SUPERVISOR_TOKEN") or "")


def api_url(path: str) -> str:
    """Return the REST API URL for a path, given with or without a leading slash or api/ prefix."""
    return API_URL + path.removeprefix("/").removeprefix("api/")


def rest(token: str, method: str, path: str, json: Any = None, *, timeout: float = 15) -> requests.Response:
    """Send one REST request to Home Assistant and return the response; connection errors are raised."""
    return requests.request(method, api_url(path), headers={"Authorization": f"Bearer {token}"},
                            json=json, timeout=timeout)


def post(token: str, path: str, data: dict[str, Any]) -> tuple[bool, str]:
    """POST data to a REST path; return (True, "ok"), or False and an error that names the URL."""
    url = api_url(path)
    try:
        response = rest(token, "POST", path, data)
    except Exception as exc:
        return False, f"{url}: {exc}"
    if response.status_code >= 400:
        return False, f"{url}: HTTP {response.status_code}: {response.text[:300]}"
    return True, "ok"


def call_ha_service(service: str, data: dict[str, Any]) -> tuple[bool, str]:
    """Call a Home Assistant service, named as domain.service, and return whether it worked and the error text if not."""
    token = ha_token()
    if not token:
        return False, NO_TOKEN
    if "." not in service:
        return False, f"Invalid service name: {service}"
    domain, name = service.split(".", 1)
    return post(token, f"services/{domain}/{name}", data)


def fire_ha_event(event_type: str, data: dict[str, Any]) -> tuple[bool, str]:
    """Fire an event on Home Assistant's event bus and return whether it worked and the error text if not."""
    token = ha_token()
    if not token:
        return False, NO_TOKEN
    return post(token, f"events/{event_type}", data)


def create_persistent_notification(title: str, message: str, notification_id: str) -> bool:
    """Create or replace a persistent notification in Home Assistant, logging a failure; return whether it was sent."""
    ok, detail = call_ha_service(
        "persistent_notification.create",
        {"title": title, "message": message, "notification_id": notification_id},
    )
    if not ok:
        print(f"Persistent notification failed: {detail}", flush=True)
    return bool(ok)


def dismiss_persistent_notification(notification_id: str) -> None:
    """Dismiss a persistent notification in Home Assistant."""
    call_ha_service("persistent_notification.dismiss", {"notification_id": notification_id})


@dataclass(frozen=True)
class WsLimits:
    """The bounds of one WebSocket session; each caller sets its own.

    timeout: seconds for the connection and for each frame. deadline: seconds for the whole session, or None.
    max_frame_bytes: the largest frame read, in UTF-8 bytes, or None. max_frames: frames read while waiting for one
    reply, or None to wait until the reply or the timeout.
    """

    timeout: float = 15
    deadline: float | None = None
    max_frame_bytes: int | None = None
    max_frames: int | None = None


class WsError(Exception):
    """A WebSocket session that ended without every result.

    kind is "auth", "timeout", "too_large", "invalid", "failed" or "no_result"; reply is the frame that ended the
    session, when there is one. Callers turn the kind into their own message, so no reply text leaks by default.
    """

    def __init__(self, kind: str, reply: dict[str, Any] | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.reply = reply


def ws(token: str, messages: list[dict[str, Any]], limits: WsLimits) -> list[Any]:
    """Log in to the WebSocket API, send each message in turn and return the result of each.

    The login is strict: Home Assistant must ask for authentication first and then accept it. The session stops at
    the first failed command. Connection errors are raised as they come; everything else is a WsError.
    """
    deadline = None if limits.deadline is None else time.monotonic() + limits.deadline
    with closing(websocket.create_connection(WS_URL, timeout=limits.timeout)) as connection:

        def receive() -> dict[str, Any]:
            """Read one frame inside the limits."""
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WsError("timeout")
                connection.settimeout(min(limits.timeout, remaining))
            raw = connection.recv()
            size = len(raw.encode() if isinstance(raw, str) else raw)
            if limits.max_frame_bytes is not None and size > limits.max_frame_bytes:
                raise WsError("too_large")
            reply = json.loads(raw)
            if not isinstance(reply, dict):
                raise WsError("invalid")
            return reply

        first = receive()
        if first.get("type") != "auth_required":
            raise WsError("auth", first)
        connection.send(json.dumps({"type": "auth", "access_token": token}))
        answer = receive()
        if answer.get("type") != "auth_ok":
            raise WsError("auth", answer)
        results = []
        for request_id, message in enumerate(messages, 1):
            connection.send(json.dumps({**message, "id": request_id}))
            results.append(_result(receive, request_id, limits.max_frames))
        return results


def _result(receive: Callable[[], dict[str, Any]], request_id: int, max_frames: int | None) -> Any:
    """Read frames until the reply to request_id; return its result or raise WsError."""
    read = 0
    while max_frames is None or read < max_frames:
        reply = receive()
        read += 1
        if reply.get("id") != request_id or reply.get("type") != "result":
            continue
        if not reply.get("success"):
            raise WsError("failed", reply)
        return reply.get("result")
    raise WsError("no_result")
