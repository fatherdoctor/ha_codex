"""Bounded Home Assistant checks, owned by the worker, not the model."""
from __future__ import annotations

import json
import os
import re
import secrets
import socketserver
import threading
import time
from contextlib import AbstractContextManager, closing
from pathlib import Path
from typing import Any, Protocol

import requests
import websocket

from relay_reads import PRIVATE, ReadError, fetch_states, password_state
import tool_threads

MAX_CHECKS = 24
MAX_FIND_MATCHES = 10
ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
DOMAIN_RE = re.compile(r"[a-z_][a-z0-9_]*")
PATH_RE = re.compile(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)?/?")


class VerificationWorker(Protocol):
    """What this module uses of the worker: the tasks, the options, and the worker's access to Home Assistant.

    server.py passes an object that returns its own module globals by name, and the tests pass the server module.
    Each name that the code below reads from `self.worker` is listed here, with the type that server.py gives it,
    so that a reader and a type checker see the whole dependency. tests/test_verification.py fails when the code
    reads a name that is not in this list, or when server.py does not have a listed name.
    """

    CONFIG_ROOT: Path
    # threading.RLock; the worker's one lock for `tasks`.
    lock: AbstractContextManager[Any]
    tasks: dict[str, dict[str, Any]]

    def api_token(self) -> str: ...
    def build_manifest(self) -> dict[str, dict[str, Any]]: ...
    def check_home_assistant_config(self) -> dict[str, str]: ...
    def diff_manifests(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]: ...
    def find_lovelace_dashboard_refs(self, changes: dict[str, list[str]]) -> list[dict[str, str]]: ...
    def get_run_dir(self, task_id: str) -> Path: ...
    def get_task_dir(self, task_id: str) -> Path: ...
    def ha_token(self) -> str: ...
    def redact(self, text: str) -> str: ...
    def save_lovelace_dashboard(self, ref: dict[str, str]) -> tuple[bool, str]: ...
    def task_cancellation_requested(self, task_id: str) -> bool: ...
    def task_options(self, task_id: str) -> dict[str, Any]: ...
    def task_turns(self, task: dict[str, Any]) -> list[dict[str, Any]]: ...
    def update_task(self, task_id: str, **updates: Any) -> bool: ...
    def utc_now(self) -> str: ...
    def validate_changed_files(self, changes: dict[str, list[str]]) -> list[str]: ...


