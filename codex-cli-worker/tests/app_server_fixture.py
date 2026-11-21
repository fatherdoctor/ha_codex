"""A stand-in for `codex app-server` that answers the worker over JSON-RPC, for tests that run whole exchanges.

Modeled on upstream's tests/app_server_fixture.py (moryoav/home-assistant-codex 0.1.82, MIT). This copy also
keeps every request the worker sent, so a test can check the thread and turn settings it asked for.
"""

import io
import json
from queue import Queue
from threading import Event
from typing import Any, Callable

SANDBOX_TYPES = {"read-only": "readOnly", "workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"}


class StubAppServer:
    """Answer the worker's requests and send the items of one exchange.

    session_id is the thread the stub reports for thread/start and thread/resume. final is the answer object
    Codex gives, sent as the final agentMessage, or None for a turn without an answer. items are sent as
    item/completed before it. on_turn runs when the turn starts, as Codex would edit files then. hold keeps the
    turn open until the worker stops it. turn_error ends the turn as failed with that message. failure="eof"
    closes stdout right after the turn starts, as a process that dies would.
    """

    turn_id = "fixture-turn"

    def __init__(self, session_id: str, final: dict[str, Any] | None = None, *, items=(),
                 on_turn: Callable[[], None] | None = None, hold: bool = False,
                 turn_error: str | None = None, failure: str | None = None, stderr: str = "") -> None:
        self.session_id = session_id
        self.final = final
        self.items = items
        self.on_turn = on_turn
        self.hold = hold
        self.turn_error = turn_error
        self.failure = failure
        self.messages: Queue[str] = Queue()
        self.requests: list[dict[str, Any]] = []
        self.started = Event()
        self.stdin = self.stdout = self
        self.stderr = io.StringIO(stderr)
        self.returncode: int | None = None
        self.closed = False

    def params(self, method: str) -> dict[str, Any]:
        """Return the params of the first request the worker sent with this method."""
        for message in self.requests:
            if message.get("method") == method:
                return message.get("params") or {}
        raise AssertionError(f"the worker sent no {method} request")

    def emit(self, **message: Any) -> None:
        """Queue one line for the worker's reader."""
        self.messages.put(json.dumps(message) + "\n")

    def write(self, line: str) -> int:
        """Take one request from the worker and answer it as the app server does."""
        message = json.loads(line)
        self.requests.append(message)
        method = message.get("method")
        if method == "initialize":
            self.emit(id=message["id"], result={})
        elif method in {"thread/start", "thread/resume"}:
            self.emit(id=message["id"], result={
                "thread": {"id": self.session_id}, "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "sandbox": {"type": SANDBOX_TYPES[message["params"]["sandbox"]]},
            })
        elif method == "turn/start":
            self.start_turn(message)
        return len(line)

    def start_turn(self, message: dict[str, Any]) -> None:
        """Answer turn/start, then send the exchange's items, its answer and the end of the turn."""
        turn = {"id": self.turn_id}
        self.emit(id=message["id"], result={"turn": turn})
        self.emit(method="turn/started", params={"threadId": self.session_id, "turn": turn})
        if self.on_turn:
            self.on_turn()
        self.started.set()
        if self.failure == "eof":
            self.close()
            return
        if self.hold:
            return
        context = {"threadId": self.session_id, "turnId": self.turn_id}
        for item in self.items:
            self.emit(method="item/completed", params={**context, "item": item})
        if self.final is not None:
            self.emit(method="item/completed", params={**context, "item": {
                "id": "final", "type": "agentMessage", "phase": "final_answer", "text": json.dumps(self.final),
            }})
        outcome: dict[str, Any] = {**turn, "status": "completed"}
        if self.turn_error:
            outcome.update(status="failed", error={"message": self.turn_error})
        self.emit(method="turn/completed", params={"threadId": self.session_id, "turn": outcome})

    def readline(self) -> str:
        """Return the next line for the worker, or "" once the stub is closed."""
        return self.messages.get(timeout=10)

    def flush(self) -> None:
        """Nothing is buffered."""

    def close(self) -> None:
        """End stdout, as the app server does when its stdin closes."""
        if not self.closed:
            self.closed = True
            self.messages.put("")

    def poll(self) -> int | None:
        """Return the exit code, or None while the stub runs."""
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Exit cleanly, unless the stub was stopped."""
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        """Stop the stub as a signal would."""
        self.returncode = -15
        self.close()

    kill = terminate
