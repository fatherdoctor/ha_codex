"""Bridge Codex app-server turns to explicit, single-action user approvals.

The worker owns the process and authentication. This adapter owns JSON-RPC
correlation and live requests; only redacted display records leave it. Approval
responses never install command rules or grant access for a session.
"""

from __future__ import annotations

import copy
import json
import posixpath
import re
import threading
import time
import uuid
from typing import Any, Callable, TextIO


APPROVAL_GONE = "This permission request is no longer waiting for an answer."
DISPLAY_LIMIT = 32000
REQUEST_LIMIT = 50
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
# The app's permission policy, its house rules and Codex's own configuration: a change there is never allowed here.
PROTECTED_PREFIXES = ("/usr/share/ha_codex", "/data/codex-home/AGENTS.md", "/data/codex-home/config.toml",
                      "/data/options.json", "/data/worker_api_token")


def touches_protected_path(path: str) -> bool:
    """Return whether a file change path is, or lies inside, a protected policy location."""
    normalized = posixpath.normpath(posixpath.join("/config", str(path)))
    return any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)


# The same places as a command spells them: quotes, backslashes and doubled slashes are removed first, and the
# names are matched loosely (a glob such as /usr/share/ha_c* still names the image folder). Best effort on the
# text, like the file-path check: it cannot see a path a command builds at run time.
PROTECTED_COMMAND_RES = (
    re.compile(r"/share/+ha_", re.IGNORECASE),
    re.compile(r"ha_codex/+(policy|agents|house)", re.IGNORECASE),
    re.compile(r"codex-home/+[^\s/]*[*?\[]", re.IGNORECASE),
    re.compile(r"codex-home.*(agents\.md|config\.toml)|(agents\.md|config\.toml).*codex-home", re.IGNORECASE | re.DOTALL),
    re.compile(r"/data/+options\.json|worker_api_token", re.IGNORECASE),
)


def command_names_protected_path(command: str) -> bool:
    """Return whether a command's text names a protected policy location by any plain spelling."""
    text = re.sub(r"[\'\"\\]", "", str(command or ""))
    text = re.sub(r"/{2,}", "/", text)
    return any(pattern.search(text) for pattern in PROTECTED_COMMAND_RES)


FILE_APPROVAL = "item/fileChange/requestApproval"


_SERVICE_PATH = re.compile(r"/api/services/([a-z0-9_]+)/([a-z0-9_]+)")

_SERVICE_WORDS = {"turn_on": "turns on", "turn_off": "turns off", "toggle": "toggles"}

_RELOADS = {
    ("automation", "reload"): ("Reload the automations.", "Automations read their configuration again; none is run by this step."),
    ("script", "reload"): ("Reload the scripts.", "Scripts read their configuration again; none is run by this step."),
    ("scene", "reload"): ("Reload the scenes.", "Scenes read their configuration again; none is applied by this step."),
    ("homeassistant", "restart"): ("Restart Home Assistant.", "Home Assistant is unavailable for a minute or two."),
}

_FILE_EFFECTS = (
    (re.compile(r"(^|/)automations\.yaml$"), "Your automations change once automations are reloaded."),
    (re.compile(r"(^|/)scripts\.yaml$"), "Your scripts change once scripts are reloaded."),
    (re.compile(r"(^|/)scenes\.yaml$"), "Your scenes change once scenes are reloaded."),
    (re.compile(r"(^|/)\.storage/lovelace"), "A dashboard changes; open it to see the change."),
    (re.compile(r"(^|/)(configuration\.yaml|packages/.+\.ya?ml)$"),
     "Home Assistant's configuration changes; most parts take effect after a reload or a restart."),
    (re.compile(r"\.ya?ml$"), "A configuration file changes; it takes effect when Home Assistant next reads it."),
)

_KIND_WORDS = {"add": "Create", "update": "Change", "delete": "Delete"}

_ENTITY_ID = re.compile(r"(?<![\w.-])[a-z_][a-z0-9_]*\.[a-z0-9_]+\b")


def _entity_ids(text: str) -> list[str]:
    """Return the entity ids a command names, in order, without repeats."""
    return list(dict.fromkeys(_ENTITY_ID.findall(text)))