def public_check(entry: Any) -> Any:
    """Return a check without exact blocked-request details, saved scopes or grants.

    Only dashboard captures of versions before 0.4.0 have these fields, and saved chats keep them. Request
    parameters can carry values the denylists don't recognize, so the agent, Home Assistant events and
    token-authenticated API callers get each request's label only.
    """
    if not isinstance(entry, dict):
        return entry
    result = {key: value for key, value in entry.items() if key not in {"permission_scope", "permission_grants"}}

    def labels(items: Any) -> list[dict[str, Any]]:
        """Keep only the readable label of each described request."""
        return [{"label": item.get("label", "")} for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    if "permission_candidates" in entry:
        result["permission_candidates"] = [{"id": item.get("id"), "kind": item.get("kind"), "label": item.get("label", "")}
                                           for item in entry["permission_candidates"] or [] if isinstance(item, dict)]
    for key in ("permission_review", "permission_decision"):
        if isinstance(entry.get(key), dict):
            result[key] = {**entry[key], **{name: labels(entry[key][name]) for name in ("approved", "carried") if name in entry[key]}}
    return result


def dashboard_path(payload: dict[str, Any]) -> str:
    """Return the payload's dashboard path, /lovelace/0 when it names none; raise ValueError when it is not local."""
    path = payload.get("path", "/lovelace/0")
    if not isinstance(path, str) or not PATH_RE.fullmatch(path):
        raise ValueError("Use a local dashboard path such as /lovelace/0")
    return path


def find_row(row: dict[str, Any], words: list[str], domain: str | None) -> tuple[int, dict[str, str]] | None:
    """Return (other words, answer row) for one item of Core's state list, or None when it does not match or is hidden.

    A match holds every word across its friendly name and entity id. Rank by the fewest extra words in either
    field that holds the whole query. If the words are split across both fields, rank by their combined words.
    The answer row carries only the entity id, friendly name and state.
    """
    entity_id = row.get("entity_id")
    state = row.get("state")
    if not isinstance(entity_id, str) or not ENTITY_RE.fullmatch(entity_id) or not isinstance(state, str):
        return None
    entity_domain, object_id = entity_id.split(".", 1)
    if domain is not None and entity_domain != domain:
        return None
    if PRIVATE.search(object_id) or password_state(row):
        return None
    attributes = row.get("attributes")
    friendly = attributes.get("friendly_name") if isinstance(attributes, dict) else None
    name = friendly if isinstance(friendly, str) else ""
    fields = (name.lower(), object_id.replace("_", " "))
    matching_fields = [field for field in fields if all(word in field for word in words)]
    if not matching_fields:
        combined = " ".join(fields)
        if not all(word in combined for word in words):
            return None
        matching_fields = [combined]
    query_words = set(words)
    other_words = min(len(set(re.findall(r"[a-z0-9]+", field)) - query_words) for field in matching_fields)
    return other_words, {"entity_id": entity_id, "name": name[:120], "state": state[:120]}


class Verification:
    """Run the bounded checks a running task may request with its capability, and record them on its current turn."""

    def __init__(self, worker: VerificationWorker) -> None:
        """Keep the worker that provides tasks, options and Home Assistant access; no capability is issued yet."""
        self.worker = worker
        # One private exchange id per running task; the manual check from the page still sends one. A chat's tool
        # request names its thread instead (tool_threads.py); the name and the "capability expired" answer are
        # kept from earlier versions.
        self.capabilities: dict[str, str] = {}
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()

    def begin(self, task_id: str) -> str:
        """Start the exchange's checks: a new exchange id, no recorded checks, and no expired screenshots.

        The screenshots are those of versions before 0.4.0 (cleanup).

        The exchange id stays in the worker. A request names its chat by thread id (tool_threads).
        """
        self.cleanup()
        with self.lock:
            capability = secrets.token_urlsafe(32)
            self.capabilities[task_id] = capability
        self.worker.update_task(task_id, verification=[], verification_attachments=[])
        return capability

    def end(self, task_id: str) -> None:
        """End the exchange's checks, so later requests for its chat are refused."""
        with self.lock:
            self.capabilities.pop(task_id, None)

    def dispatch(self, payload: Any) -> dict[str, Any]:
        """Run a request's check for the chat that its thread id names; raise ValueError if no exchange runs there."""
        if not isinstance(payload, dict):
            raise ValueError("Expected a verification object")
        task_id = tool_threads.CHATS.task_for(payload.get("thread_id"))
        with self.lock:
            supplied = self.capabilities.get(task_id) if task_id else None
        if not supplied or self.worker.task_cancellation_requested(task_id):
            raise ValueError("No running exchange for this chat")
        with self.worker.lock:
            if self.worker.tasks.get(task_id, {}).get("status") != "running":
                raise ValueError("No running exchange for this chat")
        return self.run(task_id, {k: v for k, v in payload.items() if k != "thread_id"}, supplied)

    def active(self, task_id: str, turn_id: str | None, capability: str | None) -> bool:
        """Reject checks that outlive cancellation, completion, or a replaced turn."""
        with self.lock:
            current = self.capabilities.get(task_id)
        with self.worker.lock:
            task = self.worker.tasks.get(task_id, {})
            authorized = current == capability and task.get("status") == "running"
            return bool(capability and task and authorized and task.get("current_turn_id") == turn_id
                        and not self.worker.task_cancellation_requested(task_id))

    def core(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call a Core API path through the Supervisor proxy and return the JSON of its answer.

        Requests to the integration's own codex_cli/ paths also carry the worker token in their JSON body.
        Raises ValueError when no token is available, the answer is not HTTP 200, or its body exceeds 256 KiB.
        """
        token = self.worker.ha_token()
        if not token:
            raise ValueError("Home Assistant authentication is unavailable")
        headers = {"Authorization": f"Bearer {token}"}
        if path.startswith("codex_cli/"):
            kwargs["json"] = {**kwargs.get("json", {}), "worker_token": self.worker.api_token()}
        # The automatic token belongs to the Supervisor proxy, never a supplied URL.
        url = "http://supervisor/core/api/" + path
        with requests.request(method, url, headers=headers, timeout=20,
                              allow_redirects=False, stream=True, **kwargs) as response:
            if response.status_code != 200:
                raise ValueError(f"Home Assistant returned HTTP {response.status_code}")
            content = bytearray()
            for chunk in response.iter_content(8192):
                content.extend(chunk)
                if len(content) > 256 * 1024:
                    raise ValueError("Home Assistant response exceeds the diagnostic limit")
        return json.loads(content)

    def ws_read(self, message: dict[str, Any]) -> Any:
        """Send one command over the Core WebSocket and return its result.

        Raises ValueError when authentication fails, a reply exceeds 1 MiB, or no successful result arrives.
        """
        token = self.worker.ha_token()
        with closing(websocket.create_connection("ws://supervisor/core/websocket", timeout=15)) as ws:
            first = json.loads(ws.recv())
            if first.get("type") == "auth_required":
                ws.send(json.dumps({"type": "auth", "access_token": token}))
                first = json.loads(ws.recv())
            if first.get("type") != "auth_ok":
                raise ValueError("Home Assistant WebSocket authentication failed")
            ws.send(json.dumps({"id": 1, **message}))
            for _ in range(20):
                raw = ws.recv()
                if len(raw) > 1024 * 1024:
                    raise ValueError("Dashboard response exceeds the verification limit")
                result = json.loads(raw)
                if result.get("id") == 1:
                    if not result.get("success"):
                        raise ValueError("Home Assistant could not read this dashboard")
                    return result["result"]
        raise ValueError("Home Assistant did not return a result")

    def run(self, task_id: str, payload: dict[str, Any], capability: str | None = None) -> dict[str, Any]:
        """Run one check for the task's current turn and add its redacted result to the turn's verification list.

        Returns an "unavailable" result instead of raising when another check is running, the capability
        has expired, the turn's check limit is reached, or the operation fails.
        """
        # One inspection at a time, even if the agent launches parallel commands.
        if not self.execution_lock.acquire(blocking=False):
            return {"status": "unavailable", "message": "Another verification is running"}
        try:
            with self.lock:
                capability = capability or self.capabilities.get(task_id)
            with self.worker.lock:
                task = self.worker.tasks.get(task_id, {})
                turn_id = task.get("current_turn_id")
                entries = list(task.get("verification") or [])
            if not self.active(task_id, turn_id, capability):
                return {"status": "unavailable", "message": "Verification capability expired"}
            if len(entries) >= MAX_CHECKS:
                return {"status": "unavailable", "message": "Verification limit reached for this turn"}
            operation = payload.get("operation")
            entry = {"operation": str(operation)[:40], "checked_at": self.worker.utc_now()}
            try:
                if operation == "entity":
                    entry.update(self.entity(payload))
                elif operation == "find":
                    entry.update(self.find(payload))
                elif operation == "config_check":
                    entry.update(self.worker.check_home_assistant_config())
                    entry["status"] = {"valid": "passed", "invalid": "failed"}.get(entry["result"], "unavailable")
                elif operation == "logs":
                    target = payload.get("target", "core")
                    if target != "core":
                        raise ValueError("Only Core logs are available to verification")
                    result = self.core("POST", "codex_cli/diagnostic_logs", json={"target": target})
                    entry.update(status="observed", target=target, message=result["text"], truncated=result.get("truncated", False))
                elif operation == "dashboard_readback":
                    # Check the path before anything is saved, so a bad path saves nothing.
                    path = dashboard_path(payload)
                    if payload.get("save_pending") is True:
                        self.save_pending_dashboard(task_id, path)
                    entry.update(self.dashboard_readback(path, payload.get("expected_config")))
                else:
                    raise ValueError("Supported operations: entity, find, config_check, logs, dashboard_readback")
            except Exception as exc:
                # Do not persist network exception URLs, tokens, or response bodies.
                detail = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
                entry.update(status="unavailable", message=f"Verification could not finish: {detail[:200]}")
            entry = json.loads(self.worker.redact(json.dumps(entry)))
            with self.worker.lock:
                if self.active(task_id, turn_id, capability):
                    self.worker.update_task(task_id, verification=[*entries, entry])
                else:
                    return {"status": "unavailable", "message": "Verification capability expired"}
            return entry
        finally:
            self.execution_lock.release()

    def entity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read an entity's state from Core, compare it with any expected_state, and keep only the named attributes."""
        entity_id = payload.get("entity_id", "")
        if not isinstance(entity_id, str) or not ENTITY_RE.fullmatch(entity_id):
            raise ValueError("Invalid entity_id")
        expected = payload.get("expected_state")
        if expected is not None and (not isinstance(expected, str) or len(expected) > 255):
            raise ValueError("expected_state must be a short string")
        # Only explicitly requested attributes are retained. No blanket state dump.
        keys = payload.get("attributes", [])
        if not isinstance(keys, list) or len(keys) > 10 or any(not isinstance(k, str) or len(k) > 80 for k in keys):
            raise ValueError("Request at most ten named attributes")
        state = self.core("GET", f"states/{entity_id}")
        attributes = {key: state.get("attributes", {}).get(key) for key in keys}
        if len(json.dumps(attributes)) > 8000:
            raise ValueError("Requested attributes exceed the diagnostic limit")
        return {"status": "observed" if expected is None else "passed" if state.get("state") == expected else "failed",
                "entity_id": entity_id, "state": state.get("state"), "expected_state": expected,
                "attributes": attributes, "last_updated": state.get("last_updated"),
                "message": "Fresh entity state readback. This does not verify automation triggers, conditions, or actions."}

    def find(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Find entities by the name a person uses for them, so a device command needs no search of the registry files.

        An entity matches when its friendly name or entity id holds every word of the name. The answer is at
        most MAX_FIND_MATCHES rows of entity id, friendly name and state, the closest name first.
        Credential-like and password-mode entities are left out, as they are for relay reads.
        """
        name = payload.get("name")
        if not isinstance(name, str) or len(name) > 120:
            raise ValueError("name must be a short string")
        words = re.findall(r"[a-z0-9]+", name.lower())
        if not words:
            raise ValueError("name must hold a letter or a digit")
        domain = payload.get("domain")
        if domain is not None and (not isinstance(domain, str) or len(domain) > 64 or not DOMAIN_RE.fullmatch(domain)):
            raise ValueError("Invalid domain")
        token = self.worker.ha_token()
        if not token:
            raise ValueError("Home Assistant authentication is unavailable")
        try:
            states = fetch_states("/states", token)
        except ReadError as exc:
            raise ValueError(str(exc)) from None
        if not isinstance(states, list):
            raise ValueError("Home Assistant returned an invalid entity list")
        found = [find_row(row, words, domain) for row in states if isinstance(row, dict)]
        # The closest name first: the device itself before its extra sensors, buttons and settings.
        ranked = sorted((pair for pair in found if pair), key=lambda pair: (pair[0], pair[1]["entity_id"]))
        matches = [row for _, row in ranked]
        shown = matches[:MAX_FIND_MATCHES]
        message = ("No entity matches this name. Try fewer words or another domain." if not matches
                   else "Entities whose name or entity id holds every word. Use the exact entity_id for the action.")
        return {"status": "observed", "name": name, "domain": domain, "matches": shown,
                "total_matches": len(matches), "truncated": len(matches) > len(shown), "message": message}

    def dashboard_readback(self, path: str, expected: Any) -> dict[str, Any]:
        """Read the dashboard at a checked path from Core and compare it with expected when one is given."""
        dashboard = path.strip("/").split("/")[0]
        message = {"type": "lovelace/config"}
        if dashboard != "lovelace":
            message["url_path"] = dashboard
        actual = self.ws_read(message)
        return {"status": "observed" if expected is None else "passed" if actual == expected else "failed",
                "path": path, "message": "Fresh dashboard configuration readback" +
                (" matches the saved configuration." if expected is not None and actual == expected else
                 " differs from the saved configuration." if expected is not None else "."),
                "view_count": len(actual.get("views", []))}

    def require_dashboard_save_permission(self, task_id: str) -> None:
        """Worker writes obey the same narrowed mode as the current chat turn."""
        options = self.worker.task_options(task_id)
        if options.get("codex_sandbox") == "read-only":
            raise ValueError("Dashboard saves are disabled in read-only mode")
        if not options.get("auto_save_lovelace", True):
            raise ValueError("Dashboard auto-save is disabled; read the loaded dashboard back without save_pending")

    def save_pending_dashboard(self, task_id: str, path: str) -> None:
        """Save only a dashboard edited in this turn, so the readback that follows reads the edit."""
        self.require_dashboard_save_permission(task_id)
        manifest = self.worker.get_run_dir(task_id) / "manifest-before.json"
        if not manifest.is_file():
            raise ValueError("The turn has no baseline for a dashboard save")
        changes = self.worker.diff_manifests(json.loads(manifest.read_text()), self.worker.build_manifest())
        dashboard = path.strip("/").split("/")[0]
        refs = [ref for ref in self.worker.find_lovelace_dashboard_refs(changes)
                if (ref.get("url_path") or "lovelace") == dashboard]
        if not refs:
            return  # No pending edit for this dashboard; read the current Core state.
        for ref in refs:
            errors = self.worker.validate_changed_files({"added": [], "changed": [ref["storage_file"]], "deleted": []})
            if errors:
                raise ValueError("The edited dashboard failed JSON validation")
            self.require_dashboard_save_permission(task_id)
            ok, _ = self.worker.save_lovelace_dashboard(ref)
            if not ok:
                raise ValueError("Home Assistant did not accept the pending dashboard")
            config = json.loads((self.worker.CONFIG_ROOT / ref["storage_file"]).read_text())["data"]["config"]
            check = self.dashboard_readback(path, config)
            if check["status"] != "passed":
                raise ValueError("The pending dashboard was accepted but API readback differs")

    def cleanup(self) -> None:
        """Delete expired dashboard screenshots that versions before 0.4.0 kept, from known conversation metadata.

        Version 0.4.0 takes no screenshots, and the last ones expire seven days after the update.
        Remove this method after version 0.5.
        """
        with self.worker.lock:
            tasks = list(self.worker.tasks.values())
        for task in tasks:
            root = self.worker.get_task_dir(task["task_id"]).resolve()
            for turn in self.worker.task_turns(task):
                for item in (turn.get("verification_attachments") or []):
                    if item.get("origin") != "verification" or item.get("expires_at", 0) > time.time():
                        continue
                    path = (root / item.get("path", "")).resolve()
                    if path.is_relative_to(root) and path.is_file():
                        path.unlink(missing_ok=True)

    def serve(self, path: str | Path) -> socketserver.ThreadingUnixStreamServer:
        """Start a Unix socket server at path that answers verification requests in background threads, and return it.

        Any existing socket file is replaced, and the new one is accessible to its owner only.
        """
        engine = self

        class Handler(socketserver.StreamRequestHandler):
            """Answer one newline-terminated JSON request per connection."""

            def handle(self) -> None:
                """Read one request of at most 16384 bytes, dispatch it and write the result as a JSON line.

                A request that is invalid, too large or refused gets a generic "unavailable" result.
                """
                self.request.settimeout(240)
                try:
                    raw = self.rfile.readline(16385)
                    if len(raw) > 16384:
                        raise ValueError("Request too large")
                    result = engine.dispatch(json.loads(raw))
                except (OSError, ValueError):
                    result = {"status": "unavailable", "message": "Invalid or expired verification request"}
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass  # A cancelled caller may have closed its pipe/socket.

        Path(path).unlink(missing_ok=True)
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        server.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
