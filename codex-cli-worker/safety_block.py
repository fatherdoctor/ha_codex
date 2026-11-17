"""Recognize the pinned app-server's typed safety error, without reading model prose."""

from __future__ import annotations

from typing import Any

from approvals import AppServerSession


SUMMARY = "OpenAI's safety filter blocked this task."
NEXT_STEP = "Rephrase the request or retry it. Normal use needs no special access."
MESSAGE = f"{SUMMARY} {NEXT_STEP}"


def is_safety_block(message: Any, thread_id: str, turn_id: str) -> bool:
    """Match CyberPolicy in rust-v0.166.0's ErrorNotification or failed Turn.

    The app-server's TurnError preserves codexErrorInfo; exec JSONL drops it.
    Never classify a message-only error or an assistant's answer as this signal.
    """
    if not thread_id or not turn_id or not isinstance(message, dict) or "id" in message:
        return False
    params = message.get("params")
    if not isinstance(params, dict) or params.get("threadId") != thread_id:
        return False
    if message.get("method") == "error":
        if params.get("turnId") != turn_id or type(params.get("willRetry")) is not bool:
            return False
        error = params.get("error")
    elif message.get("method") == "turn/completed":
        turn = params.get("turn")
        if not isinstance(turn, dict) or turn.get("id") != turn_id or turn.get("status") != "failed":
            return False
        error = turn.get("error")
    else:
        return False
    return isinstance(error, dict) and error.get("codexErrorInfo") == "cyberPolicy"


class SafetyBlockSession(AppServerSession):
    """Stop one worker exchange when the runtime reports its safety block."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.safety_blocked = False

    def receive(self, message: dict[str, Any]) -> None:
        """Keep the normal transport for other messages; never retry a block."""
        with self.lock:
            if self._closed:
                return
            if self.is_active() and is_safety_block(message, self.thread_id, self.turn_id):
                self.safety_blocked = True
                self.fail(MESSAGE)
                return
            super().receive(message)
