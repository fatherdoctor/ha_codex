"""Proofs P1 to P7, P9 and P10 of the long-lived Codex design, run against the pinned Codex CLI.

The design (CX-SPEED, version 4) keeps one `codex app-server` process alive and runs each chat as a thread in
it. Before any of that is built, these tests check what the design assumes about the pinned program:

- P1: after `thread/start` or `thread/resume`, every required tool program runs, and Codex sends a ready notice
  for each (`mcpServer/startupStatus/updated`).
- P2: with `thread_unload_delay_secs=0`, `thread/unsubscribe` unloads an idle thread, its tool programs stop,
  and `thread/closed` arrives.
- P3: `thread/resume` of a paused thread gives the same thread id and its history, and the next turn sees the
  earlier turns.
- P4: a turn whose permission mode differs from the thread's applies that mode and starts no tool program again.
- P5: three times (process start to `initialize`; `thread/start` to all tools ready; `thread/resume` of a chat
  with 20 turns to all tools ready). Not a gate: the times are printed for the pull request.
- P6: after `turn/interrupt`, `turn/completed` arrives and the thread accepts a new turn.
- P7: what Codex does when a second process (as remote control is) adds a turn to a thread that is live here.
  Recorded, not a gate, unless Codex damages the session file.
- P9: each tool program receives `_meta.threadId` in `tools/call`, equal to the chat's thread id, and the id of a
  new thread is known before its first tool call. A sub-agent's call carries the sub-agent's own thread id.
- P10: two threads of one process run a turn at the same time, each event names its thread, each thread has its
  own tool programs, and `turn/interrupt` of one does not stop the other.

P8 (the sandbox and the worker's sockets) is in test_sandbox_sockets.py.

No account and no network: a local HTTP server stands in for the model and answers with scripted items. The
tool programs are the worker's own, launched with the worker's own arguments (server.build_app_server_args),
each behind a small relay that writes every line Codex sends it to a log. Their sockets do not exist here, so a
tool call returns an error to Codex; the proofs read only what Codex sent.

The tests skip when the pinned CLI is not present, and fail instead when HA_TEST_REQUIRE_CODEX=1 (the
Verification checks container job sets it). Run them by hand with HA_TEST_CODEX=<path to the codex binary of the
pinned package> (SANDBOX_TESTING.md).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER))

PINNED_VERSION = "0.166.0"
CODEX = os.environ.get("HA_TEST_CODEX", "/usr/local/bin/codex")
MODEL = "gpt-5.1-codex"
WAIT = 45


def pinned_cli_present() -> bool:
    """Return True when the binary in HA_TEST_CODEX (or the image's) is the pinned version."""
    try:
        version = subprocess.run([CODEX, "--version"], capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return PINNED_VERSION in version


def needs_cli(test_class: type) -> type:
    """Skip a test class without the pinned CLI, or fail at once when HA_TEST_REQUIRE_CODEX=1 asks for it.

    The container step of the verification workflow sets HA_TEST_REQUIRE_CODEX=1. A move of the Codex pin that
    leaves PINNED_VERSION behind then fails the step, so a green step always means the proofs ran.
    """
    if pinned_cli_present():
        return test_class
    reason = f"the pinned Codex CLI {PINNED_VERSION} is not at {CODEX}"
    if os.environ.get("HA_TEST_REQUIRE_CODEX") == "1":
        raise RuntimeError(f"HA_TEST_REQUIRE_CODEX=1, but {reason}")
    return unittest.skip(reason)(test_class)



def allow_local_sockets() -> None:
    """Allow the stand-in model's local HTTP server and the Unix sockets under pytest.

    The Home Assistant pytest plugin blocks socket creation before each test. These tests need a TCP server on
    127.0.0.1 and Unix sockets, so they lift the block and keep connections limited to 127.0.0.1 and Unix
    sockets. Call it in setUp: the plugin blocks again before the next test. Outside pytest it does nothing.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest_socket

        pytest_socket.enable_socket()
        pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)


# ---------------------------------------------------------------------------------------------------------------
# The stand-in model
# ---------------------------------------------------------------------------------------------------------------

def message_text(item: dict[str, Any]) -> str:
    """Return the text of one input message item of a Responses request."""
    parts = item.get("content")
    if isinstance(parts, str):
        return parts
    return "".join(part.get("text", "") for part in parts or [] if isinstance(part, dict))


class QuietHTTPServer(ThreadingHTTPServer):
    """An HTTP server that does not print the broken pipe of a request Codex stopped (an interrupted turn)."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Ignore a client that went away; report anything else."""
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


class ScriptedModel:
    """A Responses API stand-in that answers each request from the script named in the latest user message.

    A user message that contains `[script:<name>]` selects `scripts[<name>]`, a list of steps. The step taken is
    the number of tool outputs after that message, so one request gets one step and the model needs no state.
    A step is a dict: {"shell": <command>, **extra arguments}, {"tool": (<server>, <tool>), "arguments": {...}},
    {"spawn": <message for a sub-agent>}, {"hold": <threading.Event>} (wait, then answer with the next step), or
    {"final": <text>}. Past the end of the script the model answers "done".

    A request from Codex's own reviewer (Auto-review) is told apart by its output schema and answered with
    `review_outcome` ("allow" or "deny"); it counts in `reviews`, not in the scripts.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, list[dict[str, Any]]] = {}
        self.review_outcome = "deny"
        self.reviews: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self._lock = threading.Lock()
        model = self

        class Handler(BaseHTTPRequestHandler):
            """Serve one Responses request as an event stream."""

            def log_message(self, *_args: Any) -> None:
                """Keep the stand-in's access log out of the test output."""

            def do_POST(self) -> None:
                """Answer one request with the step its script names."""
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                item = model.answer(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in (
                    {"type": "response.created", "response": {"id": "resp"}},
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "resp", "status": "completed",
                                                                "output": [item]}},
                ):
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()

        self.http = QuietHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        """Return the address Codex's model provider setting points at."""
        return f"http://127.0.0.1:{self.http.server_port}/v1"

    def close(self) -> None:
        """Stop the HTTP server."""
        self.http.shutdown()
        self.http.server_close()

    def requests_for(self, name: str) -> list[dict[str, Any]]:
        """Return the requests whose latest user message selected the script `name`."""
        return [request for request in self.requests if script_name(request) == name]

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """Pick the step for one request, waiting first when the step says hold."""
        if is_review(request):
            with self._lock:
                self.reviews.append(request)
            verdict = {"risk_level": "low", "user_authorization": "unknown", "outcome": self.review_outcome,
                       "rationale": "scripted test reviewer"}
            return self.item(request, {"final": json.dumps(verdict)}, "review")
        with self._lock:
            self.requests.append(request)
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            name = script_name(request)
            steps = self.scripts.get(name or "", [])
            index = outputs_since_user(request)
            step = steps[index] if index < len(steps) else {"final": "done"}
            if "hold" in step:
                step["hold"].wait(WAIT)
                step = steps[index + 1] if index + 1 < len(steps) else {"final": "done"}
            return self.item(request, step, f"{name}_{index}")
        finally:
            with self._lock:
                self.in_flight -= 1

    @staticmethod
    def item(request: dict[str, Any], step: dict[str, Any], call_id: str) -> dict[str, Any]:
        """Build the one output item of a step."""
        if "final" in step:
            return {"type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
                    "content": [{"type": "output_text", "text": step["final"], "annotations": []}]}
        if "shell" in step:
            namespace, tool = find_tool(request, "exec_command")
            arguments = {"cmd": step["shell"], "max_output_tokens": 4000,
                         **{key: value for key, value in step.items() if key != "shell"}}
        elif "spawn" in step:
            namespace, tool = find_tool(request, "spawn_agent")
            arguments = {"message": step["spawn"]}
        else:
            server, wanted = step["tool"]
            namespace, tool = find_tool(request, wanted, server)
            arguments = step.get("arguments", {})
        item = {"type": "function_call", "call_id": call_id, "name": tool, "arguments": json.dumps(arguments)}
        if namespace:
            item["namespace"] = namespace
        return item


def is_review(request: dict[str, Any]) -> bool:
    """Return True for a request from Codex's reviewer: it asks for an answer with an allow or deny outcome."""
    schema = ((request.get("text") or {}).get("format") or {}).get("schema") or {}
    return "outcome" in (schema.get("properties") or {})


def script_name(request: dict[str, Any]) -> str | None:
    """Return the script the latest user message of a request names, or None."""
    for item in reversed(request.get("input") or []):
        if item.get("type") == "message" and item.get("role") == "user":
            text = message_text(item)
            if "[script:" in text:
                return text.split("[script:", 1)[1].split("]", 1)[0]
    return None


def outputs_since_user(request: dict[str, Any]) -> int:
    """Count the tool outputs after the latest user message that names a script."""
    count = 0
    for item in reversed(request.get("input") or []):
        if item.get("type") == "message" and item.get("role") == "user" and "[script:" in message_text(item):
            return count
        if str(item.get("type", "")).endswith("_output"):
            count += 1
    return count


def offered_tools(request: dict[str, Any]) -> list[tuple[str | None, str]]:
    """Return (namespace, name) for each function a request offers the model."""
    found = []
    for tool in request.get("tools") or []:
        if tool.get("type") == "namespace":
            found.extend((tool.get("name"), child.get("name")) for child in tool.get("tools") or [])
        else:
            found.append((None, tool.get("name")))
    return found


def find_tool(request: dict[str, Any], name: str, server: str | None = None) -> tuple[str | None, str]:
    """Return (namespace, function name) of one offered function.

    Without `server`, the function is named `name` (a built-in such as exec_command). With it, the function is
    the MCP tool `name` of that server: Codex offers it in a namespace or under a name that holds the server's.
    """
    for namespace, offered in offered_tools(request):
        if server is None and offered == name:
            return namespace, offered
        if server is not None and offered and offered.endswith(name) and server in f"{namespace}{offered}":
            return namespace, offered
    raise AssertionError(f"no such tool offered: {server or ''} {name}: {offered_tools(request)}")


# ---------------------------------------------------------------------------------------------------------------
# The tool programs, each behind a relay that logs what Codex sends
# ---------------------------------------------------------------------------------------------------------------

RELAY = textwrap.dedent('''
    """Start one tool program and write each line Codex sends it, with the time and process id, to a log."""
    import json, os, subprocess, sys, threading, time
    log, real = sys.argv[1], sys.argv[2:]
    child = subprocess.Popen(real, stdin=subprocess.PIPE, stdout=sys.stdout, bufsize=0)
    def write(kind, value):
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "pid": os.getpid(), "time": time.time(), "value": value}) + "\\n")
    write("start", real)
    for line in sys.stdin.buffer:
        try:
            write("in", json.loads(line))
        except ValueError:
            write("raw", line.decode(errors="replace"))
        child.stdin.write(line)
        child.stdin.flush()
    write("eof", None)
    child.stdin.close()
    child.wait()
''')


def worker_app_server_args(root: Path) -> tuple[list[str], list[str]]:
    """Return the worker's own app-server arguments with each tool program behind the relay, and the server names.

    server.build_app_server_args is the launch the chat page uses. Only the binary and each tool program's
    command change: the command becomes the relay, which then starts the same program with the same arguments.

    The option home_assistant_mcp is off here: these proofs cover the worker's own tool programs, and the bundled
    hass-mcp (installed only in the image, with a Supervisor token) cannot start on a test runner, so Codex would
    report it failed after its 20 second start timeout. test_ha_mcp_option.py covers that server's launch.
    """
    import hass_mcp
    import server

    relay = root / "relay.py"
    relay.write_text(RELAY, encoding="utf-8")
    with patch.object(hass_mcp, "enabled", lambda _options: False):
        args = server.build_app_server_args()
    assert args[1] == "app-server", args[:2]
    args = args[2:]
    names = []
    rewritten = []
    index = 0
    while index < len(args):
        flag, value = args[index], args[index + 1] if index + 1 < len(args) else ""
        if flag == "--config" and value.startswith("mcp_servers.") and ".command=" in value:
            name = value.split(".")[1]
            names.append(name)
            program = json.loads(value.split("=", 1)[1])
            # The program's own args follow in the next --config pair (".args="); fold them into the relay's.
            arg_value = args[index + 3]
            assert arg_value.startswith(f"mcp_servers.{name}.args="), arg_value
            program_args = json.loads(arg_value.split("=", 1)[1])
            rewritten += ["--config", f"mcp_servers.{name}.command={json.dumps(sys.executable)}",
                          "--config", f"mcp_servers.{name}.args="
                          + json.dumps([str(relay), str(root / f"tool-{name}.log"), program, *program_args])]
            index += 4
            continue
        rewritten.append(flag)
        index += 1
    return rewritten, names


def tool_log(root: Path, name: str) -> list[dict[str, Any]]:
    """Return the lines the relay of one tool program wrote."""
    path = root / f"tool-{name}.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tool_processes(root: Path, names: list[str]) -> dict[str, list[int]]:
    """Return the relay process ids that started, by tool program."""
    return {name: [line["pid"] for line in tool_log(root, name) if line["kind"] == "start"] for name in names}


def pid_alive(pid: int) -> bool:
    """Return True while a process runs and is not a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def process_table() -> dict[int, tuple[int, int, str]]:
    """Return {pid: (parent pid, process group, start time)} of every live process that is not a zombie."""
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        # After the name: state, parent pid, process group, ...; the start time is field 22 of stat(5).
        state, parent, group, started = fields[0], int(fields[1]), int(fields[2]), fields[19]
        if state != "Z":
            table[int(entry.name)] = (parent, group, started)
    return table


def process_group(group_id: int) -> set[tuple[int, str]]:
    """Return (pid, start time) of every live member of one process group.

    The start time tells a process from a later one that gets the same pid.
    """
    return {(pid, started) for pid, (_parent, group, started) in process_table().items() if group == group_id}


def process_tree(root_pid: int) -> set[tuple[int, str]]:
    """Return (pid, start time) of every live descendant of a running process."""
    table = process_table()
    found = set()
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        for pid, (parent, _group, _started) in table.items():
            if parent == parent_pid and pid not in found:
                found.add(pid)
                pending.append(pid)
    return {(pid, table[pid][2]) for pid in found}


def still_running(processes: set[tuple[int, str]]) -> set[tuple[int, str]]:
    """Return the processes of the set that still run as the same process."""
    table = process_table()
    return {(pid, started) for pid, started in processes if pid in table and table[pid][2] == started}


def end_processes(processes: set[tuple[int, str]], timeout: float = 5) -> None:
    """Wait for each process to end; stop it with SIGTERM, then SIGKILL, when it outlives the timeout."""
    for signal_number in (None, signal.SIGTERM, signal.SIGKILL):
        if signal_number is not None:
            for pid, _started in still_running(processes):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal_number)
        deadline = time.monotonic() + timeout
        while still_running(processes) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not still_running(processes):
            return
    raise AssertionError(f"processes left after SIGKILL: {sorted(still_running(processes))}")


def tool_calls(root: Path, names: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Return (server name, params) of every tools/call request Codex sent to a tool program."""
    calls = []
    for name in names:
        for line in tool_log(root, name):
            value = line.get("value")
            if line["kind"] == "in" and isinstance(value, dict) and value.get("method") == "tools/call":
                calls.append((name, value.get("params") or {}))
    return calls


# ---------------------------------------------------------------------------------------------------------------
# A JSON-RPC client for `codex app-server`
# ---------------------------------------------------------------------------------------------------------------

def accept_everything(method: str, _params: dict[str, Any]) -> dict[str, Any]:
    """Accept every request from Codex, in the form the worker's own session answers it (approvals.py)."""
    from approvals import TOOL_APPROVAL, decision_result

    return decision_result("tool" if method == TOOL_APPROVAL else "command", "allow")


class AppServer:
    """One `codex app-server` process with a reader that keeps every message.

    approve(method, params) answers a request from Codex; by default every approval is accepted.
    """

    def __init__(self, root: Path, model: ScriptedModel, extra_args: list[str] | None = None,
                 tool_args: list[str] | None = None, home: Path | None = None) -> None:
        self.home = home or root / "codex-home"
        self.home.mkdir(exist_ok=True)
        settings = {
            "model_provider": "fixture", "model_providers.fixture.name": "Fixture",
            "model_providers.fixture.base_url": model.base_url,
            "model_providers.fixture.wire_api": "responses",
            "model_providers.fixture.requires_openai_auth": False,
            "check_for_update_on_startup": False,
        }
        config = [part for key, value in settings.items() for part in ("-c", f"{key}={json.dumps(value)}")]
        self.stderr = (root / "stderr.log").open("a", encoding="utf-8")
        self.started_at = time.monotonic()
        self.proc = subprocess.Popen(
            [CODEX, "app-server", *config, *(tool_args or []), *(extra_args or [])],
            cwd=root / "workspace", env={**os.environ, "CODEX_HOME": str(self.home)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, bufsize=1, start_new_session=True)
        self.messages: list[dict[str, Any]] = []
        self.changed = threading.Condition()
        self.responses: dict[int, queue.Queue] = {}
        self.next_id = 0
        self.write_lock = threading.Lock()
        self.requests_seen: list[tuple[str, dict[str, Any]]] = []
        self.approve: Callable[[str, dict[str, Any]], dict[str, Any]] = accept_everything
        threading.Thread(target=self.read, daemon=True).start()

    def read(self) -> None:
        """Keep each notification, route each response, and answer each request from Codex."""
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message and "method" in message:
                self.requests_seen.append((message["method"], message.get("params") or {}))
                self.send({"id": message["id"], "result": self.approve(message["method"], message.get("params") or {})})
            elif "id" in message:
                box = self.responses.get(message["id"])
                if box:
                    box.put(message)
            with self.changed:
                self.messages.append(message)
                self.changed.notify_all()

    def send(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC line."""
        with self.write_lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return the whole answer, a result or an error."""
        self.next_id += 1
        box: queue.Queue = queue.Queue()
        self.responses[self.next_id] = box
        self.send({"id": self.next_id, "method": method, "params": params or {}})
        return box.get(timeout=WAIT)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its result; fail on an error answer."""
        answer = self.call(method, params)
        assert "error" not in answer, (method, answer)
        return answer["result"]

    def initialize(self) -> float:
        """Run the handshake; return the seconds from process start to its answer."""
        self.request("initialize", {"clientInfo": {"name": "live-proofs", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
        elapsed = time.monotonic() - self.started_at
        self.send({"method": "initialized"})
        return elapsed

    def wait_for(self, test: Callable[[dict[str, Any]], bool], after: int = 0, timeout: float = WAIT) -> dict[str, Any]:
        """Wait for the first message at or after index `after` that passes `test`."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                for message in self.messages[after:]:
                    if test(message):
                        return message
                left = deadline - time.monotonic()
                assert left > 0, f"timed out; last messages: {self.messages[-8:]}"
                self.changed.wait(left)

    def mark(self) -> int:
        """Return the index of the next message, for wait_for."""
        with self.changed:
            return len(self.messages)

    def notices(self, method: str, after: int = 0) -> list[dict[str, Any]]:
        """Return the notifications of one method at or after index `after`."""
        with self.changed:
            return [m for m in self.messages[after:] if m.get("method") == method and "id" not in m]

    def turn(self, thread_id: str, text: str, wait: bool = True, **settings: Any) -> dict[str, Any]:
        """Start one turn; with wait, return its turn/completed notice."""
        start = self.mark()
        turn = self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}],
                                           **settings})["turn"]
        if not wait:
            return turn
        return self.wait_for(lambda m: m.get("method") == "turn/completed"
                             and (m.get("params") or {}).get("turn", {}).get("id") == turn["id"], start)

    def close(self) -> None:
        """Close the input, stop the process, and wait until each process it started has ended too.

        Codex starts child processes (the tool programs, and a git clone of its plugins under CODEX_HOME). A child
        can outlive the app-server and still write in the test's folder when tearDown removes it, which fails with
        "Directory not empty". The app-server runs in its own process group, so the children that leave its tree
        when it exits are still found. Calling close a second time does nothing more.
        """
        children = set()
        if self.proc.poll() is None:
            children = process_tree(self.proc.pid) | process_group(self.proc.pid)
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()
        deadline = time.monotonic() + 5
        while self.proc.poll() is None and time.monotonic() < deadline:
            children |= process_tree(self.proc.pid)
            time.sleep(0.05)
        if self.proc.poll() is None:
            children |= process_tree(self.proc.pid)
            self.proc.kill()
        self.proc.wait(timeout=5)
        # The process group id is the app-server's pid (start_new_session). The kernel keeps that id in use while
        # the group has a member, so the group still names only Codex's children after the app-server is gone.
        end_processes(children | process_group(self.proc.pid))
        self.stderr.close()
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdout.close()


