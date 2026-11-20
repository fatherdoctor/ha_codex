"""Bridge Codex app-server turns to explicit, single-action user approvals.

The worker owns the process and authentication. This adapter owns JSON-RPC
correlation and live requests; only redacted display records leave it. Approval
responses never install command rules or grant access for a session.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from typing import Any, Callable, TextIO

import hass_mcp


APPROVAL_GONE = "This permission request is no longer waiting for an answer."
DISPLAY_LIMIT = 32000
REQUEST_LIMIT = 50
# A plan update with more entries than this is ignored: nobody reads a plan that long.
PLAN_STEP_LIMIT = 50
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
TOOL_APPROVAL = "mcpServer/elicitation/request"

# The (server, tool) pairs whose action requests get a page card: the plain Home Assistant tool that server.py
# registers as home_assistant_call, and the write tools of the bundled Home Assistant MCP server (hass_mcp.py), which
# Codex asks about before each call. A question from any other server or tool is still declined (_elicitation_refusal).
TOOL_CARDS = frozenset({("home_assistant_call", "call"),
                        *((hass_mcp.SERVER_NAME, tool) for tool in hass_mcp.WRITE_TOOLS)})

# Codex 0.166.0 asks before a tool call with a form whose _meta has codex_approval_kind "mcp_tool_call" and the full
# arguments in tool_params. The tool name is only in its message, which for a tool outside the ChatGPT apps is always
# this sentence (codex-rs/core/src/mcp_tool_call.rs, build_mcp_tool_approval_fallback_message).
_TOOL_QUESTION = re.compile(r'Allow the ([A-Za-z0-9_-]{1,64}) MCP server to run tool "([A-Za-z0-9_-]{1,64})"\?')


_SERVER_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _elicitation_refusal(params: dict[str, Any]) -> str:
    """Name the tool server, when it has a plain name, in the refusal shown in the chat."""
    name = params.get("serverName")
    source = f"The {name} tool" if isinstance(name, str) and _SERVER_NAME.fullmatch(name) else "A tool"
    return f"{source} asked a question this page cannot show, so it was declined and nothing was changed."


def decision_result(kind: str, allow: bool) -> dict[str, Any]:
    """The reply Codex reads for a request of this kind: a tool's form, or a command's or file change's decision."""
    if kind == "tool":
        # Accept with no remember option is one approval; Codex asks again before the next call.
        return {"action": "accept" if allow else "decline", "content": None, "_meta": None}
    return {"decision": "accept" if allow else "decline"}


def tool_request(params: dict[str, Any]) -> tuple[str, str] | None:
    """Return (server, tool) when this is Codex's own request to run a tool that has a page card, else None."""
    meta = params.get("_meta")
    if params.get("mode") != "form" or not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call":
        return None
    question = _TOOL_QUESTION.fullmatch(str(params.get("message") or ""))
    server = params.get("serverName")
    if not question or question.group(1) != server or (server, question.group(2)) not in TOOL_CARDS:
        return None
    return server, question.group(2)


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

def _service_confirmation(domain: str, service: str, targets: list[str]) -> dict[str, str]:
    """Plain words for one Home Assistant action on the named targets, or on all it reaches when none is named."""
    if (domain, service) in _RELOADS:
        what, effect = _RELOADS[(domain, service)]
        return {"what": what, "where": "This Home Assistant.", "effect": effect}
    where = ", ".join(targets) if targets else f"every {domain} the action reaches"
    verb = _SERVICE_WORDS.get(service, f"runs {domain}.{service} on")
    if domain == "scene" and service == "turn_on":
        verb = "applies"
    return {"what": f"Run the Home Assistant action {domain}.{service}.",
            "where": (where[0].upper() + where[1:] if where.startswith("every ") else where) + ".",
            "effect": f"This {verb} {where}."}

