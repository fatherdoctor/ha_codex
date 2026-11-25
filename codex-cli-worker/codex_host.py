"""Keep one `codex app-server` process alive and run each chat as a thread in it.

This module is the host of that process. The worker asks it first for each exchange (server.py,
run_live_exchange) while the owner's warm_start option is on, and starts a Codex process for the exchange, as
before, when the host cannot take it.

What the host does:

- It starts the process with `thread_unload_delay_secs=0` and sends `initialize` once.
- A chat is live (its thread is loaded, with its tool programs) or paused (unloaded; its history stays in its session
  file). `wake` loads a chat with `thread/start` or `thread/resume` and waits until each named tool program is ready.
  `pause` sends `thread/unsubscribe` and waits for `thread/closed`.
- At most `LIVE_CHATS_MAX` chats are live. To wake one more, the host pauses the idle live chat that was used longest
  ago, and the selected chat last. A chat with a running exchange is never paused. When every live chat works, `wake`
  raises `AllChatsBusy` and the caller keeps the message in its queue.
- One reader thread reads the process output for its whole life and gives each line to one place, by the thread id
  in the line (see `_route`). A Codex sub-agent is its own thread; the host maps it to its chat through the parent's
  `collabAgentToolCall` item.
- A fingerprint of everything that reaches Codex at its start is compared at each message (`ready_for_message`).
  When it differs, a restart is due. A due restart waits until no exchange runs; new messages wait in the queue.
- A crash (the end of the output, or no reply to a host request in time) fails every running exchange and starts a
  new process. After `CRASH_LIMIT` crashes in `CRASH_WINDOW` seconds the host starts none for `CRASH_PAUSE` seconds.
- Before a turn, the host compares the chat's session file with what it recorded at the end of the last exchange.
  When another process (remote control) wrote to it, the host pauses and wakes the chat, so Codex reads it again.

The host never decides what Codex may do. The permission mode, the sandbox and the approval questions belong to the
turn and to approvals.AppServerSession, as on the start path.
"""

from __future__ import annotations

import collections
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any

# The largest number of live chats. Each live chat holds its own tool programs in memory, so this is the one
# resource rule of the host. Every use of the value goes through CodexHost.live_max.
LIVE_CHATS_MAX = 3

# Added to the process arguments: a thread unloads as soon as the host unsubscribes from it.
UNLOAD_ARGS = ["--config", "thread_unload_delay_secs=0"]

REPLY_TIMEOUT = 30.0  # No reply to a host request in this time counts as a crash.
READY_TIMEOUT = (
    60.0  # A woken chat's tool programs must be ready in this time, or the wake fails.
)
INTERRUPT_TIMEOUT = (
    10.0  # A canceled turn must complete in this time, or a restart is due.
)
THREAD_JOIN_TIMEOUT = (
    2.0  # How long a stop waits for the reader threads of the process it reaped.
)
CRASH_LIMIT = 3
CRASH_WINDOW = 600.0
CRASH_PAUSE = 3600.0
STDERR_LINES = 50

UNSUPPORTED = {"code": -32601, "message": "Unsupported worker request"}


class HostError(RuntimeError):
    """The host could not do what was asked. The caller uses the start path of today instead."""


class AllChatsBusy(HostError):
    """Every live chat has a running exchange, so no chat can be paused to make room."""


def fingerprint(rows: dict[str, Any]) -> dict[str, str]:
    """Hash each row of the fingerprint on its own, so a change can be named without its value."""
    return {
        name: hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()
        ).hexdigest()
        for name, value in rows.items()
    }