def ready_servers(app: AppServer, after: int) -> dict[str, str]:
    """Return the latest startup status of each tool program since index `after`."""
    status = {}
    for notice in app.notices("mcpServer/startupStatus/updated", after):
        params = notice.get("params") or {}
        status[params.get("name")] = params.get("status")
    return status


def wait_ready(app: AppServer, names: list[str], after: int) -> float:
    """Wait until every named tool program reports ready since `after`; return the seconds it took."""
    begin = time.monotonic()
    app.wait_for(lambda _m: all(ready_servers(app, after).get(name) == "ready" for name in names), after)
    return time.monotonic() - begin


STAND_IN = """\
import os, subprocess, sys, time
from pathlib import Path

# Two writers that keep adding files under the stand-in's CODEX_HOME, as Codex's plugin clone does:
# one in a session of its own, still a child here, and one whose parent exits at once, so it leaves this tree.
home = Path(os.environ["CODEX_HOME"])
writer = ("import itertools, sys, time\\n"
          "for n in itertools.count():\\n"
          "    open(f'{sys.argv[1]}/{n}', 'w').close(); time.sleep(0.02)\\n")
(home / "own").mkdir()
(home / "orphan").mkdir()
own = subprocess.Popen([sys.executable, "-c", writer, str(home / "own")], start_new_session=True,
                       stdout=subprocess.DEVNULL)
launcher = subprocess.Popen([sys.executable, "-c",
                             "import subprocess, sys; "
                             "print(subprocess.Popen(sys.argv[1:], stdout=subprocess.DEVNULL).pid)",
                             sys.executable, "-c", writer, str(home / "orphan")], stdout=subprocess.PIPE, text=True)
orphan = int(launcher.communicate()[0])
(home / "pids").write_text(f"{own.pid} {orphan}")
sys.stdin.read()
"""


