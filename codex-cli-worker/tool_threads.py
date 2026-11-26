"""Which chat a tool request belongs to: the thread id that Codex puts in each tool call.

Codex adds the thread id of the calling chat to the `_meta` of every `tools/call` request it sends to a tool
program (`_meta.threadId`, codex-rs/core/src/mcp_tool_call.rs at rust-v0.166.0). The model writes a call's
arguments, but not its `_meta`. Each tool program passes that id on to the worker, and the worker looks it up
here. The id replaces the per-exchange keys of earlier versions; AUDITING.md says why the sandbox makes the
keys unnecessary.

A chat's own thread is bound when its exchange starts or when Codex reports the new thread. A sub-agent that
Codex starts for the chat is its own thread, and its tool calls name that thread, so it is bound to the same
chat when the chat's completed spawn item names it. Only a spawn counts: in the other collab tools the model
writes the target thread. A thread that one chat holds is never bound to another. Every binding of a chat goes
when its exchange ends.
"""

import re
import threading

# Codex thread ids are UUIDs. Anything else is refused before the lookup.
THREAD_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def valid_thread_id(value: object) -> str | None:
    """Return the value when it has the form of a Codex thread id, else None."""
    if isinstance(value, str) and THREAD_ID_RE.fullmatch(value):
        return value
    return None


def thread_id_from_call(params: object) -> str:
    """Return the thread id that Codex put in a tools/call request, or "" when there is none.

    Only `params._meta.threadId` counts. A `threadId` among the arguments is the model's text and is ignored.
    """
    meta = params.get("_meta") if isinstance(params, dict) else None
    value = meta.get("threadId") if isinstance(meta, dict) else None
    return value if isinstance(value, str) else ""


class ChatThreads:
    """The thread ids of the chats whose exchange runs now, each mapped to its task."""

    def __init__(self) -> None:
        """Start with no thread bound."""
        self.lock = threading.Lock()
        # Thread id -> task id.
        self.tasks: dict[str, str] = {}

    def bind(self, task_id: str, thread_id: object) -> bool:
        """Bind a thread to the task. Return False for a bad id or a thread that another task holds.

        The first task to bind a thread keeps it until its exchange ends; the same task may bind it again. Only
        the worker binds, from its own exchange start, from the thread that Codex reports for that exchange, and
        from the chat's completed spawn items.
        """
        thread_id = valid_thread_id(thread_id)
        if thread_id is None or not task_id:
            return False
        with self.lock:
            if self.tasks.setdefault(thread_id, task_id) != task_id:
                return False
        return True

    def release(self, task_id: str) -> None:
        """Remove every thread of the task, its own and its sub-agents'."""
        with self.lock:
            for thread_id in [key for key, value in self.tasks.items() if value == task_id]:
                del self.tasks[thread_id]

    def task_for(self, thread_id: object) -> str | None:
        """Return the task that the thread is bound to, or None."""
        thread_id = valid_thread_id(thread_id)
        if thread_id is None:
            return None
        with self.lock:
            return self.tasks.get(thread_id)

    def threads_of(self, task_id: str) -> list[str]:
        """Return the threads bound to the task, sorted."""
        with self.lock:
            return sorted(key for key, value in self.tasks.items() if value == task_id)


# The one map of the worker. The modules that answer tool requests read it; run_task writes it.
CHATS = ChatThreads()