def changed_rows(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Return the names of the rows whose hash differs, or that only one side has, in sorted order."""
    return sorted(
        name for name in set(old) | set(new) if old.get(name) != new.get(name)
    )


def file_signature(path: Path | None) -> tuple[int, int] | None:
    """Return the size and change time of a file, or None when there is no file."""
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


@dataclass
class Chat:
    """What the host knows about one chat."""

    chat_id: str
    thread_id: str = ""
    live: bool = False
    last_used: float = 0.0
    session: Any = None  # The AppServerSession of the running exchange, or None.
    thread_params: dict[str, Any] = field(default_factory=dict)
    tool_names: tuple[str, ...] = ()
    file_signature: tuple[int, int] | None = None
    turn_running: bool = False  # Set by turn/started, cleared by turn/completed, for this chat's own thread.
    turn_done: threading.Event = field(default_factory=threading.Event)

    @property
    def working(self) -> bool:
        """A chat works while an exchange is attached or its turn has not completed."""
        return self.session is not None or self.turn_running


@dataclass
class _Taken:
    """A process that a stop or a crash took from the host, with what must be ended outside the lock."""

    process: Any
    sessions: list[Any]
    readers: list[threading.Thread]


class CodexHost:
    """One long-lived app-server process and the chats that live in it.

    Arguments are what the worker supplies; the host imports nothing from server.py:

    - spawn(args, env) starts the process and returns it, with text stdin, stdout and stderr.
    - reap(process) stops it and waits for it (server.terminate_and_reap_process).
    - args() and env() give the process arguments and environment (build_app_server_args, codex_env).
    - fingerprint_rows() gives the raw rows of the fingerprint: the environment, the arguments, the sign-in, the
      versions, the rule files. A row whose value is None means "signed out" only for the row named "sign_in".
    - enabled() is the owner's switch. Off: the host starts nothing, and every call says so.
    - session_file(thread_id) gives the path of the thread's session file, or None.
    - redact(text) hides credentials in text that goes to a log.
    - log(line) writes one line to the worker log. The host never logs a fingerprint value.
    - lock is the worker's reentrant lock, the one that AppServerSession uses.

    A caller of wake, pause, select, attach, detach, cancel, ready_for_message, stop or close must not hold that
    lock: these methods wait for replies that the reader thread delivers, and the reader needs the lock to do it.
    """

    def __init__(
        self,
        *,
        spawn: Callable[[list[str], dict[str, str]], Any],
        reap: Callable[[Any], Any],
        args: Callable[[], list[str]],
        env: Callable[[], dict[str, str]],
        fingerprint_rows: Callable[[], dict[str, Any]],
        enabled: Callable[[], bool],
        session_file: Callable[[str], Path | None],
        redact: Callable[[str], str],
        log: Callable[[str], None],
        lock: Any,
        live_max: int = LIVE_CHATS_MAX,
        clock: Callable[[], float] = time.monotonic,
        reply_timeout: float = REPLY_TIMEOUT,
        ready_timeout: float = READY_TIMEOUT,
    ) -> None:
        self.spawn = spawn
        self.reap = reap
        self.args = args
        self.env = env
        self.fingerprint_rows = fingerprint_rows
        self.enabled = enabled
        self.session_file = session_file
        self.redact = redact
        self.log = log
        self.lock = lock
        self.live_max = live_max
        self.clock = clock
        self.reply_timeout = reply_timeout
        self.ready_timeout = ready_timeout
        self.process: Any = None
        self.writer: Any = None
        self.restart_due = False
        self.selected = ""
        self.chats: dict[str, Chat] = {}
        self.stderr_tail: collections.deque[str] = collections.deque(
            maxlen=STDERR_LINES
        )
        self._stderr_count = 0  # Lines read from the error output so far, so an exchange copies only its own.
        self._fingerprint: dict[str, str] = {}
        self._threads: dict[
            str, str
        ] = {}  # thread id -> chat id, for each chat's thread and its sub-agents
        self._replies: dict[str, dict[str, Any]] = {}
        self._waiting: dict[str, threading.Event] = {}
        self._next_id = 0
        self._generation = (
            0  # Counts processes, so a reader of an old process cannot touch a new one.
        )
        self._crashes: list[float] = []
        self._paused_until = 0.0
        # The status of each tool program by (thread id or None, name), from the notices since the last wake began.
        self._tool_status: dict[tuple[str | None, str], str] = {}
        self._closed_threads: set[str] = set()
        self._readers: list[
            threading.Thread
        ] = []  # The two reader threads of the current process.
        self._restart_thread: threading.Thread | None = None
        self._closed = False  # Set by close(): no process starts again.
        self.stop_due = ""  # The reason of a stop that waits for the running exchanges (switch off, sign-out).
        self._notice = threading.Condition(self.lock)
        self._wake_lock = (
            threading.Lock()
        )  # One wake or pause at a time, so ready notices belong to it.

    # The process ---------------------------------------------------------------------------------------------

    def running(self) -> bool:
        """Return True while a process is up."""
        with self.lock:
            return self.process is not None

    def ready_for_message(self) -> bool:
        """Compare the fingerprint, restart when due and possible, and say whether a new exchange may start here.

        False means: use the start path of today, or (when a restart is due) keep the message in the queue. The
        caller tells the two apart with `restart_due`. A restart, the owner switch off and a sign-out never cut a
        running exchange off: the process goes away only when no chat works, and the check and the stop are one
        step under the lock, so an exchange cannot start between them.
        """
        if self._closed:
            return False
        if not self.enabled():
            return self._stop_when_idle("the owner switch is off")
        rows = self.fingerprint_rows()
        if rows.get("sign_in") is None:
            return self._stop_when_idle("signed out")
        taken = None
        with self.lock:
            self.stop_due = ""
            if self.clock() < self._paused_until:
                return False
            new = fingerprint(rows)
            if self.process is not None and new != self._fingerprint:
                names = changed_rows(self._fingerprint, new)
                self.log(f"Codex host: restart due, changed: {', '.join(names)}.")
                self.restart_due = True
            if self.process is not None and not self.restart_due:
                return True
            if self.process is not None:
                if self._anyone_working():
                    return False
                taken = self._take_process()
        if taken is not None:
            self._finish_stop(taken, "restart")
        return self._start(new)

    def _stop_when_idle(self, reason: str) -> bool:
        """Stop the process now when no chat works, else when the last exchange ends (detach). Return False."""
        with self.lock:
            if self.process is None:
                self.stop_due = ""
                return False
            if self._anyone_working():
                self.stop_due = reason
                return False
            self.stop_due = ""
            taken = self._take_process()
        self._finish_stop(taken, reason)
        return False

    def _anyone_working(self) -> bool:
        """True while any chat has a running exchange. Lock held."""
        return any(chat.working for chat in self.chats.values())

    def _start(self, new_fingerprint: dict[str, str]) -> bool:
        """Start a process and initialize it. On failure, stop that process again and return False."""
        with self.lock:
            if self._closed:
                return False
            if self.process is not None:
                return True
            try:
                process = self.spawn([*self.args(), *UNLOAD_ARGS], self.env())
            except OSError as exc:
                self.log(self.redact(f"Codex host: the process did not start: {exc}"))
                return False
            self._generation += 1
            generation = self._generation
            self.process = process
            self.writer = process.stdin
            self._fingerprint = new_fingerprint
            self.restart_due = False
            self._readers = [
                threading.Thread(
                    target=self._read,
                    args=(process.stdout, generation),
                    daemon=True,
                    name="codex-host-reader",
                ),
                threading.Thread(
                    target=self._read_errors,
                    args=(process.stderr,),
                    daemon=True,
                    name="codex-host-stderr",
                ),
            ]
            for reader in self._readers:
                reader.start()
        try:
            self._call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "home_assistant_worker",
                        "title": "Home Assistant",
                        "version": "1",
                    }
                },
            )
            with self.lock:
                self._write({"method": "initialized"})
        except (HostError, OSError, ValueError) as exc:
            # Only this start's process: a crash in _call may already have started the next one.
            self.stop(f"initialize failed: {exc}", generation=generation)
            return False
        self.log("Codex host: the process started.")
        self._wake_selected()
        return True

    def stop(self, reason: str, generation: int | None = None) -> None:
        """Stop the process at once. Every chat is paused, and a running exchange is failed.

        The routine stops (restart, switch off, sign-out) go through ready_for_message, which waits for work. This
        is for a failed start and for close(). With a generation, it stops only the process of that generation.
        """
        with self.lock:
            if self.process is None or generation not in (None, self._generation):
                return
            taken = self._take_process()
        self._finish_stop(taken, reason)

    def close(self) -> None:
        """Stop for good, as the worker shuts down: no process starts after this."""
        with self.lock:
            self._closed = True
            restart = self._restart_thread
        if restart is not None and restart is not threading.current_thread():
            restart.join(THREAD_JOIN_TIMEOUT)
        self.stop("the worker stops")

    def _take_process(self) -> _Taken:
        """Take the process away from the host: every chat is paused, every waiter wakes. Lock held."""
        taken = _Taken(self.process, self._forget_all(), self._readers)
        self.process = None
        self.writer = None
        self._readers = []
        return taken

    def _finish_stop(
        self,
        taken: _Taken,
        reason: str | None,
        failure: str = "The Codex process stopped before the exchange finished.",
    ) -> None:
        """Fail the taken sessions, reap the process and wait a short time for its readers; log unless reason is None.

        The lock is not held. A reader that calls this (the end of the output) does not wait for itself.
        """
        for session in taken.sessions:
            session.fail(failure)
        self.reap(taken.process)
        for reader in taken.readers:
            if reader is not threading.current_thread():
                reader.join(THREAD_JOIN_TIMEOUT)
        if reason is not None:
            self.log(f"Codex host: the process stopped ({reason}).")

    def _forget_all(self) -> list[Any]:
        """Mark every chat paused and wake every waiter; return the sessions that were attached. Lock held."""
        sessions = [
            chat.session for chat in self.chats.values() if chat.session is not None
        ]
        for chat in self.chats.values():
            chat.live = False
            chat.session = None
            chat.turn_running = False
            chat.turn_done.set()
        self._threads = {
            chat.thread_id: chat.chat_id
            for chat in self.chats.values()
            if chat.thread_id
        }
        for event in self._waiting.values():
            event.set()
        self._notice.notify_all()
        return sessions

    def _crash(self, generation: int, reason: str) -> None:
        """Fail each running exchange, reap the process, and start a new one unless crashes repeat."""
        with self.lock:
            if generation != self._generation or self.process is None:
                return
            taken = self._take_process()
            now = self.clock()
            self._crashes = [
                moment for moment in self._crashes if now - moment < CRASH_WINDOW
            ] + [now]
            too_many = len(self._crashes) >= CRASH_LIMIT
            if too_many:
                self._paused_until = now + CRASH_PAUSE
                self._crashes = []
        self._finish_stop(
            taken, None, failure="Codex stopped before the exchange finished."
        )
        if too_many:
            self.log(
                f"Codex host: {CRASH_LIMIT} crashes in {int(CRASH_WINDOW // 60)} minutes ({reason}); no live "
                f"process for {int(CRASH_PAUSE // 60)} minutes. Each message starts its own Codex process."
            )
            return
        self.log(f"Codex host: the process crashed ({reason}); starting a new one.")
        with self.lock:
            if self._closed:
                return
            self._restart_thread = threading.Thread(
                target=self.ready_for_message, daemon=True, name="codex-host-restart"
            )
            self._restart_thread.start()

    # Requests of the host ------------------------------------------------------------------------------------

    def _write(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC line. Lock held."""
        if self.writer is None:
            raise OSError("no Codex process")
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request of the host and wait for its reply. No reply in time is a crash."""
        with self.lock:
            generation = self._generation
            self._next_id += 1
            request_id = f"host-{self._next_id}"
            event = self._waiting[request_id] = threading.Event()
            try:
                self._write({"id": request_id, "method": method, "params": params})
            except (OSError, ValueError) as exc:
                self._waiting.pop(request_id, None)
                raise HostError(f"{method}: {exc}") from None
        answered = event.wait(self.reply_timeout)
        with self.lock:
            self._waiting.pop(request_id, None)
            reply = self._replies.pop(request_id, None)
        if reply is None:
            if not answered:
                self._crash(generation, f"no reply to {method}")
            raise HostError(f"{method}: no reply")
        if reply.get("error"):
            raise HostError(
                f"{method}: {self.redact(str(reply['error'].get('message') or 'failed'))}"
            )
        return reply.get("result") or {}

    # Chats ----------------------------------------------------------------------------------------------------

    def chat(self, chat_id: str) -> Chat:
        """Return the record of a chat, made on first use."""
        with self.lock:
            return self.chats.setdefault(chat_id, Chat(chat_id))

    def live_chats(self) -> list[str]:
        """Return the ids of the live chats."""
        with self.lock:
            return sorted(chat.chat_id for chat in self.chats.values() if chat.live)

    def chat_for_thread(self, thread_id: Any) -> str:
        """Return the chat a thread belongs to (its own thread or a sub-agent's), or an empty string."""
        with self.lock:
            return (
                self._threads.get(thread_id, "") if isinstance(thread_id, str) else ""
            )

    def select(
        self, chat_id: str, thread_params: dict[str, Any], tool_names: list[str]
    ) -> bool:
        """A person opened a chat: remember the selection and wake the chat.

        When every live chat works, the chat stays paused and wakes at its next message, which waits in the queue
        (False). A failed wake is also False; the message then takes the start path.
        """
        with self.lock:
            self.selected = chat_id
        try:
            self.wake(chat_id, thread_params, tool_names)
        except HostError:
            return False
        return True

    def _wake_selected(self) -> None:
        """After a start or restart, wake the selected chat at once, when the host knows its thread."""
        with self.lock:
            chat = self.chats.get(self.selected)
            if chat is None or not chat.thread_id or not chat.tool_names:
                return
            params, names = dict(chat.thread_params), list(chat.tool_names)
        try:
            self.wake(chat.chat_id, params, names)
        except HostError as exc:
            self.log(self.redact(f"Codex host: the selected chat did not wake: {exc}"))

    def wake(
        self, chat_id: str, thread_params: dict[str, Any], tool_names: list[str]
    ) -> str:
        """Make a chat live and return its thread id. Raise AllChatsBusy or HostError when that cannot be done.

        thread_params are those of thread/start, or of thread/resume when the chat has a thread. tool_names are the
        tool programs that must be ready before the chat is live. A wake holds the wake lock for up to
        reply_timeout (the thread request) plus ready_timeout (the tool programs), and longer when it first pauses
        another chat, so a page request that calls it (select) can wait that long.

        When the thread loaded but its tool programs failed or were not ready in time, the thread is paused again
        before the error is raised, so that every thread that holds tool programs is a live chat inside the cap.
        """
        with self._wake_lock:
            with self.lock:
                if self.process is None:
                    raise HostError("no Codex process")
                generation = self._generation
                chat = self.chats.setdefault(chat_id, Chat(chat_id))
                chat.last_used = self.clock()
                if chat.live:
                    return chat.thread_id
                victim = self._victim_for(chat_id)
            if victim:
                self._pause_locked_out(victim)
            with self.lock:
                self._tool_status = {}
                params = dict(thread_params)
                if chat.thread_id:
                    params["threadId"] = chat.thread_id
                method = "thread/resume" if params.get("threadId") else "thread/start"
            result = self._call(method, params)
            thread_id = (result.get("thread") or {}).get("id")
            if (
                not isinstance(thread_id, str)
                or not thread_id
                or (params.get("threadId") not in (None, thread_id))
            ):
                # Codex may now hold a thread that no chat owns; a fresh process frees it.
                with self.lock:
                    if self.process is not None:
                        self.restart_due = True
                self.log(
                    "Codex host: Codex gave another thread than the one asked for; restart due."
                )
                raise HostError(f"{method}: Codex gave another thread")
            with self.lock:
                chat.thread_id = thread_id
                chat.thread_params = {
                    key: value for key, value in params.items() if key != "threadId"
                }
                chat.tool_names = tuple(tool_names)
                self._threads[thread_id] = chat_id
                self._closed_threads.discard(thread_id)

                def settled() -> bool:
                    failed, waiting = self._tool_report(tool_names, thread_id)
                    return (
                        self._generation != generation
                        or self.process is None
                        or bool(failed)
                        or not waiting
                    )

                self._notice.wait_for(settled, self.ready_timeout)
                if self._generation != generation or self.process is None:
                    raise HostError("the Codex process stopped while the chat woke")
                failed, waiting = self._tool_report(tool_names, thread_id)
                if not failed and not waiting:
                    chat.live = True
                    chat.file_signature = file_signature(self.session_file(thread_id))
                    return thread_id
            self._unload_after_failed_wake(chat_id, thread_id)
            if failed:
                raise HostError("tool programs failed: " + ", ".join(failed))
            raise HostError("tool programs not ready in time: " + ", ".join(waiting))

    def _tool_report(
        self, names: list[str], thread_id: str
    ) -> tuple[list[str], list[str]]:
        """Return the named tool programs that failed and those not ready yet, for one thread. Lock held.

        A notice counts when it names this thread or names no thread. A notice of any other thread (a sub-agent
        that the host does not know yet, or another chat) is not about this wake.
        """
        failed, waiting = [], []
        for name in names:
            status = self._tool_status.get(
                (thread_id, name), self._tool_status.get((None, name))
            )
            if status == "ready":
                continue
            if status in (None, "starting"):
                waiting.append(name)
            else:
                failed.append(name)
        return sorted(failed), sorted(waiting)

    def _unload_after_failed_wake(self, chat_id: str, thread_id: str) -> None:
        """Unload a thread that loaded but did not become a live chat; when that fails, a restart is due."""
        try:
            closed = self._unload(thread_id)
        except HostError:
            closed = False
        with self.lock:
            self._forget_sub_agents(chat_id, thread_id)
            if not closed and self.process is not None:
                self.restart_due = True
                self.log(
                    "Codex host: a chat that did not wake did not unload; restart due."
                )

    def _victim_for(self, chat_id: str) -> str:
        """Return the chat to pause so that one more can be live, or "" when there is room. Lock held.

        The idle live chat used longest ago goes first, the selected chat last, a working chat never.
        """
        live = [
            chat
            for chat in self.chats.values()
            if chat.live and chat.chat_id != chat_id
        ]
        if len(live) < self.live_max:
            return ""
        idle = [chat for chat in live if not chat.working]
        if not idle:
            raise AllChatsBusy("every live chat is working")
        idle.sort(key=lambda chat: (chat.chat_id == self.selected, chat.last_used))
        return idle[0].chat_id

    def follow(self, chat_id: str, thread_id: str | None) -> None:
        """Let go of a thread the host holds for a chat when it is not the chat's conversation (thread_id).

        That happens after a wake that loaded a thread and then failed, or a cancel while a new chat loaded: the
        message then ran without the live process, so the chat went on in another thread. The host's thread is
        unloaded when it is live, and forgotten, so the next wake resumes the chat's own thread. Raise HostError
        when that thread is working or does not unload.
        """
        with self._wake_lock:
            with self.lock:
                chat = self.chats.get(chat_id)
                if chat is None or not chat.thread_id or chat.thread_id == thread_id:
                    return
                old = chat.thread_id
            self.log(
                "Codex host: a chat went on in another thread; letting go of the one loaded here."
            )
            self._pause_locked_out(chat_id)
            with self.lock:
                self._threads.pop(old, None)
                chat.thread_id = ""
                chat.thread_params = {}
                chat.tool_names = ()
                chat.file_signature = None

    def pause(self, chat_id: str) -> None:
        """Unload a live chat that does not work. Its history stays in its session file."""
        with self._wake_lock:
            self._pause_locked_out(chat_id)

    def _pause_locked_out(self, chat_id: str) -> None:
        """Pause a chat; the caller holds the wake lock, not the worker lock.

        The chat stops being live in the same step as the check that it does not work, before the unload lets go
        of the lock. So an exchange that arrives during the unload finds the chat paused (attach refuses it) and
        takes the queue or the start path; it never runs on a thread that Codex unloads.
        """
        with self.lock:
            chat = self.chats.get(chat_id)
            if chat is None or not chat.live:
                return
            if chat.working:
                raise HostError("a working chat is never paused")
            thread_id = chat.thread_id
            generation = self._generation
            chat.live = False
        try:
            closed = self._unload(thread_id)
        except HostError:
            with self.lock:
                # Codex did not take the unload: the thread is still loaded, so the chat is live again.
                if self._generation == generation and self.process is not None:
                    chat.live = True
            raise
        with self.lock:
            self._forget_sub_agents(chat_id, thread_id)
            if not closed:
                # The thread may still hold memory; a fresh process frees it.
                self.restart_due = True
                self.log("Codex host: a paused chat did not unload; restart due.")

    def _unload(self, thread_id: str) -> bool:
        """Send thread/unsubscribe and wait for thread/closed. True when the thread closed or the process is gone."""
        self._call("thread/unsubscribe", {"threadId": thread_id})
        with self.lock:
            return self._notice.wait_for(
                lambda: thread_id in self._closed_threads or self.process is None,
                self.reply_timeout,
            )

    def _forget_sub_agents(self, chat_id: str, thread_id: str) -> None:
        """Drop the sub-agent threads of a chat that is no longer loaded. Lock held."""
        for sub, owner in list(self._threads.items()):
            if owner == chat_id and sub != thread_id:
                del self._threads[sub]

    # Exchanges -------------------------------------------------------------------------------------------------

    def attach(self, chat_id: str, session: Any) -> None:
        """Give a live chat's output to the session of its exchange, after the session file check.

        The session must be on the shared connection (AppServerSession with owns_connection=False). When another
        process wrote to the chat's session file since the last exchange, the chat is paused and woken first, so
        Codex reads the file again.
        """
        with self.lock:
            chat = self.chats.get(chat_id)
            if chat is None or not chat.live or self.process is None:
                raise HostError("the chat is not live")
            if chat.working:
                raise HostError("the chat already has a running exchange")
            foreign = chat.file_signature != file_signature(
                self.session_file(chat.thread_id)
            )
            params, names = dict(chat.thread_params), list(chat.tool_names)
        if foreign:
            self.log(
                "Codex host: the chat's session file changed outside this process; waking it again."
            )
            self.pause(chat_id)
            self.wake(chat_id, params, names)
        with self.lock:
            if not chat.live or self.process is None:
                raise HostError("the chat is not live")
            chat.session = session
            chat.turn_done.clear()
            chat.last_used = self.clock()

    def detach(self, chat_id: str) -> None:
        """End an exchange: the chat stays live. Record its session file.

        When a restart or a stop waits and this was the last running exchange, it runs now, on the caller's thread:
        a restart can take as long as a start (reply_timeout for initialize, then a wake of the selected chat).
        """
        with self.lock:
            chat = self.chats.get(chat_id)
            if chat is None:
                return
            chat.session = None
            chat.last_used = self.clock()
            if chat.live:
                chat.file_signature = file_signature(self.session_file(chat.thread_id))
            restart = (
                self.restart_due or bool(self.stop_due)
            ) and not self._anyone_working()
        if restart:
            self.ready_for_message()

    def cancel(self, chat_id: str, timeout: float = INTERRUPT_TIMEOUT) -> bool:
        """Stop the running exchange of one chat; the other chats go on.

        The session's close sends turn/interrupt for this chat's thread. When turn/completed does not arrive in
        time, the exchange counts as stopped and a restart is due (False).
        """
        with self.lock:
            chat = self.chats.get(chat_id)
            session = chat.session if chat else None
        if chat is None:
            return True
        if session is not None:
            session.close()
        with self.lock:
            chat.session = None
            running = chat.turn_running or bool(getattr(session, "turn_running", False))
        if running and not chat.turn_done.wait(timeout):
            with self.lock:
                chat.turn_running = False
                self.restart_due = True
            self.log("Codex host: a canceled turn did not end; restart due.")
            return False
        return True

    def stderr_mark(self) -> int:
        """Return a mark of the error output read so far, for stderr_since."""
        with self.lock:
            return self._stderr_count

    def stderr_since(self, mark: int) -> list[str]:
        """Return the redacted error lines read after a mark, at most the last STDERR_LINES of them."""
        with self.lock:
            new = self._stderr_count - mark
            return list(self.stderr_tail)[-new:] if new > 0 else []

    def request_restart(self, reason: str) -> None:
        """Mark a restart due, for example after a sign-in or an options save. It waits for running exchanges."""
        with self.lock:
            if self.process is None:
                return
            self.restart_due = True
        self.log(f"Codex host: restart due ({reason}).")

    # The reader ------------------------------------------------------------------------------------------------

    def _read(self, handle: Any, generation: int) -> None:
        """Read the process output for its whole life. The end of the output is a crash."""
        try:
            for line in iter(handle.readline, ""):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._route(message, generation)
        except (OSError, ValueError):
            pass
        self._crash(generation, "the output ended")

    def _read_errors(self, handle: Any) -> None:
        """Keep the last lines of the process's error output, redacted, for the log of a failed exchange."""
        try:
            for line in iter(handle.readline, ""):
                with self.lock:
                    self.stderr_tail.append(self.redact(line.rstrip("\n")))
                    self._stderr_count += 1
        except (OSError, ValueError):
            pass

    def _route(self, message: dict[str, Any], generation: int) -> None:
        """Give one line to one place: the host, the session of the chat it names, an error reply, or nowhere."""
        with self.lock:
            if generation != self._generation:
                return
            request_id = message.get("id")
            method = message.get("method")
            params = (
                message.get("params") if isinstance(message.get("params"), dict) else {}
            )
            if request_id is not None and not method:
                session = self._reply_owner(request_id)
                if session is None:
                    return
            else:
                self._note(method, params)
                chat = self.chats.get(self.chat_for_thread(_thread_of(params)))
                session = chat.session if chat else None
                if session is None:
                    if request_id is not None:
                        # Nobody is at that chat to answer a question from Codex.
                        self._write({"id": request_id, "error": UNSUPPORTED})
                    return
        try:
            session.receive(message)
        except Exception as exc:  # noqa: BLE001 - any error of one session ends that exchange, never the reader.
            session.fail(f"Codex permission connection failed: {exc}")

    def _reply_owner(self, request_id: Any) -> Any:
        """Take a reply for the host, or return the session that sent the request (None: drop it). Lock held."""
        if isinstance(request_id, str) and request_id in self._waiting:
            return _HostReply(self, request_id)
        for chat in self.chats.values():
            if chat.session is not None and chat.session.owns_request(request_id):
                return chat.session
        return None

    def _note(self, method: Any, params: dict[str, Any]) -> None:
        """Record what the host itself follows: tool status, closed threads, turns, sub-agents. Lock held."""
        thread_id = _thread_of(params)
        if method == "mcpServer/startupStatus/updated":
            name = params.get("name")
            if isinstance(name, str):
                # Kept by thread, so a wake reads only its own thread's notices (see _tool_report).
                key = thread_id if isinstance(thread_id, str) else None
                self._tool_status[(key, name)] = str(params.get("status") or "")
        elif method == "thread/closed" and isinstance(thread_id, str):
            self._closed_threads.add(thread_id)
        elif method in ("item/started", "item/completed"):
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") == "collabAgentToolCall":
                owner = self.chat_for_thread(item.get("senderThreadId"))
                receivers = item.get("receiverThreadIds")
                if owner and isinstance(receivers, list):
                    for child in receivers:
                        if (
                            isinstance(child, str)
                            and child
                            and child not in self._threads
                        ):
                            self._threads[child] = owner
        elif method in ("turn/started", "turn/completed"):
            chat = self.chats.get(self.chat_for_thread(thread_id))
            if chat is not None and chat.thread_id == thread_id:
                chat.turn_running = method == "turn/started"
                if method == "turn/completed":
                    chat.turn_done.set()
        self._notice.notify_all()


class _HostReply:
    """Stands in for a session when a reply answers a request of the host: it files the reply and wakes the call."""

    def __init__(self, host: CodexHost, request_id: str) -> None:
        self.host = host
        self.request_id = request_id

    def receive(self, message: dict[str, Any]) -> None:
        with self.host.lock:
            self.host._replies[self.request_id] = message
            event = self.host._waiting.get(self.request_id)
        if event is not None:
            event.set()

    def fail(self, _message: str) -> None:
        """A reply cannot fail; nothing to do."""


def _thread_of(params: dict[str, Any]) -> Any:
    """The thread id a line names: params.threadId, or params.thread.id for a thread notice."""
    if "threadId" in params:
        return params["threadId"]
    thread = params.get("thread")
    return thread.get("id") if isinstance(thread, dict) else None
