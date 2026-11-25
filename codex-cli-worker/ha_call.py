"""The worker side of the home_assistant_call tool: one Home Assistant action, sent with the worker's own access.

Codex asks the person before each call (the tool is not approved ahead), then its bridge,
ha_call_mcp.py, passes the request here over a private socket with the thread id that Codex put in
the call. A chat and the ChatGPT app's remote control each have their own socket, so the socket
tells the worker which of the two asks. This module, not the bridge, holds the checks:

- on the chat socket, a request that does not name the thread of a running chat sends nothing;
- on the app's socket (RelayCall), a request while remote control does not run sends nothing;
- a malformed request (a service that is not domain.service, an unknown field) sends nothing;
- a request that names the entity ("name") is sent only when the name fits exactly one entity
  of the service's domain (name_match.py); otherwise it sends nothing and lists the candidates.

After a call whose target is only entity ids, the worker reads up to MAX_READBACK of them back,
so Codex needs no second tool call.

An action that only answers with data (calendar.get_events, weather.get_forecasts) is refused by Home Assistant
with HTTP 400 unless the request asks for the answer. Home Assistant refuses it before the action runs, so the
worker then sends the same call once more with ?return_response and gives Codex the data, cut at
MAX_RESPONSE_BYTES.

It refuses no service by name: the person's answer to Codex's request is the gate.
"""
from __future__ import annotations

import json
import os
import re
import socketserver
import threading
from pathlib import Path

import requests

import name_match
import tool_threads
from relay_reads import ATTRIBUTES, PRIVATE, fetch_states, password_state

SERVICE_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
TARGET_KEYS = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id"})
MAX_REQUEST_BYTES = 65536
MAX_TARGET_IDS = 50
MAX_CHANGED_REPORTED = 50
MAX_READBACK = 5
CALL_TIMEOUT_SECONDS = 60
CORE_SERVICES_URL = "http://supervisor/core/api/services/"
# The data of an action that answers with data is given to Codex up to this size, as serialized JSON.
MAX_RESPONSE_BYTES = 20000
# Home Assistant's text when a call needs ?return_response (homeassistant/components/api/__init__.py). It answers
# this before the action runs, so the call may be sent again with the query.
RESPONSE_REQUIRED = "requires responses but caller did not ask for responses"
# The task name of the ChatGPT app's remote control. A page chat's id never has this form (it starts with a
# date). The app's calls hold while the relay process runs, not for one turn.
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
            raise CallRefused(f"target {key} must be one id or a list of 1 to {MAX_TARGET_IDS} ids")
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
        raise CallRefused("put " + ", ".join(sorted(overlap)) + " in target, not in data")
    return service, {**data, **target}


def needs_response(response):
    """Whether Home Assistant refused the call only because the action answers with data and the request did not ask."""
    return response.status_code == 400 and RESPONSE_REQUIRED in (response.text or "")


def response_data(data):
    """Return the fields that give Codex an action's data: whole when small, else a cut text and a note."""
    serialized = json.dumps(data)
    if len(serialized) <= MAX_RESPONSE_BYTES:
        return {"response": data}
    cut = serialized[:MAX_RESPONSE_BYTES]
    # The cut text travels as a JSON string, whose quotes and backslashes are escaped; keep that within the limit too.
    while len(json.dumps(cut)) > MAX_RESPONSE_BYTES:
        cut = cut[:MAX_RESPONSE_BYTES - (len(json.dumps(cut)) - len(cut))]
    return {"response": cut, "response_cut": True,
            "response_note": (f"The response was {len(serialized)} bytes and was cut at {len(cut)} bytes, so it is "
                              "incomplete and not valid JSON. Ask for less, for example a shorter period.")}


def changed_entities(answer):
    """Return the entity ids of the states Home Assistant reports as changed, at most MAX_CHANGED_REPORTED."""
    if not isinstance(answer, list):
        return []
    ids = [item.get("entity_id") for item in answer if isinstance(item, dict)]
    return [entity for entity in ids if isinstance(entity, str)][:MAX_CHANGED_REPORTED]