def _command_confirmation(command: str, cwd: str, network: str) -> dict[str, str]:
    """Plain words for a command: a Home Assistant action when one is named, else the command itself."""
    lower = (command or "").lower()
    call = _SERVICE_PATH.search(lower)
    if call:
        domain, service = call.groups()
        if (domain, service) in _RELOADS:
            what, effect = _RELOADS[(domain, service)]
            return {"what": what, "where": "This Home Assistant.", "effect": effect}
        targets = [entity for entity in _entity_ids(lower) if entity.split(".")[0] == domain] or _entity_ids(lower)
        where = ", ".join(targets) if targets else f"every {domain} the action reaches"
        verb = _SERVICE_WORDS.get(service, f"runs {domain}.{service} on")
        if domain == "scene" and service == "turn_on":
            verb = "applies"
        return {"what": f"Run the Home Assistant action {domain}.{service}.", "where": (where if targets else where.capitalize()) + ".",
                "effect": f"This {verb} {where}."}
    where = f"In {cwd}" + (f", reaching {network}" if network else "") + "." if cwd else "On this Home Assistant."
    return {"what": "Run one command outside the sandbox, shown below.", "where": where,
            "effect": "Only what that command does; it may read files and use the network."}

def _file_effect(paths: list[str]) -> str:
    """The plain effect of changing these files: the most specific one that applies to each, without repeats."""
    effects: list[str] = []
    for path in paths:
        effect = next((text for pattern, text in _FILE_EFFECTS if pattern.search(path)), "The file changes.")
        if effect not in effects:
            effects.append(effect)
    return " ".join(effects)

def _changes_confirmation(changes: list[dict[str, Any]]) -> dict[str, str]:
    """Plain words for a file change: create, change or delete, which files, and what that does."""
    present = {str(change.get("kind") or "update") for change in changes}
    verbs = " and ".join(word.lower() for kind, word in _KIND_WORDS.items() if kind in present) or "change"
    paths = list(dict.fromkeys(str(change[key]) for change in changes for key in ("path", "move_path") if change.get(key)))
    count = len(changes)
    what = f"{verbs[0].upper() + verbs[1:]} {count} file{'s' if count != 1 else ''}."
    return {"what": what, "where": (", ".join(paths) + ".") if paths else "Not given.", "effect": _file_effect(paths)}


def confirmation(record: dict[str, Any]) -> dict[str, str]:
    """Describe the redacted action without making or suppressing its approval decision."""
    text = (_command_confirmation(record["command"], record["cwd"], record.get("network", ""))
            if record["kind"] == "command" else _changes_confirmation(record["changes"]))
    if not record["can_allow"]:
        text["effect"] = "This request cannot be allowed here. " + record["note"]
    return text