def _command_confirmation(command: str, cwd: str, network: str) -> dict[str, str]:
    """Plain words for a command: a Home Assistant action when one is named, else the command itself."""
    lower = (command or "").lower()
    call = _SERVICE_PATH.search(lower)
    if call:
        domain = call.group(1)
        targets = [entity for entity in _entity_ids(lower) if entity.split(".")[0] == domain] or _entity_ids(lower)
        return _service_confirmation(domain, call.group(2), targets)
    where = f"In {cwd}" + (f", reaching {network}" if network else "") + "." if cwd else "On this Home Assistant."
    return {"what": "Run one command outside the sandbox, shown below.", "where": where,
            "effect": "Only what that command does; it may read files and use the network."}

_SERVICE_NAME = re.compile(r"([a-z0-9_]+)\.([a-z0-9_]+)")

# The id fields a target can hold besides entity_id, and how the card names each of their ids.
_TARGET_WORDS = {"device_id": "of device", "area_id": "in area", "floor_id": "on floor", "label_id": "with label"}

def _target_names(domain: str, target: Any) -> list[str]:
    """Name every id the target holds, so the card says no less than the call reaches.

    Entity ids are named as they are, of any domain; an area, device, floor or label id as "every <domain> in area
    <id>" and so on. A target that is not an object of these fields, each one id or a list of ids, gives no names,
    so the words fall back to every <domain> the action reaches; the JSON on the card still shows it in full.
    """
    if not isinstance(target, dict) or not set(target) <= {"entity_id", *_TARGET_WORDS}:
        return []
    names: list[str] = []
    for key in ("entity_id", *_TARGET_WORDS):
        value = target.get(key, [])
        ids = [value] if isinstance(value, str) else value
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            return []
        if key == "entity_id":
            names += [f"every {domain}" if item == "all" else item for item in ids]
        else:
            names += [f"every {domain} {_TARGET_WORDS[key]} {item}" for item in ids]
    return names

# The target fields a Home Assistant action's data can hold at its top level.
_TARGET_FIELDS = ("entity_id", *_TARGET_WORDS)

def _data_targets(domain: str, data: dict[str, Any]) -> list[str]:
    """Name what an action reaches from the target fields at the top level of its data (the REST API's form)."""
    return _target_names(domain, {key: data[key] for key in _TARGET_FIELDS if key in data})

_SERVICE_NAME_PART = re.compile(r"[a-z0-9_]+")

_ENTITY_ACTIONS = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}

# What each dashboard tool of hass-mcp does, in the card's words.
_DASHBOARD_WORDS = {
    "set_dashboard_config": "Replace the whole configuration of a dashboard.",
    "add_card": "Add a card to a dashboard.",
    "update_card": "Replace a card on a dashboard.",
    "remove_card": "Remove a card from a dashboard.",
    "move_card": "Move a card on a dashboard.",
    "add_view": "Add a view to a dashboard.",
    "remove_view": "Remove a view from a dashboard.",
    "update_view": "Change a view of a dashboard.",
    "restore_dashboard": "Restore a dashboard from one of its backups.",
}

def _hass_mcp_confirmation(tool: str, parsed: Any) -> dict[str, str] | None:
    """Plain words for a write tool of the Home Assistant MCP server, or None when the arguments do not fit its form.

    entity_action and call_service_tool are Home Assistant actions and get the same words as home_assistant_call.
    entity_action sends {"entity_id": entity_id, **params} (hass-mcp 0.6.0, app/server.py), so a target inside
    params replaces the entity id, and the card names what params holds.
    """
    arguments = parsed if isinstance(parsed, dict) else {}
    if tool == "restart_ha":
        return _service_confirmation("homeassistant", "restart", [])
    if tool == "entity_action":
        entity_id, params = arguments.get("entity_id"), arguments.get("params") or {}
        service = _ENTITY_ACTIONS.get(arguments.get("action"))
        if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id) or not service or not isinstance(params, dict):
            return None
        domain = entity_id.split(".")[0]
        return _service_confirmation(domain, service, _data_targets(domain, {"entity_id": entity_id, **params}))
    if tool == "call_service_tool":
        domain, service, data = arguments.get("domain"), arguments.get("service"), arguments.get("data") or {}
        if not all(isinstance(name, str) and _SERVICE_NAME_PART.fullmatch(name) for name in (domain, service)):
            return None
        if not isinstance(data, dict):
            return None
        return _service_confirmation(domain, service, _data_targets(domain, data))
    if tool in _DASHBOARD_WORDS:
        url_path = arguments.get("url_path")
        where = f"The dashboard {url_path}." if isinstance(url_path, str) and url_path else "The default dashboard."
        if arguments.get("dry_run") is True:
            effect = "Nothing changes: this is a dry run that only shows what the change would be."
        else:
            effect = "The dashboard changes at once; open it to see the change."
        return {"what": _DASHBOARD_WORDS[tool], "where": where, "effect": effect}
    return None


