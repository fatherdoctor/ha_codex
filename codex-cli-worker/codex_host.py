"""Keep one `codex app-server` process alive and run each chat as a thread in it.

This module is the host of that process. Nothing starts it yet: the worker still starts one Codex process for each
exchange (server.py, run_task). A later change asks the host first and keeps the start path of today as the
fallback for every failure here.

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
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The largest number of live chats. Each live chat holds its own tool programs in memory, so this is the one
# resource rule of the host. Every use of the value goes through CodexHost.live_max.
LIVE_CHATS_MAX = 3

# Added to the process arguments: a thread unloads as soon as the host unsubscribes from it.
UNLOAD_ARGS = ["--config", "thread_unload_delay_secs=0"]

REPLY_TIMEOUT = 30.0  # No reply to a host request in this time counts as a crash.
READY_TIMEOUT = 60.0  # A woken chat's tool programs must be ready in this time, or the wake fails.
INTERRUPT_TIMEOUT = 10.0  # A canceled turn must complete in this time, or a restart is due.
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
    return {name: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
            for name, value in rows.items()}


def changed_rows(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Return the names of the rows whose hash differs, or that only one side has, in sorted order."""
    return sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))


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
    """

    def __init__(
        self, *, spawn: Callable[[list[str], dict[str, str]], Any], reap: Callable[[Any], Any],
        args: Callable[[], list[str]], env: Callable[[], dict[str, str]],
        fingerprint_rows: Callable[[], dict[str, Any]], enabled: Callable[[], bool],
        session_file: Callable[[str], Path | None], redact: Callable[[str], str], log: Callable[[str], None],
        lock: Any, live_max: int = LIVE_CHATS_MAX, clock: Callable[[], float] = time.monotonic,
        reply_timeout: float = REPLY_TIMEOUT, ready_timeout: float = READY_TIMEOUT,
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
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        self._fingerprint: dict[str, str] = {}
        self._threads: dict[str, str] = {}  # thread id -> chat id, for each chat's thread and its sub-agents
        self._replies: dict[str, dict[str, Any]] = {}
        self._waiting: dict[str, threading.Event] = {}
        self._next_id = 0
        self._generation = 0  # Counts processes, so a reader of an old process cannot touch a new one.
        self._crashes: list[float] = []
        self._paused_until = 0.0
        self._tool_status: dict[str, str] = {}
        self._closed_threads: set[str] = set()
        self._waking = ""  # The chat that wake() loads now.
        self._notice = threading.Condition(self.lock)
        self._wake_lock = threading.Lock()  # One wake or pause at a time, so ready notices belong to it.

    # The process ---------------------------------------------------------------------------------------------

    def running(self) -> bool:
        """Return True while a process is up."""
        with self.lock:
            return self.process is not None

    def ready_for_message(self) -> bool:
        """Compare the fingerprint, restart when due and possible, and say whether a new exchange may start here.

        False means: use the start path of today, or (when a restart is due) keep the message in the queue. The
        caller tells the two apart with `restart_due`.
        """
        if not self.enabled():
            self.stop("the owner switch is off")
            return False
        rows = self.fingerprint_rows()
        if rows.get("sign_in") is None:
            self.stop("signed out")
            return False
        with self.lock:
            if self.clock() < self._paused_until:
                return False
            new = fingerprint(rows)
            if self.process is not None and new != self._fingerprint:
                names = changed_rows(self._fingerprint, new)
                self.log(f"Codex host: restart due, changed: {', '.join(names)}.")
                self.restart_due = True
            if self.restart_due and any(chat.working for chat in self.chats.values()):
                return False
            if self.process is not None and not self.restart_due:
                return True
        if self.restart_due:
            self.stop("restart")
        return self._start(new)

    def _start(self, new_fingerprint: dict[str, str]) -> bool:
        """Start a process and initialize it. On failure, stop it again and return False."""
        with self.lock:
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
            threading.Thread(target=self._read, args=(process.stdout, generation), daemon=True,
                             name="codex-host-reader").start()
            threading.Thread(target=self._read_errors, args=(process.stderr,), daemon=True,
                             name="codex-host-stderr").start()
        try:
            self._call("initialize", {"clientInfo": {
                "name": "home_assistant_worker", "title": "Home Assistant", "version": "1"}})
            with self.lock:
                self._write({"method": "initialized"})
        except HostError:
            self.stop("initialize failed")
            return False
        self.log("Codex host: the process started.")
        self._wake_selected()
        return True

    def stop(self, reason: str) -> None:
        """Stop the process. Every chat is paused. A running exchange is failed (a stop is only for no-work cases)."""
        with self.lock:
            process = self.process
            if process is None:
                return
            self.process = None
            self.writer = None
            sessions = self._forget_all()
        for session in sessions:
            session.fail("The Codex process stopped before the exchange finished.")
        self.reap(process)
        self.log(f"Codex host: the process stopped ({reason}).")

    def _forget_all(self) -> list[Any]:
        """Mark every chat paused and wake every waiter; return the sessions that were attached. Lock held."""
        sessions = [chat.session for chat in self.chats.values() if chat.session is not None]
        for chat in self.chats.values():
            chat.live = False
            chat.session = None
            chat.turn_running = False
            chat.turn_done.set()
        self._threads = {chat.thread_id: chat.chat_id for chat in self.chats.values() if chat.thread_id}
        for event in self._waiting.values():
            event.set()
        self._notice.notify_all()
        return sessions

    def _crash(self, generation: int, reason: str) -> None:
        """Fail each running exchange, reap the process, and start a new one unless crashes repeat."""
        with self.lock:
            if generation != self._generation or self.process is None:
                return
            process = self.process
            self.process = None
            self.writer = None
            sessions = self._forget_all()
            now = self.clock()
            self._crashes = [moment for moment in self._crashes if now - moment < CRASH_WINDOW] + [now]
            too_many = len(self._crashes) >= CRASH_LIMIT
            if too_many:
                self._paused_until = now + CRASH_PAUSE
                self._crashes = []
        for session in sessions:
            session.fail("Codex stopped before the exchange finished.")
        self.reap(process)
        if too_many:
            self.log(f"Codex host: {CRASH_LIMIT} crashes in {int(CRASH_WINDOW // 60)} minutes ({reason}); no live "
                     f"process for {int(CRASH_PAUSE // 60)} minutes. Each message starts its own Codex process.")
            return
        self.log(f"Codex host: the process crashed ({reason}); starting a new one.")
        threading.Thread(target=self.ready_for_message, daemon=True, name="codex-host-restart").start()

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
            raise HostError(f"{method}: {self.redact(str(reply['error'].get('message') or 'failed'))}")
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
            return self._threads.get(thread_id, "") if isinstance(thread_id, str) else ""

    def select(self, chat_id: str, thread_params: dict[str, Any], tool_names: list[str]) -> bool:
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

    def wake(self, chat_id: str, thread_params: dict[str, Any], tool_names: list[str]) -> str:
        """Make a chat live and return its thread id. Raise AllChatsBusy or HostError when that cannot be done.

        thread_params are those of thread/start, or of thread/resume when the chat has a thread. tool_names are the
        tool programs that must be ready before the chat is live.
        """
        with self._wake_lock:
            with self.lock:
                if self.process is None:
                    raise HostError("no Codex process")
                chat = self.chats.setdefault(chat_id, Chat(chat_id))
                chat.last_used = self.clock()
                if chat.live:
                    return chat.thread_id
                victim = self._victim_for(chat_id)
            if victim:
                self._pause_locked_out(victim)
            with self.lock:
                self._tool_status = {}
                self._waking = chat_id
                params = dict(thread_params)
                if chat.thread_id:
                    params["threadId"] = chat.thread_id
                method = "thread/resume" if params.get("threadId") else "thread/start"
            result = self._call(method, params)
            thread_id = (result.get("thread") or {}).get("id")
            if not isinstance(thread_id, str) or not thread_id or (params.get("threadId") not in (None, thread_id)):
                raise HostError(f"{method}: Codex gave another thread")
            with self.lock:
                chat.thread_id = thread_id
                chat.thread_params = {key: value for key, value in params.items() if key != "threadId"}
                chat.tool_names = tuple(tool_names)
                self._threads[thread_id] = chat_id
                self._closed_threads.discard(thread_id)
                ready = self._notice.wait_for(lambda: self._tools_settled(tool_names), self.ready_timeout)
                failed = sorted(name for name in tool_names if self._tool_status.get(name) not in (None, "ready",
                                                                                                  "starting"))
                if not ready or failed:
                    raise HostError("tool programs not ready: " + ", ".join(failed or sorted(
                        name for name in tool_names if self._tool_status.get(name) != "ready")))
                chat.live = True
                chat.file_signature = file_signature(self.session_file(thread_id))
                return thread_id

    def _tools_settled(self, names: list[str]) -> bool:
        """True when each named tool program is ready, or one has failed, or the process is gone. Lock held."""
        if self.process is None:
            return True
        status = [self._tool_status.get(name) for name in names]
        return all(value == "ready" for value in status) or any(value not in (None, "ready", "starting")
                                                                  for value in status)

    def _victim_for(self, chat_id: str) -> str:
        """Return the chat to pause so that one more can be live, or "" when there is room. Lock held.

        The idle live chat used longest ago goes first, the selected chat last, a working chat never.
        """
        live = [chat for chat in self.chats.values() if chat.live and chat.chat_id != chat_id]
        if len(live) < self.live_max:
            return ""
        idle = [chat for chat in live if not chat.working]
        if not idle:
            raise AllChatsBusy("every live chat is working")
        idle.sort(key=lambda chat: (chat.chat_id == self.selected, chat.last_used))
        return idle[0].chat_id

    def pause(self, chat_id: str) -> None:
        """Unload a live chat that does not work. Its history stays in its session file."""
        with self._wake_lock:
            self._pause_locked_out(chat_id)

    def _pause_locked_out(self, chat_id: str) -> None:
        """Pause a chat; the caller holds the wake lock, not the worker lock."""
        with self.lock:
            chat = self.chats.get(chat_id)
            if chat is None or not chat.live:
                return
            if chat.working:
                raise HostError("a working chat is never paused")
            thread_id = chat.thread_id
        self._call("thread/unsubscribe", {"threadId": thread_id})
        with self.lock:
            closed = self._notice.wait_for(lambda: thread_id in self._closed_threads or self.process is None,
                                           self.reply_timeout)
            chat.live = False
            for sub, owner in list(self._threads.items()):
                if owner == chat_id and sub != thread_id:
                    del self._threads[sub]
            if not closed:
                # The thread may still hold memory; a fresh process frees it.
                self.restart_due = True
                self.log("Codex host: a paused chat did not unload; restart due.")

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
            foreign = chat.file_signature != file_signature(self.session_file(chat.thread_id))
            params, names = dict(chat.thread_params), list(chat.tool_names)
        if foreign:
            self.log("Codex host: the chat's session file changed outside this process; waking it again.")
            self.pause(chat_id)
            self.wake(chat_id, params, names)
        with self.lock:
            if not chat.live or self.process is None:
                raise HostError("the chat is not live")
            chat.session = session
            chat.turn_done.clear()
            chat.last_used = self.clock()

    def detach(self, chat_id: str) -> None:
        """End an exchange: the chat stays live. Record its session file; restart now if a restart is due."""
        with self.lock:
            chat = self.chats.get(chat_id)
            if chat is None:
                return
            chat.session = None
            chat.last_used = self.clock()
            if chat.live:
                chat.file_signature = file_signature(self.session_file(chat.thread_id))
            restart = self.restart_due and not any(other.working for other in self.chats.values())
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
        except (OSError, ValueError):
            pass

    def _route(self, message: dict[str, Any], generation: int) -> None:
        """Give one line to one place: the host, the session of the chat it names, an error reply, or nowhere."""
        with self.lock:
            if generation != self._generation:
                return
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
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
            # A notice that names another chat's thread (a sub-agent there starts a tool) is not about this wake.
            other = isinstance(thread_id, str) and self._threads.get(thread_id) not in (None, self._waking)
            if isinstance(name, str) and not other:
                self._tool_status[name] = str(params.get("status") or "")
        elif method == "thread/closed" and isinstance(thread_id, str):
            self._closed_threads.add(thread_id)
        elif method in ("item/started", "item/completed"):
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") == "collabAgentToolCall":
                owner = self.chat_for_thread(item.get("senderThreadId"))
                receivers = item.get("receiverThreadIds")
                if owner and isinstance(receivers, list):
                    for child in receivers:
                        if isinstance(child, str) and child and child not in self._threads:
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
