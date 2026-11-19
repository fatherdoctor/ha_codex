"""The worker side of the home_assistant_call tool: one Home Assistant action, sent with the worker's own access.

Codex asks the person before each call (the tool is not approved ahead), then its bridge,
ha_call_mcp.py, passes the request here over a private socket with the key of the running
turn. This module, not the bridge, holds the checks:

- a request without the current key of a running chat sends nothing;
- a malformed request (a service that is not domain.service, an unknown field) sends nothing;
- a request that names the entity ("name") is sent only when the name fits exactly one entity
  of the service's domain (name_match.py); otherwise it sends nothing and lists the candidates.

After a call whose target is only entity ids, the worker reads up to MAX_READBACK of them back,
so Codex needs no second tool call.

It refuses no service by name: the person's answer to Codex's request is the gate.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import threading

import name_match
from relay_reads import ATTRIBUTES, PRIVATE, fetch_states, password_state
import requests
import tool_threads

SERVICE_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
TARGET_KEYS = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id"})
MAX_REQUEST_BYTES = 65536
MAX_TARGET_IDS = 50
MAX_CHANGED_REPORTED = 50
MAX_READBACK = 5
CALL_TIMEOUT_SECONDS = 60
CORE_SERVICES_URL = "http://supervisor/core/api/services/"
# The task name of the ChatGPT app's remote control. A page chat's id never has this form (it starts with a
# date). The relay's key holds while the relay process runs, not for one turn.
RELAY_TASK = "remote-control"


class CallRefused(ValueError):
    """A request the worker will not send to Home Assistant; the message says why, for Codex."""


class NotSent(Exception):
    """A request by name that fits no entity, or more than one: nothing is sent, and the answer lists the candidates."""

    def __init__(self, answer):
        """Keep the answer for Codex; its message is the exception's text."""
        super().__init__(answer["message"])
        self.answer = answer


def parse_target(target):
    """Return the target as a dict of id lists, or raise CallRefused if it holds anything but known id fields."""
    if target is None:
        return {}
    if not isinstance(target, dict):
        raise CallRefused("target must be an object")
    unknown = set(target) - TARGET_KEYS
    if unknown:
        raise CallRefused("target accepts only " + ", ".join(sorted(TARGET_KEYS)))
    parsed = {}
    for key, value in target.items():
        ids = [value] if isinstance(value, str) else value
        if not isinstance(ids, list) or not ids or len(ids) > MAX_TARGET_IDS:
            raise CallRefused(
                f"target {key} must be one id or a list of 1 to {MAX_TARGET_IDS} ids"
            )
        if not all(isinstance(item, str) and item and len(item) <= 255 for item in ids):
            raise CallRefused(f"target {key} must hold text ids")
        parsed[key] = ids if isinstance(value, list) else value
    return parsed


def parse_request(arguments):
    """Check the tool's arguments and return (service, body); raise CallRefused for a request not to send.

    The body is what Home Assistant receives: the data, with the target's id fields beside it.
    """
    if not isinstance(arguments, dict):
        raise CallRefused("expected an object with service, target and data")
    unknown = set(arguments) - {"service", "target", "data"}
    if unknown:
        raise CallRefused("unsupported field: " + ", ".join(sorted(unknown)))
    service = arguments.get("service")
    if not isinstance(service, str) or not SERVICE_RE.fullmatch(service):
        raise CallRefused("service must be domain.service, for example light.turn_off")
    target = parse_target(arguments.get("target"))
    data = arguments.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise CallRefused("data must be an object")
    overlap = TARGET_KEYS & set(data)
    if overlap:
        raise CallRefused(
            "put " + ", ".join(sorted(overlap)) + " in target, not in data"
        )
    return service, {**data, **target}


def changed_entities(answer):
    """Return the entity ids of the states Home Assistant reports as changed, at most MAX_CHANGED_REPORTED."""
    if not isinstance(answer, list):
        return []
    ids = [item.get("entity_id") for item in answer if isinstance(item, dict)]
    return [entity for entity in ids if isinstance(entity, str)][:MAX_CHANGED_REPORTED]