def _tool_confirmation(tool: str, arguments: str) -> dict[str, str]:
    """Plain words for a tool request: the Home Assistant action it names, else the tool and its arguments."""
    try:
        parsed = json.loads(arguments)
    except ValueError:
        parsed = None
    server, _, name = tool.partition(".")
    if server == hass_mcp.SERVER_NAME:
        words = _hass_mcp_confirmation(name, parsed)
        if words:
            return words
    service = parsed.get("service") if isinstance(parsed, dict) else None
    call = _SERVICE_NAME.fullmatch(service) if isinstance(service, str) else None
    if call:
        # Only the target names what the action reaches; an entity id inside data (a message, say) is not one.
        domain = call.group(1)
        return _service_confirmation(domain, call.group(2), _target_names(domain, parsed.get("target")))
    return {"what": f"Run the tool {tool}, with the arguments shown below.", "where": "This Home Assistant.",
            "effect": "Only what the tool does with these arguments."}

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
    if record["kind"] == "command":
        text = _command_confirmation(record["command"], record["cwd"], record.get("network", ""))
    elif record["kind"] == "tool":
        text = _tool_confirmation(record["tool"], record["arguments"])
    else:
        text = _changes_confirmation(record["changes"])
    if not record["can_allow"]:
        text["effect"] = "This request cannot be allowed here. " + record["note"]
    return text


# Codex's reviewer reports each review it makes in an Auto-review chat (app-server-protocol common.rs at
# rust-v0.166.0). It sends no request: it allows or denies in place of the person.
AUTO_REVIEW_EVENTS = {"item/autoApprovalReview/started", "item/autoApprovalReview/completed"}
AUTO_REVIEW_STATUSES = {"inProgress", "approved", "denied", "timedOut", "aborted"}
AUTO_REVIEW_RISKS = {"low", "medium", "high", "critical"}
AUTO_REVIEW_RATIONALE_LIMIT = 500


def auto_review_event(method: str, params: dict[str, Any], redact: Callable[[str], str]) -> dict[str, Any]:
    """Return the chat event for one review by Codex's reviewer: its id, status, risk, what it checked and why.

    Only known words pass: an unknown status reads as "unknown" and is shown as
    not allowed. The reviewer's reason is model text, so it is redacted and
    clipped like any other text the chat shows.
    """
    review = params.get("review") if isinstance(params.get("review"), dict) else {}
    action = params.get("action") if isinstance(params.get("action"), dict) else {}
    # A list or an object is not a word, and a set lookup of it would raise, so only text is looked up.
    status = review.get("status") if isinstance(review.get("status"), str) else None
    if method.endswith("/started"):
        status = "inProgress"
    elif status not in AUTO_REVIEW_STATUSES - {"inProgress"}:
        status = "unknown"
    risk = review.get("riskLevel") if isinstance(review.get("riskLevel"), str) else ""
    rationale = review.get("rationale") if isinstance(review.get("rationale"), str) else ""
    return {
        "type": "auto_review",
        "review_id": str(params.get("reviewId") or "")[:128],
        "status": status,
        "risk_level": risk if risk in AUTO_REVIEW_RISKS else "",
        "action": str(action.get("type") or "")[:40],
        "rationale": redact(rationale)[:AUTO_REVIEW_RATIONALE_LIMIT],
    }