class HomeAssistantCall:
    """Answer the chat socket: send a call of a chat's running exchange to Home Assistant."""

    def __init__(self, worker):
        """Keep the worker that provides tasks and the token."""
        self.worker = worker
        self.begun = set()
        self.lock = threading.Lock()

    def begin(self, task_id):
        """Allow calls for the task's running exchange; run_task calls this for every exchange, a scheduled run's included."""
        with self.lock:
            self.begun.add(task_id)

    def end(self, task_id):
        """End the task's calls, so a later request for it sends nothing."""
        with self.lock:
            self.begun.discard(task_id)

    def task_for(self, payload):
        """Return the chat task that may send this request, or None.

        The request names its chat by the thread id that Codex wrote into the call: that chat needs a running
        exchange that is not canceled (begin ran for it and end did not). Who started the exchange, a person or
        a schedule, makes no difference here: Codex's question before the call is the gate, and it waits in the
        chat for a person. No request on this socket is the app's, whatever it holds.
        """
        task_id = tool_threads.CHATS.task_for(payload.get("thread_id"))
        with self.lock:
            begun = task_id is not None and task_id in self.begun
        if not begun:
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
            return {"status": "unavailable", "message": "No running exchange: nothing was sent"}
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
        except (ValueError, requests.RequestException) as exc:  # relay_reads.ReadError is a ValueError
            raise CallRefused(f"Could not read the entity list ({type(exc).__name__}). Nothing was sent.") from None
        if not isinstance(states, list):
            raise CallRefused("Home Assistant returned an invalid entity list. Nothing was sent.")
        try:
            found = name_match.resolve(states, arguments["name"], service)
        except ValueError as exc:
            raise CallRefused(str(exc)) from None
        if "entity_id" in found:
            return {**rest, "target": {"entity_id": found["entity_id"]}}
        count = found["total"]
        if count:
            message = (f"{count} entities fit this name. Nothing was sent: ask the person which one, "
                       "then call again with target.entity_id.")
        else:
            message = f"No {service.split('.', 1)[0]} entity fits this name. Nothing was sent."
        raise NotSent({"status": "not_sent", "message": message, "candidates": found["candidates"],
                       "total_matches": count})

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
                rows.append({"entity_id": entity_id, "error": f"read back failed ({type(exc).__name__})"})
                continue
            if not isinstance(row, dict) or password_state(row):
                continue
            attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            rows.append({"entity_id": entity_id, "state": str(row.get("state"))[:120],
                         "attributes": {key: attributes[key] for key in sorted(ATTRIBUTES & set(attributes))}})
        return rows

    def send(self, service, body):
        """Send one service call to Home Assistant and return the result for Codex.

        The call is sent once. Only when Home Assistant refuses it because the action answers with data and the
        request did not ask for it (it refuses before the action runs) is the same call sent once more, with
        ?return_response.
        """
        token = self.worker.ha_token()
        if not token:
            return {"status": "failed", "message": "Home Assistant authentication is unavailable"}
        domain, name = service.split(".", 1)
        url = CORE_SERVICES_URL + f"{domain}/{name}"
        with_response = False
        try:
            response = requests.post(url, json=body, headers={"Authorization": f"Bearer {token}"},
                                     timeout=CALL_TIMEOUT_SECONDS, allow_redirects=False)
            if needs_response(response):
                with_response = True
                response = requests.post(url + "?return_response", json=body,
                                         headers={"Authorization": f"Bearer {token}"},
                                         timeout=CALL_TIMEOUT_SECONDS, allow_redirects=False)
        except requests.RequestException as exc:
            # The request may have reached Home Assistant; it is not sent again.
            return {"status": "failed", "message": f"No answer from Home Assistant ({type(exc).__name__})"}
        if response.status_code != 200:
            return {"status": "failed", "http_status": response.status_code,
                    "message": response.text[:500] or f"Home Assistant returned HTTP {response.status_code}"}
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not with_response:
            return {"status": "ok", "service": service, "changed": changed_entities(answer)}
        # With ?return_response Home Assistant answers {"changed_states": [...], "service_response": {...}}.
        if isinstance(answer, dict) and ("changed_states" in answer or "service_response" in answer):
            changed, data = changed_entities(answer.get("changed_states")), answer.get("service_response")
        else:
            changed, data = changed_entities(answer), answer
        return {"status": "ok", "service": service, "changed": changed, **response_data(data)}

    def serve(self, path):
        """Start a Unix socket server at path, readable by the worker's user only, that answers call requests.

        A command in a Codex sandbox cannot connect to it (tests/test_sandbox_sockets.py); only a tool bridge
        that Codex starts outside the sandbox, or a command a person or Codex's reviewer let out of it, can.
        """
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
                    result = {"status": "unavailable", "message": "Invalid call request"}
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


class RelayCall(HomeAssistantCall):
    """Answer the ChatGPT app's own socket: a request on it is remote control's, and holds while the relay runs.

    The socket, not a key, says that the request is the app's (D-CX-KEYS): only the relay's bridge gets this
    socket's path (server.relay_env), and a page chat's bridge uses the chat socket. The app has no chat on the
    page; Codex in the relay asks the person in the app before each call.
    """

    def __init__(self, worker, relay_running):
        """Keep the worker, and relay_running, which says whether the remote-control process runs now."""
        super().__init__(worker)
        self.relay_running = relay_running

    def task_for(self, payload):
        """Return RELAY_TASK while the relay runs and is not stopping, else None; the request's thread id is not read."""
        return RELAY_TASK if self.relay_running() else None