class AppServerSession:
    """Run one app-server turn and correlate each user decision with its request.

    All state and writes use the worker's reentrant lock, including cancellation
    and the HTTP decision route. A browser cannot supply protocol request IDs,
    commands, permission scopes, or policy amendments.
    """

    def __init__(
        self, writer: TextIO, *, lock: Any, thread_params: dict[str, Any],
        turn_params: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
        on_approvals: Callable[[list[dict[str, Any]]], None],
        on_final: Callable[[str], None], is_active: Callable[[], bool],
        redact: Callable[[str], str],
    ) -> None:
        """Bind a live transport to one worker exchange and its persistence callbacks."""
        self.writer = writer
        self.lock = lock
        self.thread_params = copy.deepcopy(thread_params)
        self.turn_params = copy.deepcopy(turn_params)
        self.on_event = on_event
        self.on_approvals = on_approvals
        self.on_final = on_final
        self.is_active = is_active
        self.redact = redact
        self.finished = threading.Event()
        # When Codex last sent anything, or the user last answered; the worker's stall watchdog reads it.
        self.last_activity = time.monotonic()
        self.error = ""
        self.returncode = 1
        self.thread_id = ""
        self.turn_id = ""
        self._closed = False
        self._turn_ended = False
        self._next_id = 0
        self._requests: dict[str, str] = {}
        self._pending: dict[str, Any] = {}
        self._pending_files: dict[str, str] = {}
        self._records: list[dict[str, Any]] = []
        self._file_changes: dict[str, list[dict[str, Any]]] = {}
        self._final_text = ""

    def _write(self, message: dict[str, Any]) -> None:
        """Write one complete JSON-RPC message while the shared lock is held."""
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def _request(self, method: str, params: dict[str, Any]) -> None:
        """Send a client request with a namespace distinct from server request IDs."""
        self._next_id += 1
        request_id = f"worker-{self._next_id}"
        self._requests[request_id] = method
        self._write({"id": request_id, "method": method, "params": params})

    def start(self) -> None:
        """Initialize the protocol; thread and turn creation follow their replies."""
        with self.lock:
            if self._closed or not self.is_active():
                self.close()
                return
            self._request("initialize", {"clientInfo": {
                "name": "home_assistant_worker", "title": "Home Assistant", "version": "1",
            }})

    def read(self, handle: TextIO) -> None:
        """Read the JSONL transport until completion or EOF, failing closed on errors."""
        try:
            for line in iter(handle.readline, ""):
                self.receive(json.loads(line))
                if self.finished.is_set():
                    break
            if not self.finished.is_set():
                self.fail("Codex closed the permission connection before the exchange finished.")
        except Exception as exc:
            self.fail(f"Codex permission connection failed: {exc}")

    def fail(self, message: str) -> None:
        """Record a redacted transport error and invalidate every outstanding request."""
        with self.lock:
            self.error = self.redact(message)
            try:
                self.close()
            except OSError:
                # A failed audit write must not keep the transport alive.
                pass

    def close(self) -> None:
        """Expire all live requests and close stdin; saved records cannot grant access."""
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.thread_id and self.turn_id and not self._turn_ended:
                try:
                    self._write({"id": "worker-stop", "method": "turn/interrupt", "params": {
                        "threadId": self.thread_id, "turnId": self.turn_id,
                    }})
                except (OSError, ValueError):
                    pass
            self._pending.clear()
            self._pending_files.clear()
            for record in self._records:
                if record["status"] == "pending":
                    record["status"] = "expired"
            try:
                if self._records:
                    self.on_approvals(copy.deepcopy(self._records))
            finally:
                self.finished.set()
                try:
                    self.writer.close()
                except (OSError, ValueError):
                    pass

    def _display(self, value: Any) -> tuple[str, bool]:
        """Redact display text and signal when review would hide part of an action."""
        raw = str(value or "")
        text = self.redact(raw)
        return text[:DISPLAY_LIMIT], text != raw or len(text) > DISPLAY_LIMIT

    def _publish_approval(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """Expose only a request for the active thread and turn, with an opaque UI ID."""
        if params.get("threadId") != self.thread_id or params.get("turnId") != self.turn_id:
            raise ValueError("permission request does not belong to the active exchange")
        if len(self._records) >= REQUEST_LIMIT:
            self._write({"id": request_id, "result": {"decision": "decline"}})
            self.on_event({"type": "error", "message": "Too many permission requests in this exchange; request denied."})
            return
        if request_id in self._pending.values():
            raise ValueError("duplicate live permission request")
        reason, _ = self._display(params.get("reason"))
        clipped = False
        # Anything but a new command, such as input typed into a running one, is shown but never allowed.
        request_kind = params.get("kind", "command") if method == COMMAND_APPROVAL else ""
        record: dict[str, Any] = {
            "approval_id": uuid.uuid4().hex, "status": "pending", "reason": reason,
            "kind": "command" if method == COMMAND_APPROVAL else "file_change",
            # The counter's kind: a new command, input to a running one, another command request, or a file change.
            "prompt_kind": ("file_change" if method != COMMAND_APPROVAL else "command" if request_kind == "command"
                            else "command_input" if request_kind == "writeStdin" else "other"),
            "can_allow": True, "command": "", "cwd": "", "changes": [],
        }
        if method == COMMAND_APPROVAL:
            record["command"], truncated = self._display(params.get("command"))
            record["cwd"], folder_truncated = self._display(params.get("cwd"))
            clipped |= truncated or folder_truncated
            record["scope"] = (
                "Allow this command once outside the configured sandbox. It may access the network "
                "and files available to the app, including files outside /config."
            )
            if request_kind == "writeStdin":
                record["scope"] = "Send this input to a command that is already running outside the configured sandbox."
            record["can_allow"] = bool(record["command"] and record["cwd"]) and request_kind == "command"
            network = params.get("networkApprovalContext")
            if isinstance(network, dict):
                record["network"], _ = self._display(network.get("host"))
        else:
            record["changes"] = copy.deepcopy(self._file_changes.get(str(params.get("itemId")), []))
            record["scope"] = "Allow these file changes once, including any listed paths outside /config."
            record["can_allow"] = bool(record["changes"]) and all(
                change.get("path") and change.get("diff") and change.get("kind") in {"add", "update", "delete"}
                for change in record["changes"])
            clipped |= any(change.get("truncated") or change.get("redacted") for change in record["changes"])
        if method == COMMAND_APPROVAL:
            # A command allowed once runs outside the sandbox, so it is held to the same protected places.
            protected = command_names_protected_path(params.get("command") or "")
        else:
            protected = any(
                touches_protected_path(change.get("path") or "") or touches_protected_path(change.get("move_path") or "")
                for change in record["changes"])
        if clipped or protected:
            record["can_allow"] = False
        if protected:
            record["note"] = "This request touches the app's permission policy or settings, which Codex may not edit. Deny it."
        elif request_kind == "writeStdin":
            record["note"] = "Input to a running command cannot be allowed here. Deny it and ask for the whole action as one command."
        elif request_kind not in ("", "command"):
            record["note"] = "This kind of request cannot be allowed here. Deny it and ask for a single command or file change."
        elif not record["can_allow"]:
            record["note"] = "The complete action is missing, too large, or redacted. Deny it and request an action without hidden details."
        else:
            record["note"] = "Known credentials are hidden. This does not change your sandbox setting or approve future requests."
        record["confirmation"] = confirmation(record)
        self._records.append(record)
        self._pending[record["approval_id"]] = request_id
        if method == FILE_APPROVAL:
            self._pending_files[record["approval_id"]] = str(params.get("itemId"))
        # Persist the reviewable request before it can receive a decision.
        self.on_approvals(copy.deepcopy(self._records))

    def waiting_for_user(self) -> bool:
        """Return True while a permission request waits for the user, when silence from Codex is expected."""
        with self.lock:
            return bool(self._pending) and not self._closed

    def decide(self, approval_id: str, decision: str, decided_by: dict[str, str] | None = None) -> None:
        """Apply exactly one allow/deny response to a still-live request.

        Persistence precedes the write to Codex. If either fails, the connection
        is closed so a retry cannot accidentally authorize the action twice.
        """
        if decision not in {"allow", "deny"}:
            raise ValueError("decision must be allow or deny")
        with self.lock:
            if self._closed or not self.is_active() or approval_id not in self._pending:
                raise LookupError(APPROVAL_GONE)
            record = next(item for item in self._records if item["approval_id"] == approval_id)
            if decision == "allow" and not record["can_allow"]:
                # The note already says why this request can only be denied.
                raise ValueError(record["note"])
            request_id = self._pending.pop(approval_id)
            self._pending_files.pop(approval_id, None)
            # The stall clock starts again from the answer, not from when the request arrived.
            self.last_activity = time.monotonic()
            record["status"] = "allowed" if decision == "allow" else "denied"
            if decided_by:
                record["decided_by"] = dict(decided_by)
            try:
                self.on_approvals(copy.deepcopy(self._records))
                self._write({"id": request_id, "result": {"decision": "accept" if decision == "allow" else "decline"}})
            except (OSError, ValueError):
                record["status"] = "expired"
                self.fail("Could not deliver the permission decision. The exchange was stopped.")
                raise OSError(self.error) from None

    def receive(self, message: dict[str, Any]) -> None:
        """Dispatch a server message without treating model prose as permission."""
        with self.lock:
            self.last_activity = time.monotonic()
            if self._closed:
                return
            if not isinstance(message, dict):
                raise ValueError("invalid app-server message")
            method = message.get("method")
            params = message.get("params") or {}
            if "id" in message and method:
                if not self.is_active():
                    self.close()
                elif method in {COMMAND_APPROVAL, FILE_APPROVAL}:
                    self._publish_approval(message["id"], method, params)
                elif method == "item/permissions/requestApproval":
                    # Broad turn/session grants are deliberately unsupported.
                    self._write({"id": message["id"], "result": {"permissions": {}, "scope": "turn"}})
                    self.on_event({"type": "error", "message": "Broad permission grant denied. Request a single command or file change instead."})
                elif method == "mcpServer/elicitation/request":
                    self._write({"id": message["id"], "result": {"action": "decline"}})
                else:
                    self._write({"id": message["id"], "error": {"code": -32601, "message": "Unsupported worker request"}})
                return
            if "id" in message:
                self._reply(message)
                return
            if method == "serverRequest/resolved":
                changed = False
                for approval_id, request_id in list(self._pending.items()):
                    if request_id == params.get("requestId") and params.get("threadId") == self.thread_id:
                        self._pending.pop(approval_id)
                        self._pending_files.pop(approval_id, None)
                        next(item for item in self._records if item["approval_id"] == approval_id)["status"] = "expired"
                        changed = True
                if changed:
                    self.on_approvals(copy.deepcopy(self._records))
            elif method == "turn/started":
                if params.get("threadId") == self.thread_id:
                    self.turn_id = params["turn"]["id"]
                    self.on_event({"type": "turn.started"})
            elif params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id and method in {"item/started", "item/completed"}:
                self._item(method, params["item"])
            elif method == "item/fileChange/patchUpdated" and params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id:
                self._cache_file_changes(str(params["itemId"]), params.get("changes") or [])
            elif method == "turn/completed" and params.get("threadId") == self.thread_id:
                turn = params["turn"]
                if turn.get("id") != self.turn_id:
                    raise ValueError("completion does not belong to the active exchange")
                self._turn_ended = True
                if turn.get("status") == "completed" and self._final_text:
                    self.on_final(self._final_text)
                    self.returncode = 0
                else:
                    self.error = self.redact(str((turn.get("error") or {}).get("message") or "Codex finished without a final answer."))
                self.close()

    def _reply(self, message: dict[str, Any]) -> None:
        """Advance initialization only after the preceding request succeeded."""
        method = self._requests.pop(message["id"], None)
        if method is None:
            return
        if message.get("error"):
            raise ValueError(f"{method}: {message['error'].get('message', 'request failed')}")
        result = message.get("result") or {}
        if method == "initialize":
            self._write({"method": "initialized"})
            self._request("thread/resume" if self.thread_params.get("threadId") else "thread/start", self.thread_params)
        elif method in {"thread/start", "thread/resume"}:
            if result.get("approvalPolicy") != "on-request" or result.get("approvalsReviewer") != "user":
                raise ValueError("Codex did not enable user approval requests")
            self.thread_id = result["thread"]["id"]
            expected = self.thread_params.get("threadId")
            if expected and self.thread_id != expected:
                raise ValueError("Codex resumed a different conversation")
            self.on_event({"type": "thread.started", "thread_id": self.thread_id})
            self._request("turn/start", {**self.turn_params, "threadId": self.thread_id})
        elif method == "turn/start":
            self.turn_id = result["turn"]["id"]

    def _cache_file_changes(self, item_id: str, raw_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bound and redact complete file actions, refusing to replace a pending review."""
        changes = []
        remaining = DISPLAY_LIMIT
        for change in raw_changes[:REQUEST_LIMIT]:
            kind = change.get("kind") or {}
            entry: dict[str, Any] = {"kind": str(kind.get("type") or "unknown"), "truncated": False, "redacted": False}
            for key, raw in (("path", change.get("path")), ("move_path", kind.get("move_path")), ("diff", change.get("diff"))):
                raw = str(raw or "")
                text = self.redact(raw)
                entry["redacted"] |= text != raw
                entry["truncated"] |= len(text) > max(remaining, 0)
                entry[key] = text[:max(remaining, 0)]
                remaining -= len(text)
            changes.append(entry)
        if len(raw_changes) > REQUEST_LIMIT and changes:
            changes[-1]["truncated"] = True
        if item_id in self._pending_files.values() and self._file_changes.get(item_id) != changes:
            raise ValueError("File changes were updated while permission was pending. The exchange was stopped.")
        self._file_changes[item_id] = changes
        return changes

    def _item(self, method: str, item: dict[str, Any]) -> None:
        """Translate app-server items into the worker's existing activity format."""
        kind = item.get("type")
        mapped = {"agentMessage": "agent_message", "commandExecution": "command_execution",
                  "fileChange": "file_change", "mcpToolCall": "mcp_tool_call", "webSearch": "web_search"}
        if kind not in mapped and kind != "reasoning":
            # Images are collected from the rollout after completion. Do not
            # duplicate image bytes or user input in the activity log.
            return
        event_item = {**item, "type": mapped.get(kind, kind)}
        if item.get("status") == "inProgress":
            event_item["status"] = "in_progress"
        if kind == "commandExecution":
            event_item.update(aggregated_output=item.get("aggregatedOutput"), exit_code=item.get("exitCode"))
        elif kind == "fileChange":
            changes = self._cache_file_changes(str(item["id"]), item.get("changes") or [])
            if method == "item/completed":
                self._file_changes.pop(str(item["id"]), None)
            event_item["changes"] = [{"path": c["path"], "kind": c["kind"], "move_path": c["move_path"]} for c in changes]
        elif kind == "reasoning":
            event_item = {"id": item["id"], "type": "reasoning", "text": "\n".join(item.get("summary") or [])}
        elif kind == "agentMessage" and method == "item/completed":
            if item.get("phase") != "commentary":
                self._final_text = str(item.get("text") or "")
        self.on_event({"type": method.replace("/", "."), "item": event_item})