class AppServerSession:
    """Run one app-server turn and correlate each user decision with its request.

    All state and writes use the worker's reentrant lock, including cancellation
    and the HTTP decision route. A browser cannot supply protocol request IDs,
    commands, permission scopes, or policy amendments.

    The session works in one of two ways:

    - It owns the connection (the default): one new app-server process for one
      exchange. The session sends initialize, then thread/start or thread/resume,
      then turn/start, and close ends the process input.
    - It does not own the connection (owns_connection=False): the process is
      shared by many chats and the thread in thread_params["threadId"] is already
      loaded. The session sends only turn/start, with the chat's model in the
      turn, because no thread request carries it. Its request IDs are unique to
      the session, so a reader of the shared output can give each reply to its
      session (owns_request). Close interrupts a running turn and leaves the
      input open for the other chats. The caller routes each message of the
      shared output by thread id and request ID; read is for an owned
      connection only, because on a shared output it would take the lines of
      the other chats.
    """

    def __init__(
        self, writer: TextIO, *, lock: Any, thread_params: dict[str, Any],
        turn_params: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
        on_approvals: Callable[[list[dict[str, Any]]], None],
        on_final: Callable[[str], None], is_active: Callable[[], bool],
        redact: Callable[[str], str], owns_connection: bool = True,
    ) -> None:
        """Bind a live transport to one worker exchange and its persistence callbacks."""
        attached_thread = thread_params.get("threadId")
        if not owns_connection and (not isinstance(attached_thread, str) or not attached_thread):
            raise ValueError("A session on a shared connection needs the thread id of a loaded thread.")
        self.writer = writer
        self.owns_connection = owns_connection
        # The prefix of every request ID that this session sends. On a shared connection a reply has no thread id,
        # so the ID alone says which session sent the request.
        self.request_prefix = "worker" if owns_connection else f"worker-{uuid.uuid4().hex}"
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
        self._turn_requested = False
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
        request_id = f"{self.request_prefix}-{self._next_id}"
        self._requests[request_id] = method
        self._write({"id": request_id, "method": method, "params": params})

    def owns_request(self, request_id: Any) -> bool:
        """Return True when the ID has this session's prefix, so a reply with it belongs to this session.

        The check is by prefix only: it does not say whether the request still waits. _reply ignores an ID it does
        not know.
        """
        return isinstance(request_id, str) and request_id.startswith(self.request_prefix + "-")

    @property
    def turn_running(self) -> bool:
        """Return True when turn/start was sent, Codex did not refuse it, and turn/completed has not arrived.

        On a shared connection, a caller reads this after close: the turn can still run in the process. Close sends
        turn/interrupt when it knows the turn id; when the reply to turn/start comes later, the session sends it
        then. The session still reads that reply and turn/completed after close, so the value turns False when the
        turn ends. On an owned connection close ends the process, and the value is not kept after close.
        """
        with self.lock:
            return self._turn_requested and not self._turn_ended

    def start(self) -> None:
        """Initialize the protocol; thread and turn creation follow their replies.

        On a shared connection the process is initialized and the thread is loaded, so the turn starts at once.
        """
        with self.lock:
            if self._closed or not self.is_active():
                self.close()
                return
            if not self.owns_connection:
                self.thread_id = self.thread_params["threadId"]
                self.on_event({"type": "thread.started", "thread_id": self.thread_id})
                turn = {**self.turn_params, "threadId": self.thread_id}
                if "model" in self.thread_params and "model" not in turn:
                    turn["model"] = self.thread_params["model"]
                self._start_turn(turn)
                return
            self._request("initialize", {"clientInfo": {
                "name": "home_assistant_worker", "title": "Home Assistant", "version": "1",
            }})

    def _start_turn(self, params: dict[str, Any]) -> None:
        """Send turn/start and remember that a turn can now run."""
        self._turn_requested = True
        self._request("turn/start", params)

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
                # A failed close must not keep the transport alive.
                pass

    def close(self) -> None:
        """Expire all live requests and close stdin; saved records cannot grant access.

        On a shared connection the input stays open: other chats use it. A running turn gets turn/interrupt there.
        """
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.thread_id and self.turn_id and not self._turn_ended:
                try:
                    self._write({"id": f"{self.request_prefix}-stop", "method": "turn/interrupt", "params": {
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
                if self.owns_connection:
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
        # Before the turn starts, turn_id is empty and a request without a turnId would match it.
        if not self.turn_id or params.get("threadId") != self.thread_id or params.get("turnId") != self.turn_id:
            raise ValueError("permission request does not belong to the active exchange")
        # A repeated request id is an error even when the request limit is reached.
        if request_id in self._pending.values():
            raise ValueError("duplicate live permission request")
        kind = {COMMAND_APPROVAL: "command", FILE_APPROVAL: "file_change", TOOL_APPROVAL: "tool"}[method]
        if len(self._records) >= REQUEST_LIMIT:
            self._write({"id": request_id, "result": decision_result(kind, False)})
            self.on_event({"type": "error", "message": "Too many permission requests in this exchange; request denied."})
            return
        reason, _ = self._display(params.get("reason"))
        clipped = False
        # Anything but a new command, such as input typed into a running one, is shown but never allowed.
        request_kind = params.get("kind", "command") if method == COMMAND_APPROVAL else ""
        # The counter's kind: a new command, input to a running one, a file change, or another request (a tool's too).
        prompt_kind = "file_change" if kind == "file_change" else "other"
        if kind == "command":
            prompt_kind = {"command": "command", "writeStdin": "command_input"}.get(request_kind, "other")
        record: dict[str, Any] = {
            "approval_id": uuid.uuid4().hex, "status": "pending", "reason": reason, "kind": kind,
            "prompt_kind": prompt_kind, "can_allow": True, "command": "", "cwd": "", "changes": [],
        }
        if kind == "tool":
            # Shown in full or not allowed: the arguments Codex will send, as JSON, after credentials are hidden.
            server, tool = tool_request(params)
            record["tool"] = f"{server}.{tool}"
            arguments = params["_meta"].get("tool_params")
            record["arguments"], clipped = self._display(
                json.dumps(arguments, indent=2, sort_keys=True, ensure_ascii=False))
            record["scope"] = "Allow this one tool call once. Codex asks again before the next one."
            record["can_allow"] = isinstance(arguments, dict)
        elif method == COMMAND_APPROVAL:
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
        if clipped:
            record["can_allow"] = False
        if request_kind == "writeStdin":
            record["note"] = "Input to a running command cannot be allowed here. Deny it and ask for the whole action as one command."
        elif kind == "tool" and record["can_allow"]:
            record["note"] = ("Known credentials are hidden. Allow once runs this call only; it does not change your "
                              "permission mode or approve the next call.")
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
                self._write({"id": request_id, "result": decision_result(record["kind"], decision == "allow")})
            except (OSError, ValueError):
                record["status"] = "expired"
                self.fail("Could not deliver the permission decision. The exchange was stopped.")
                raise OSError(self.error) from None

    def receive(self, message: dict[str, Any]) -> None:
        """Dispatch a server message without treating model prose as permission."""
        with self.lock:
            self.last_activity = time.monotonic()
            if self._closed:
                if not self.owns_connection:
                    self._track_closed_turn(message)
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
                elif method == TOOL_APPROVAL and tool_request(params):
                    self._publish_approval(message["id"], method, params)
                elif method == TOOL_APPROVAL:
                    # The page has no card for any other tool question, so say so in the chat
                    # instead of declining where nobody can see it.
                    self._write({"id": message["id"], "result": {"action": "decline"}})
                    self.on_event({"type": "error", "message": _elicitation_refusal(params)})
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
            elif method in AUTO_REVIEW_EVENTS and params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id:
                self.on_event(auto_review_event(method, params, self.redact))
            elif (method == "turn/plan/updated" and self.turn_id
                  and params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id):
                self._plan(params.get("plan"))
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

    def _track_closed_turn(self, message: Any) -> None:
        """Keep the turn state of a closed session on a shared connection, and nothing else.

        The process goes on with the turn after close, so turn_running must still see it end. Three messages count:
        the reply to turn/start (an error means no turn ran; a turn id gets the turn/interrupt that close could not
        send yet), and turn/completed for this thread and turn. A closed session sends no event, final text,
        approval or decision.
        """
        if not isinstance(message, dict):
            return
        params = message.get("params") or {}
        if "id" in message and "method" not in message:
            if self._requests.get(message["id"]) != "turn/start":
                return
            self._requests.pop(message["id"])
            turn = (message.get("result") or {}).get("turn") or {}
            if message.get("error") or not turn.get("id"):
                self._turn_requested = False
                return
            self.turn_id = turn["id"]
            if not self._turn_ended:
                try:
                    self._write({"id": f"{self.request_prefix}-stop", "method": "turn/interrupt", "params": {
                        "threadId": self.thread_id, "turnId": self.turn_id,
                    }})
                except (OSError, ValueError):
                    pass
        elif (message.get("method") == "turn/completed" and self.turn_id
              and params.get("threadId") == self.thread_id and (params.get("turn") or {}).get("id") == self.turn_id):
            self._turn_ended = True

    def _reply(self, message: dict[str, Any]) -> None:
        """Advance initialization only after the preceding request succeeded."""
        method = self._requests.pop(message["id"], None)
        if method is None:
            return
        if message.get("error"):
            if method == "turn/start":
                # Codex refused the turn, so no turn runs.
                self._turn_requested = False
            raise ValueError(f"{method}: {message['error'].get('message', 'request failed')}")
        result = message.get("result") or {}
        if method == "initialize":
            self._write({"method": "initialized"})
            self._request("thread/resume" if self.thread_params.get("threadId") else "thread/start", self.thread_params)
        elif method in {"thread/start", "thread/resume"}:
            self.thread_id = result["thread"]["id"]
            expected = self.thread_params.get("threadId")
            if expected and self.thread_id != expected:
                raise ValueError("Codex resumed a different conversation")
            self.on_event({"type": "thread.started", "thread_id": self.thread_id})
            self._start_turn({**self.turn_params, "threadId": self.thread_id})
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

    def _plan(self, plan: Any) -> None:
        """Show Codex's plan as one to-do step; ignore an update that does not have the expected shape.

        Each entry must be a dictionary with a text "step" and a text "status". The text is not
        trusted here: the event goes to on_event like every other step, where server.py redacts it
        (redact_event) and cuts it to the activity length limit (clip_activity_text).
        """
        if not isinstance(plan, list) or len(plan) > PLAN_STEP_LIMIT:
            return
        for entry in plan:
            if not isinstance(entry, dict) or not isinstance(entry.get("step"), str) or not isinstance(entry.get("status"), str):
                return
        items = [{"text": entry["step"], "completed": entry["status"] == "completed"} for entry in plan]
        self.on_event({"type": "item.updated", "item": {"id": "app-server:plan", "type": "todo_list", "items": items}})

    def _spawned(self, method: str, item: dict[str, Any]) -> bool:
        """Return True for the completed spawn item of this thread, the only collab item whose ids Codex made.

        In the other collab tools (send_input, wait, resume_agent, close_agent and the rest) the model writes the
        receiver, so those ids may name the thread of another chat.
        """
        return (method == "item/completed" and item.get("tool") == "spawnAgent"
                and item.get("senderThreadId") == self.thread_id)

    def _item(self, method: str, item: dict[str, Any]) -> None:
        """Translate app-server items into the worker's existing activity format."""
        kind = item.get("type")
        if kind == "collabAgentToolCall":
            if self._spawned(method, item):
                # The ids of the sub-agents that this thread started. Their tool calls name their own thread.
                threads = [value for value in item.get("receiverThreadIds") or [] if isinstance(value, str)]
                if threads:
                    self.on_event({"type": "agent.threads", "parent": self.thread_id, "threads": threads})
            return
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