@unittest.skipUnless(os.environ.get("HA_TEST_REQUIRE_CODEX") == "1", "requires the built worker image")
class SandboxGitIdentity(unittest.TestCase):
    def test_git_resolves_worker_and_repository_identities_in_the_sandbox(self) -> None:
        import server

        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            root = Path(folder)
            home = root / "data" / "codex-home"
            home.mkdir(parents=True)
            with (patch.multiple(server, DATA_ROOT=home.parent, CODEX_HOME=home),
                  patch.object(server, "read_options", return_value={})):
                server.write_git_identity_defaults({})
                env = server.codex_env()
            checkout = root / "checkout"
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            # Git reports the identities it would put on a commit. This works with read-only .git metadata.
            for identity in ("ChatGPT Codex <noreply@openai.com>", "Example Project <project@example.invalid>"):
                if identity.startswith("Example"):
                    subprocess.run(["git", "config", "user.name", "Example Project"], cwd=checkout, check=True)
                    subprocess.run(["git", "config", "user.email", "project@example.invalid"], cwd=checkout, check=True)
                result = subprocess.run(
                    [CODEX, "sandbox", "--config", 'sandbox_mode="read-only"', "--", "sh", "-ec",
                     "git var GIT_AUTHOR_IDENT && git var GIT_COMMITTER_IDENT"],
                    cwd=checkout, env=env, text=True, capture_output=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                identities = [line.rsplit(" ", 2)[0] for line in result.stdout.splitlines()]
                self.assertEqual(identities, [identity, identity])


@unittest.skipUnless(os.environ.get("HA_TEST_REQUIRE_CODEX") == "1", "requires the built worker image")
class SandboxWorkbook(unittest.TestCase):
    """The image ships spreadsheet support for the Python that Codex commands run."""

    def test_openpyxl_round_trip(self) -> None:
        create = textwrap.dedent("""
            from openpyxl import Workbook

            book = Workbook()
            book.active["A1"] = "=SUM(1,2)"
            book.save("example.xlsx")
        """)
        read = textwrap.dedent("""
            from openpyxl import load_workbook

            book = load_workbook("example.xlsx", read_only=True)
            print(book.active["A1"].value)
            book.close()
        """)
        with tempfile.TemporaryDirectory() as work:
            # Saving a workbook needs temporary files. Reading an existing workbook does not.
            for mode, script, expected in (("workspace-write", create, ""), ("read-only", read, "=SUM(1,2)")):
                result = subprocess.run(
                    [CODEX, "sandbox", "--config", f'sandbox_mode="{mode}"',
                     "--config", "check_for_update_on_startup=false", "--", "python3", "-I", "-c", script],
                    cwd=work, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, f"{mode}: {result.stderr}")
                self.assertEqual(result.stdout.strip(), expected, mode)



class CloseWaitsForChildren(unittest.TestCase):
    """AppServer.close ends what the app-server started, so tearDown can remove the folder (CX-PROOFS-RMTREE).

    A stand-in for the codex binary starts two writers and exits when its input closes; no Codex is needed.
    """

    def test_close_ends_the_children_before_the_folder_goes(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ha-live-proofs-close-")
        root = Path(temporary.name)
        (root / "workspace").mkdir()
        program = root / "codex"
        program.write_text(f"#!{sys.executable}\n{STAND_IN}", encoding="utf-8")
        program.chmod(0o755)
        model = type("Model", (), {"base_url": "http://127.0.0.1:9/v1"})()
        with patch.dict(globals(), {"CODEX": str(program)}):
            app = AppServer(root, model)
        pids = root / "codex-home" / "pids"
        deadline = time.monotonic() + WAIT
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        writers = [int(pid) for pid in pids.read_text().split()]
        self.assertTrue(all(pid_alive(pid) for pid in writers), writers)
        app.close()
        self.assertFalse([pid for pid in writers if pid_alive(pid)], "a child of the app-server outlived close")
        app.close()
        temporary.cleanup()
        self.assertFalse(root.exists())


THREAD = {"cwd": None, "sandbox": "workspace-write", "approvalPolicy": "on-request", "approvalsReviewer": "user",
          "model": MODEL}


@needs_cli
class LiveProofs(unittest.TestCase):
    """Each test starts its own app-server with the worker's tool programs and a scripted model."""

    def setUp(self) -> None:
        allow_local_sockets()
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="ha-live-proofs-", dir=cache)
        self.root = Path(self.temporary.name)
        (self.root / "workspace").mkdir()
        self.model = ScriptedModel()
        self.tool_args, self.names = worker_app_server_args(self.root)
        self.apps: list[AppServer] = []

    def tearDown(self) -> None:
        for app in self.apps:
            app.close()
        self.model.close()
        # Codex clones its plugin list in the background, and that clone can still write into the Codex home
        # for a moment after the process is closed. Try the removal again for up to five seconds.
        for attempt in range(10):
            try:
                self.temporary.cleanup()
                return
            except OSError:
                if attempt == 9:
                    break
                time.sleep(0.5)
        # Codex can keep writing into its own scratch folder (codex-home/.tmp) after the proof has ended; on slow
        # machines that outlasts the retries above. A leftover there is not a proof failure, so ignore errors under
        # that folder only; any other leftover still fails the test.
        scratch = (self.root / "codex-home" / ".tmp").resolve()

        def ignore_scratch(function: Any, path: str, error: BaseException) -> None:
            here = Path(path).resolve()
            # The folders above the scratch folder cannot be removed while it still holds files, so they count too.
            if here != scratch and scratch not in here.parents and here not in scratch.parents:
                raise error

        shutil.rmtree(self.root, onexc=ignore_scratch)

    def start(self, extra_args: list[str] | None = None, home: Path | None = None,
              tools: bool = True) -> AppServer:
        """Start and initialize one app-server; keep it for tearDown."""
        app = AppServer(self.root, self.model, extra_args, self.tool_args if tools else [], home)
        self.apps.append(app)
        app.initialize()
        return app

    def thread(self, app: AppServer, **overrides: Any) -> str:
        """Start one thread in the workspace and return its id."""
        params = {**THREAD, "cwd": str(self.root / "workspace"), **overrides}
        return app.request("thread/start", params)["thread"]["id"]

    def required_names(self) -> list[str]:
        """Return the tool programs the worker marks required (all but GitHub)."""
        return [name for name in self.names if name != "codex_github"]

    # P1 -------------------------------------------------------------------------------------------------------
    def test_p1_tool_programs_start_and_report_ready(self) -> None:
        app = self.start()
        mark = app.mark()
        thread_id = self.thread(app)
        wait_ready(app, self.names, mark)
        running = tool_processes(self.root, self.names)
        self.assertTrue(all(len(pids) == 1 and pid_alive(pids[0]) for pids in running.values()), running)
        # Codex writes the session file at the first turn; a thread with no turn cannot be resumed.
        self.model.scripts["p1"] = [{"final": "one"}]
        app.turn(thread_id, "[script:p1] one turn")
        # Resume in a second process (a restart) starts them again and reports them again.
        app.close()
        again = self.start()
        mark = again.mark()
        resumed = again.request("thread/resume", {"threadId": thread_id, "cwd": str(self.root / "workspace")})
        self.assertEqual(resumed["thread"]["id"], thread_id)
        wait_ready(again, self.names, mark)
        self.assertTrue(all(len(pids) == 2 for pids in tool_processes(self.root, self.names).values()))
        print(f"\nP1 holds: {len(self.names)} tool programs ({', '.join(self.names)}) start and report ready "
              "after thread/start and after thread/resume.")

    # P2 -------------------------------------------------------------------------------------------------------
    def test_p2_unsubscribe_unloads_and_stops_tool_programs(self) -> None:
        app = self.start(["-c", "thread_unload_delay_secs=0"])
        mark = app.mark()
        thread_id = self.thread(app)
        wait_ready(app, self.names, mark)
        self.model.scripts["p2"] = [{"final": "first"}]
        app.turn(thread_id, "[script:p2] one turn")
        pids = [pids[0] for pids in tool_processes(self.root, self.names).values()]
        mark = app.mark()
        app.request("thread/unsubscribe", {"threadId": thread_id})
        app.wait_for(lambda m: m.get("method") == "thread/closed"
                     and (m.get("params") or {}).get("threadId") == thread_id, mark)
        deadline = time.monotonic() + 10
        while any(pid_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse([pid for pid in pids if pid_alive(pid)], "tool programs still run after thread/closed")
        print("\nP2 holds: thread/unsubscribe with thread_unload_delay_secs=0 sends thread/closed and stops "
              f"all {len(pids)} tool programs.")

    # P3 -------------------------------------------------------------------------------------------------------
    def test_p3_paused_chat_wakes_with_its_history(self) -> None:
        app = self.start(["-c", "thread_unload_delay_secs=0"])
        thread_id = self.thread(app)
        self.model.scripts["p3a"] = [{"final": "remember the word heron"}]
        app.turn(thread_id, "[script:p3a] first turn")
        mark = app.mark()
        app.request("thread/unsubscribe", {"threadId": thread_id})
        app.wait_for(lambda m: m.get("method") == "thread/closed"
                     and (m.get("params") or {}).get("threadId") == thread_id, mark)
        mark = app.mark()
        resumed = app.request("thread/resume", {"threadId": thread_id, "cwd": str(self.root / "workspace")})
        self.assertEqual(resumed["thread"]["id"], thread_id)
        wait_ready(app, self.names, mark)
        turns = resumed["thread"].get("turns") or []
        self.assertGreaterEqual(len(turns), 1, resumed)
        self.model.scripts["p3b"] = [{"final": "second"}]
        app.turn(thread_id, "[script:p3b] second turn")
        seen = json.dumps(self.model.requests_for("p3b")[-1]["input"])
        self.assertIn("first turn", seen)
        self.assertIn("remember the word heron", seen)
        print(f"\nP3 holds: the resumed thread keeps its id, thread/resume returns {len(turns)} earlier turn(s), "
              "and the next request to the model holds the earlier turn.")

    # P4 -------------------------------------------------------------------------------------------------------
    def test_p4_turn_with_another_mode_starts_no_tool_program(self) -> None:
        app = self.start()
        mark = app.mark()
        thread_id = self.thread(app)  # Default: workspace-write, on-request, the person reviews
        wait_ready(app, self.names, mark)
        before = tool_processes(self.root, self.names)
        workspace = str(self.root / "workspace")
        modes = [
            ("Read only", {"sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                           "approvalPolicy": "on-request", "approvalsReviewer": "user"}),
            ("Auto-review", {"sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False,
                                               "writableRoots": [workspace]},
                             "approvalPolicy": "on-request", "approvalsReviewer": "auto_review"}),
            ("Default", {"sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False,
                                           "writableRoots": [workspace]},
                         "approvalPolicy": "on-request", "approvalsReviewer": "user"}),
        ]
        target = self.root / "workspace" / "p4.txt"
        for label, settings in modes:
            name = "p4" + label.replace(" ", "").replace("-", "")
            self.model.scripts[name] = [{"shell": f"echo {name} > {target}"}, {"final": label}]
            mark = app.mark()
            app.turn(thread_id, f"[script:{name}] mode {label}", **settings)
            self.assertFalse([n for n in app.notices("mcpServer/startupStatus/updated", mark)
                              if (n.get("params") or {}).get("status") == "starting"], label)
            self.assertEqual(tool_processes(self.root, self.names), before, label)
            if label == "Read only":
                self.assertFalse(target.exists(), "a Read only turn wrote a file")
            else:
                self.assertEqual(target.read_text().strip(), name, label)
            target.unlink(missing_ok=True)
        print("\nP4 holds: turns in Read only, Auto-review and Default on one thread apply each mode (Read only "
              "cannot write) and start no tool program again: no new starting notice, the same process ids.")

    # P5 -------------------------------------------------------------------------------------------------------
    def test_p5_times(self) -> None:
        cold = AppServer(self.root, self.model, None, self.tool_args)
        self.apps.append(cold)
        to_initialize = cold.initialize()
        mark = cold.mark()
        begin = time.monotonic()
        thread_id = self.thread(cold)
        start_to_ready = time.monotonic() - begin
        start_to_ready += wait_ready(cold, self.names, mark)
        self.model.scripts["p5"] = [{"final": "ok"}]
        for number in range(20):
            cold.turn(thread_id, f"[script:p5] turn {number}")
        cold.close()
        warm = self.start()
        mark = warm.mark()
        begin = time.monotonic()
        warm.request("thread/resume", {"threadId": thread_id, "cwd": str(self.root / "workspace")})
        resume_to_ready = time.monotonic() - begin
        resume_to_ready += wait_ready(warm, self.names, mark)
        print(f"\nP5 times: process start to initialize {to_initialize:.2f} s; thread/start to all tools ready "
              f"{start_to_ready:.2f} s; thread/resume of a 20-turn chat to all tools ready {resume_to_ready:.2f} s.")

    # P6 -------------------------------------------------------------------------------------------------------
    def test_p6_interrupt_completes_the_turn_and_the_thread_goes_on(self) -> None:
        app = self.start()
        thread_id = self.thread(app)
        release = threading.Event()
        self.model.scripts["p6a"] = [{"hold": release}, {"final": "late"}]
        mark = app.mark()
        turn = app.turn(thread_id, "[script:p6a] a long turn", wait=False)
        deadline = time.monotonic() + WAIT
        while not self.model.requests_for("p6a") and time.monotonic() < deadline:
            time.sleep(0.1)
        app.request("turn/interrupt", {"threadId": thread_id, "turnId": turn["id"]})
        done = app.wait_for(lambda m: m.get("method") == "turn/completed"
                            and m["params"]["turn"]["id"] == turn["id"], mark, timeout=10)
        release.set()
        status = done["params"]["turn"].get("status")
        self.assertEqual(status, "interrupted", done)
        self.model.scripts["p6b"] = [{"final": "after"}]
        after = app.turn(thread_id, "[script:p6b] the next turn")
        self.assertEqual(after["params"]["turn"].get("status"), "completed", after)
        print("\nP6 holds: after turn/interrupt, turn/completed arrives within 10 s with status interrupted, and "
              "the thread completes a new turn.")

    # P7 -------------------------------------------------------------------------------------------------------
    def test_p7_a_second_process_adds_a_turn_to_a_live_thread(self) -> None:
        here = self.start()
        thread_id = self.thread(here)
        self.model.scripts["p7a"] = [{"final": "one"}]
        here.turn(thread_id, "[script:p7a] here first")
        # Remote control is a second Codex process on the same Codex home folder.
        other = self.start(home=here.home, tools=False)
        answer = other.call("thread/resume", {"threadId": thread_id, "cwd": str(self.root / "workspace")})
        refused = "error" in answer
        if refused:
            outcome = f"refuses the resume: {answer['error'].get('message')}"
        else:
            self.model.scripts["p7b"] = [{"final": "two"}]
            other.turn(thread_id, "[script:p7b] from the other process")
            outcome = "accepts the resume and runs a turn"
        self.model.scripts["p7c"] = [{"final": "three"}]
        third = here.turn(thread_id, "[script:p7c] here again")
        self.assertEqual(third["params"]["turn"].get("status"), "completed", third)
        sees_other = "from the other process" in json.dumps(self.model.requests_for("p7c")[-1]["input"])
        # After the live process lets the thread go, the second process can take it, and the file is whole.
        here.request("thread/unsubscribe", {"threadId": thread_id})
        here.close()
        turns = other.request("thread/resume", {"threadId": thread_id,
                                                "cwd": str(self.root / "workspace")})["thread"].get("turns") or []
        self.assertGreaterEqual(len(turns), 2 if refused else 3, "the session file lost turns")
        print(f"\nP7 recorded: while the thread is live in one process, a second process on the same Codex home "
              f"{outcome}. The live process {'sees' if sees_other else 'does not see'} a foreign turn. After the "
              f"live process closes, the second process resumes the thread with {len(turns)} turns: the file is "
              "whole.")

    # P9 -------------------------------------------------------------------------------------------------------
    def every_tool_call_names_its_thread(self) -> list[str]:
        """Call one tool of each tool program in one turn; assert each call names the thread. Return the servers."""
        app = self.start()
        mark = app.mark()
        thread_id = self.thread(app)
        wait_ready(app, self.names, mark)
        # A new chat's id is known from the thread/start answer, before any tool call.
        self.assertFalse(tool_calls(self.root, self.names))
        steps: list[dict[str, Any]] = [
            {"tool": ("home_assistant", "verify"), "arguments": {"checks": []}},
            {"tool": ("home_assistant_data", "exchange_info"), "arguments": {}},
            {"tool": ("home_assistant_call", "call"), "arguments": {"domain": "light", "service": "turn_on"}},
            {"tool": ("codex_github", "github_git"), "arguments": {}},
        ]
        if "codex_scheduling" in self.names:
            steps.append({"tool": ("codex_scheduling", "list_schedules"), "arguments": {}})
        self.model.scripts["p9"] = [*steps, {"final": "called"}]
        app.turn(thread_id, "[script:p9] call each tool")
        calls = tool_calls(self.root, self.names)
        by_server = {name: params for name, params in calls}
        self.assertEqual(set(by_server), set(self.names), calls)
        for name, params in calls:
            self.assertEqual((params.get("_meta") or {}).get("threadId"), thread_id, (name, params))
        return sorted(by_server)

    def test_p9_every_tool_call_names_its_thread(self) -> None:
        servers = self.every_tool_call_names_its_thread()
        print(f"\nP9 holds: each of the {len(servers)} tool programs received _meta.threadId equal to the thread id "
              f"of thread/start ({servers}); scheduled runs are off by default, so the scheduling tool is not offered.")

    def test_p9_with_scheduled_runs_on_the_scheduling_tool_names_its_thread_too(self) -> None:
        """Review N2: with SCHEDULED_RUNS on, the worker starts a fifth tool program; it gets the thread id too."""
        import server

        with patch.object(server.scheduling, "SCHEDULED_RUNS", True):
            self.tool_args, self.names = worker_app_server_args(self.root)
        self.assertIn("codex_scheduling", self.names)
        self.assertEqual(len(self.names), 5, self.names)
        servers = self.every_tool_call_names_its_thread()
        print(f"\nP9 holds with scheduled runs on: each of the {len(servers)} tool programs received "
              f"_meta.threadId ({servers}).")

    def test_p9_a_sub_agent_names_its_own_thread(self) -> None:
        """Review F2 of the design: a sub-agent is its own thread, so its tool calls carry its own id."""
        app = self.start()
        thread_id = self.thread(app)
        self.model.scripts["p9sub"] = [{"spawn": "[script:p9child] read the exchange info"}, {"final": "parent"}]
        self.model.scripts["p9child"] = [{"tool": ("home_assistant_data", "exchange_info"), "arguments": {}},
                                         {"final": "child"}]
        mark = app.mark()
        parent_turn = app.turn(thread_id, "[script:p9sub] start a helper")
        parent_done = app.messages.index(parent_turn)
        call = app.wait_for(lambda m: m.get("method") == "item/completed"
                            and m["params"]["item"].get("type") == "mcpToolCall", mark)
        child_id = call["params"]["threadId"]
        spawned = [m["params"]["item"] for m in app.notices("item/completed", mark)
                   if m["params"]["item"].get("type") == "collabAgentToolCall"]
        calls = tool_calls(self.root, self.names)
        self.assertEqual([(params.get("_meta") or {}).get("threadId") for _name, params in calls], [child_id])
        self.assertNotEqual(child_id, thread_id, "the sub-agent's call named the parent thread")
        # The parent's spawn item is the only link from the child's id to the chat.
        self.assertEqual([(item["senderThreadId"], item["receiverThreadIds"]) for item in spawned],
                         [(thread_id, [child_id])])
        child_turn = app.wait_for(lambda m: m.get("method") == "turn/started"
                                  and m["params"]["threadId"] == child_id, mark)
        after_parent = app.messages.index(call) > parent_done
        print(f"\nP9 (sub-agent) recorded: the sub-agent's tool call names its own thread, not the chat's; the "
              "parent's collabAgentToolCall item (senderThreadId, receiverThreadIds) links the two, and the "
              f"child's turn has rootTurnId {'equal to' if child_turn['params']['turn'].get('rootTurnId') == parent_turn['params']['turn']['id'] else 'unlike'} "
              f"the parent's turn. The child's tool call came {'after' if after_parent else 'before'} the "
              "parent's turn/completed. Its tool programs start lazily, only the one it used.")

    # P10 ------------------------------------------------------------------------------------------------------
    def test_p10_two_threads_run_at_once_and_interrupt_one(self) -> None:
        app = self.start()
        mark = app.mark()
        first = self.thread(app)
        second = self.thread(app)
        wait_ready(app, self.names, mark)
        release = threading.Event()
        self.model.scripts["p10a"] = [{"hold": release}, {"final": "first done"}]
        self.model.scripts["p10b"] = [{"hold": release}, {"final": "second done"}]
        mark = app.mark()
        turn_a = app.turn(first, "[script:p10a] first chat", wait=False)
        turn_b = app.turn(second, "[script:p10b] second chat", wait=False)
        deadline = time.monotonic() + WAIT
        while self.model.most_in_flight < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.model.most_in_flight, 2, "the two turns did not reach the model at the same time")
        app.request("turn/interrupt", {"threadId": first, "turnId": turn_a["id"]})
        done_a = app.wait_for(lambda m: m.get("method") == "turn/completed"
                              and m["params"]["turn"]["id"] == turn_a["id"], mark, timeout=10)
        release.set()
        done_b = app.wait_for(lambda m: m.get("method") == "turn/completed"
                              and m["params"]["turn"]["id"] == turn_b["id"], mark)
        self.assertEqual(done_a["params"]["turn"]["status"], "interrupted")
        self.assertEqual(done_b["params"]["turn"]["status"], "completed")
        unnamed = [m["method"] for m in app.messages[mark:]
                   if m.get("method", "").startswith(("turn/", "item/")) and "id" not in m
                   and (m.get("params") or {}).get("threadId") not in {first, second}]
        self.assertFalse(unnamed, unnamed)
        programs = tool_processes(self.root, self.names)
        self.assertTrue(all(len(pids) == 2 for pids in programs.values()), programs)
        print("\nP10 holds: two threads of one process were at the model at the same time; every turn and item "
              "notice names its thread; each thread has its own tool programs (2 of each); turn/interrupt of one "
              "left the other to complete.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