class HomeAssistantCall:
    """Issue each running turn a call key, and send the calls that present it to Home Assistant."""

    def __init__(self, worker, relay_running=lambda: False):
        """Keep the worker that provides tasks and the token.

        relay_running says whether the remote-control process runs now; the relay's key holds only then.
        """
        self.worker = worker
        self.relay_running = relay_running
        self.capabilities = {}
        self.lock = threading.Lock()

    def begin(self, task_id):
        """Allow calls for the task's running exchange, or issue the relay's key for a relay launch, and return it.

        run_task calls this only when a person attends the exchange. For a chat the value stays in the worker;
        only the relay's value goes to a process. An earlier value of the same task stops working.
        """
        capability = secrets.token_urlsafe(32)
        with self.lock:
            self.capabilities[task_id] = capability
        return capability

    def end(self, task_id):
        """End the task's calls, so a later request for it sends nothing."""
        with self.lock:
            self.capabilities.pop(task_id, None)

    def task_for(self, payload):
        """Return the task that may send this request, or None.

        A request with a key comes from remote control: only the relay's key counts, and only while the relay
        runs. A request without a key names its chat by thread id: that chat needs a running exchange that is
        not canceled and that a person attends (begin ran for it). A chat's thread never reaches the relay.
        """
        if "capability" in payload:
            supplied = payload["capability"]
            if not isinstance(supplied, str) or not supplied or not supplied.isascii():
                return None
            with self.lock:
                key = self.capabilities.get(RELAY_TASK)
            if key is None or not hmac.compare_digest(supplied, key):
                return None
            # The app has no chat on the page. Codex in the relay asks the person before each call.
            return RELAY_TASK if self.relay_running() else None
        task_id = tool_threads.CHATS.task_for(payload.get("thread_id"))
        with self.lock:
            attended = (
                task_id is not None
                and task_id != RELAY_TASK
                and task_id in self.capabilities
            )
        if not attended:
            return None
        if self.worker.task_cancellation_requested(task_id):
            return None
        with self.worker.lock:
            if self.worker.tasks.get(task_id, {}).get("status") != "running":
                return None
        return task_id

    def dispatch(self, payload):
        """Answer one request from the bridge: refuse it, or send it and report what Home Assistant said."""
        if not isinstance(payload, dict):
            return {"status": "unavailable", "message": "Invalid call request"}
        task_id = self.task_for(payload)
        if task_id is None:
            return {
                "status": "unavailable",
                "message": "No running exchange that a person attends: nothing was sent",
            }
        arguments = payload.get("arguments")
        try:
            if isinstance(arguments, dict) and "name" in arguments:
                arguments = self.target_by_name(arguments)
            service, body = parse_request(arguments)
        except NotSent as not_sent:
            return not_sent.answer
        except CallRefused as refusal:
            return {"status": "refused", "message": str(refusal)}
        result = self.send(service, body)
        target = arguments.get("target") or {}
        if result["status"] == "ok" and set(target) == {"entity_id"}:
            result["readback"] = self.read_back(target["entity_id"])
        return result

    def target_by_name(self, arguments):
        """Return the arguments with the named entity as the target, or raise NotSent when no one entity fits.

        The service and data are checked first, so a malformed request is refused before Home Assistant is read.
        """
        rest = {key: value for key, value in arguments.items() if key != "name"}
        if "target" in rest:
            raise CallRefused("give a name or a target, not both")
        service, _ = parse_request(rest)
        token = self.worker.ha_token()
        if not token:
            raise CallRefused("Home Assistant authentication is unavailable")
        try:
            states = fetch_states("/states", token)
        except (
            ValueError,
            requests.RequestException,
        ) as exc:  # relay_reads.ReadError is a ValueError
            raise CallRefused(
                f"Could not read the entity list ({type(exc).__name__}). Nothing was sent."
            ) from None
        if not isinstance(states, list):
            raise CallRefused(
                "Home Assistant returned an invalid entity list. Nothing was sent."
            )
        try:
            found = name_match.resolve(states, arguments["name"], service)
        except ValueError as exc:
            raise CallRefused(str(exc)) from None
        if "entity_id" in found:
            return {**rest, "target": {"entity_id": found["entity_id"]}}
        count = found["total"]
        if count:
            message = (
                f"{count} entities fit this name. Nothing was sent: ask the person which one, "
                "then call again with target.entity_id."
            )
        else:
            message = f"No {service.split('.', 1)[0]} entity fits this name. Nothing was sent."
        raise NotSent(
            {
                "status": "not_sent",
                "message": message,
                "candidates": found["candidates"],
                "total_matches": count,
            }
        )

    def read_back(self, entity_ids):
        """Return the new state and the common attributes of up to MAX_READBACK target entities.

        Credential-like and password-mode entities are left out, as they are for every read.
        """
        token = self.worker.ha_token()
        ids = [entity_ids] if isinstance(entity_ids, str) else entity_ids
        rows = []
        for entity_id in ids[:MAX_READBACK]:
            if PRIVATE.search(entity_id.split(".", 1)[-1]):
                continue
            try:
                row = fetch_states(f"/states/{entity_id}", token)
            except (ValueError, requests.RequestException) as exc:
                rows.append(
                    {
                        "entity_id": entity_id,
                        "error": f"read back failed ({type(exc).__name__})",
                    }
                )
                continue
            if not isinstance(row, dict) or password_state(row):
                continue
            attributes = (
                row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            )
            rows.append(
                {
                    "entity_id": entity_id,
                    "state": str(row.get("state"))[:120],
                    "attributes": {
                        key: attributes[key]
                        for key in sorted(ATTRIBUTES & set(attributes))
                    },
                }
            )
        return rows

    def send(self, service, body):
        """Send one service call to Home Assistant, once, and return the result for Codex."""
        token = self.worker.ha_token()
        if not token:
            return {
                "status": "failed",
                "message": "Home Assistant authentication is unavailable",
            }
        domain, name = service.split(".", 1)
        try:
            response = requests.post(
                CORE_SERVICES_URL + f"{domain}/{name}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=CALL_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # The request may have reached Home Assistant; it is not sent again.
            return {
                "status": "failed",
                "message": f"No answer from Home Assistant ({type(exc).__name__})",
            }
        if response.status_code != 200:
            return {
                "status": "failed",
                "http_status": response.status_code,
                "message": response.text[:500]
                or f"Home Assistant returned HTTP {response.status_code}",
            }
        try:
            changed = changed_entities(response.json())
        except ValueError:
            changed = []
        return {"status": "ok", "service": service, "changed": changed}

    def serve(self, path):
        """Start a Unix socket server at path, readable by the worker's user only, that answers call requests."""
        engine = self

        class Handler(socketserver.StreamRequestHandler):
            """Answer one newline-terminated JSON request per connection."""

            def handle(self):
                """Read one bounded request, dispatch it and write the result as one JSON line."""
                self.request.settimeout(CALL_TIMEOUT_SECONDS + 10)
                try:
                    raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
                    if len(raw) > MAX_REQUEST_BYTES:
                        raise ValueError("Request too large")
                    result = engine.dispatch(json.loads(raw))
                except (OSError, ValueError):
                    result = {
                        "status": "unavailable",
                        "message": "Invalid call request",
                    }
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass  # The bridge may have gone with a cancelled turn.

        Path(path).unlink(missing_ok=True)
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        server.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
