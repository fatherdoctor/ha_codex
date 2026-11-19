"""Tests of codex_host.CodexHost against a fake app-server process.

The fake speaks the few app-server messages the host uses and records every line the host writes. No Codex program
and no network: the proofs that the pinned Codex behaves as the fake does are in test_live_proofs.py (P1 to P10).
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_host
from codex_host import AllChatsBusy, CodexHost, HostError

TOOLS = ["home_assistant_data", "home_assistant_verify"]
WAIT = 5.0


class Pipe:
    """A text stream that one side writes lines to and the other reads with readline; close ends it."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()

    def put(self, message: dict | str) -> None:
        self.lines.put(message if isinstance(message, str) else json.dumps(message) + "\n")

    def close(self) -> None:
        self.lines.put("")

    def readline(self) -> str:
        return self.lines.get()


class Input:
    """The process input: each line the host writes goes to the fake Codex."""

    def __init__(self, codex: FakeCodex) -> None:
        self.codex = codex

    def write(self, text: str) -> None:
        for line in text.splitlines():
            message = json.loads(line)
            if message.get("method") in self.codex.broken:
                raise OSError("broken pipe")
            self.codex.handle(message)

    def flush(self) -> None:
        pass


class FakeCodex:
    """One fake app-server process. The flags change how it answers."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.stdin = Input(self)
        self.stdout = Pipe()
        self.stderr = Pipe()
        self.sent: list[dict] = []
        self.silent: set[str] = set()  # Methods that get no reply.
        self.broken: set[str] = set()  # Methods whose write fails, as on a closed pipe.
        self.no_closed = False
        self.tool_status = "ready"
        self.notice_thread = False  # Put the thread id on each tool notice.
        self.after_thread: list[dict] = []  # Lines sent after the tool notices of each thread/start or resume.
        self.crash_after_thread = False  # End the output right after the reply to thread/start or resume.
        self.loaded: set[str] = set()  # Threads that hold their tool programs now.
        self.threads = 0
        self.reaped = False

    def handle(self, message: dict) -> None:
        self.sent.append(message)
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        if request_id is None or method in self.silent:
            return
        if method in ("thread/start", "thread/resume"):
            if method == "thread/start":
                self.threads += 1
                thread_id = f"p{self.number}-thread-{self.threads}"
            else:
                thread_id = params["threadId"]
            self.loaded.add(thread_id)
            self.stdout.put({"id": request_id, "result": {"thread": {"id": thread_id}}})
            if self.crash_after_thread:
                self.crash()
                return
            where = {"threadId": thread_id} if self.notice_thread else {}
            for name in TOOLS:
                self.stdout.put({"method": "mcpServer/startupStatus/updated",
                                 "params": {"name": name, "status": "starting", **where}})
                if self.tool_status != "starting":
                    self.stdout.put({"method": "mcpServer/startupStatus/updated",
                                     "params": {"name": name, "status": self.tool_status, **where}})
            for line in self.after_thread:
                self.stdout.put(line)
        elif method == "thread/unsubscribe":
            self.loaded.discard(params["threadId"])
            self.stdout.put({"id": request_id, "result": {}})
            if not self.no_closed:
                self.stdout.put({"method": "thread/closed", "params": {"threadId": params["threadId"]}})
        elif method == "initialize" or str(request_id).startswith("host-"):
            self.stdout.put({"id": request_id, "result": {}})

    def methods(self, name: str) -> list[dict]:
        return [message for message in self.sent if message.get("method") == name]

    def emit(self, message: dict) -> None:
        self.stdout.put(message)

    def crash(self) -> None:
        self.stdout.close()
        self.stderr.close()


class FakeSession:
    """Stands in for an AppServerSession on the shared connection: it records what reaches it."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.received: list[dict] = []
        self.failed = ""
        self.closed = False
        self.turn_running = False
        self.raise_on = ""

    def owns_request(self, request_id) -> bool:
        return isinstance(request_id, str) and request_id.startswith(self.prefix + "-")

    def receive(self, message: dict) -> None:
        if self.raise_on and message.get("method") == self.raise_on:
            raise ValueError("permission request does not belong to the active exchange")
        self.received.append(message)

    def fail(self, message: str) -> None:
        self.failed = message

    def close(self) -> None:
        self.closed = True


def wait_until(condition, timeout: float = WAIT) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ReleaseHookLock:
    """A reentrant lock that runs one hook, once, right after its outermost release, on the releasing thread."""

    def __init__(self) -> None:
        self.inner = threading.RLock()
        self.after_release = None

    def acquire(self, *args) -> bool:
        return self.inner.acquire(*args)

    def release(self) -> None:
        self.inner.release()
        hook, self.after_release = self.after_release, None
        if hook is not None and not self.inner._is_owned():
            hook()
        elif hook is not None:
            self.after_release = hook

    __enter__ = acquire

    def __exit__(self, *_exc) -> None:
        self.release()

    # threading.Condition uses these to wait with a reentrant lock held.
    def _is_owned(self) -> bool:
        return self.inner._is_owned()

    def _release_save(self):
        return self.inner._release_save()

    def _acquire_restore(self, state) -> None:
        self.inner._acquire_restore(state)


class HostTest(unittest.TestCase):
    live_max = 3

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.processes: list[FakeCodex] = []
        self.spawned_args: list[list[str]] = []
        self.logs: list[str] = []
        self.rows = {"env": {"HA_URL": "http://supervisor/core", "HA_TOKEN": "token-value-1"},
                     "args": ["app-server"], "sign_in": [10, 20], "versions": ["0.5.0", "0.166.0"],
                     "rules": "hash-of-rule-files"}
        self.on = True
        self.clock = Clock()
        self.on_spawn = lambda process: None
        # Each host ends with its test, with its reader threads (make_host adds the close). This check is added
        # first, so it runs last: the Home Assistant test plugin fails a test that leaves a thread behind.
        self.addCleanup(self.assert_no_host_threads)
        self.host = self.make_host()

    def make_host(self, **overrides) -> CodexHost:
        def spawn(args, env):
            self.spawned_args.append(args)
            process = FakeCodex(len(self.processes) + 1)
            self.processes.append(process)
            self.on_spawn(process)
            return process

        def reap(process):
            process.reaped = True
            process.crash()

        options = {
            "spawn": spawn, "reap": reap, "args": lambda: ["app-server"], "env": dict,
            "fingerprint_rows": lambda: json.loads(json.dumps(self.rows)), "enabled": lambda: self.on,
            "session_file": lambda thread_id: Path(self.tmp.name) / f"{thread_id}.jsonl",
            "redact": lambda text: text.replace("token-value-1", "[hidden]"), "log": self.logs.append,
            "lock": threading.RLock(), "live_max": self.live_max, "clock": self.clock, "reply_timeout": 1.0,
            "ready_timeout": 1.0}
        options.update(overrides)
        host = CodexHost(**options)
        self.addCleanup(host.close)
        return host

    def assert_no_host_threads(self) -> None:
        names = [thread.name for thread in threading.enumerate() if thread.name.startswith("codex-host-")]
        self.assertEqual(names, [])

    @property
    def codex(self) -> FakeCodex:
        return self.processes[-1]

    def start(self) -> None:
        self.assertTrue(self.host.ready_for_message())

    def wake(self, chat_id: str) -> str:
        return self.host.wake(chat_id, {"cwd": "/config"}, TOOLS)

    def begin(self, chat_id: str) -> FakeSession:
        """Wake a chat and attach a session to it, as an exchange does."""
        self.wake(chat_id)
        session = FakeSession(f"worker-{chat_id}")
        self.host.attach(chat_id, session)
        return session


class ProcessTests(HostTest):
    def test_the_process_starts_once_with_the_unload_setting(self) -> None:
        self.start()
        self.start()
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.spawned_args[0][-2:], ["--config", "thread_unload_delay_secs=0"])
        self.assertEqual([m["method"] for m in self.codex.sent[:2]], ["initialize", "initialized"])

    def test_the_owner_switch_off_starts_no_process(self) -> None:
        self.on = False
        self.assertFalse(self.host.ready_for_message())
        self.assertEqual(self.processes, [])

    def test_the_owner_switch_off_stops_a_running_process(self) -> None:
        self.start()
        self.on = False
        self.assertFalse(self.host.ready_for_message())
        self.assertTrue(self.processes[0].reaped)
        self.assertFalse(self.host.running())

    def test_a_sign_out_stops_the_process_and_starts_none(self) -> None:
        self.start()
        self.rows["sign_in"] = None
        self.assertFalse(self.host.ready_for_message())
        self.assertTrue(self.processes[0].reaped)
        self.assertEqual(len(self.processes), 1)

    def test_each_fingerprint_row_restarts_the_process_and_only_its_name_is_logged(self) -> None:
        self.start()
        changes = {"env": {"HA_URL": "http://192.0.2.7:8123", "HA_TOKEN": "token-value-1"},
                   "args": ["app-server", "--config", "model=other"], "sign_in": [11, 21],
                   "versions": ["0.5.1", "0.166.0"], "rules": "edited-agents-md"}
        for number, (name, value) in enumerate(changes.items(), start=2):
            with self.subTest(row=name):
                self.rows[name] = value
                self.assertTrue(self.host.ready_for_message())
                self.assertEqual(len(self.processes), number)
                self.assertTrue(self.processes[number - 2].reaped)
                self.assertTrue(any(line.endswith(f"changed: {name}.") for line in self.logs), self.logs)
        text = "\n".join(self.logs)
        for value in ("192.0.2.7", "token-value-1", "model=other", "edited-agents-md", "0.5.1"):
            self.assertNotIn(value, text)

    def test_an_unchanged_fingerprint_keeps_the_process(self) -> None:
        self.start()
        for _ in range(3):
            self.assertTrue(self.host.ready_for_message())
        self.assertEqual(len(self.processes), 1)

    def test_a_due_restart_waits_for_the_running_exchange(self) -> None:
        self.start()
        session = self.begin("chat-a")
        self.rows["rules"] = "edited-agents-md"
        self.assertFalse(self.host.ready_for_message())  # The new message waits in the queue.
        self.assertTrue(self.host.restart_due)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(session.failed, "")
        self.host.detach("chat-a")  # The exchange ends: now the restart runs.
        self.assertEqual(len(self.processes), 2)
        self.assertFalse(self.host.restart_due)
        self.assertTrue(self.host.ready_for_message())

    def test_after_a_restart_every_chat_is_paused_and_the_selected_one_wakes(self) -> None:
        self.start()
        self.assertTrue(self.host.select("chat-a", {"cwd": "/config"}, TOOLS))
        self.wake("chat-b")
        thread_a = self.host.chat("chat-a").thread_id
        self.host.request_restart("options saved")
        self.assertTrue(self.host.ready_for_message())
        self.assertEqual(len(self.processes), 2)
        self.assertEqual(self.host.live_chats(), ["chat-a"])
        self.assertEqual([m["params"]["threadId"] for m in self.codex.methods("thread/resume")], [thread_a])

    def test_a_crash_fails_every_running_exchange_and_starts_a_new_process(self) -> None:
        self.start()
        first, second = self.begin("chat-a"), self.begin("chat-b")
        self.codex.crash()
        self.assertTrue(wait_until(lambda: len(self.processes) == 2 and self.host.running()))
        self.assertTrue(first.failed and second.failed)
        self.assertEqual(self.host.live_chats(), [])
        self.assertTrue(self.processes[0].reaped)

    def test_the_third_crash_in_ten_minutes_starts_no_process_for_an_hour(self) -> None:
        self.start()
        for number in (2, 3):
            self.codex.crash()
            self.assertTrue(wait_until(lambda number=number: len(self.processes) == number and self.host.running()))
            self.clock.now += 60
        self.codex.crash()
        self.assertTrue(wait_until(lambda: not self.host.running()))
        time.sleep(0.1)
        self.assertEqual(len(self.processes), 3)
        self.assertFalse(self.host.ready_for_message())
        self.assertIn("3 crashes in 10 minutes", self.logs[-1])
        self.clock.now += codex_host.CRASH_PAUSE
        self.assertTrue(self.host.ready_for_message())
        self.assertEqual(len(self.processes), 4)

    def test_crashes_further_apart_than_ten_minutes_do_not_add_up(self) -> None:
        self.start()
        for number in (2, 3, 4):
            self.codex.crash()
            self.assertTrue(wait_until(lambda number=number: len(self.processes) == number and self.host.running()))
            self.clock.now += codex_host.CRASH_WINDOW
        self.assertTrue(self.host.ready_for_message())

    def test_no_reply_to_a_host_request_is_a_crash(self) -> None:
        self.start()
        session = self.begin("chat-a")
        self.codex.silent.add("thread/start")
        with self.assertRaises(HostError):
            self.wake("chat-b")
        self.assertTrue(session.failed)
        self.assertTrue(wait_until(lambda: len(self.processes) == 2))

    def test_the_error_output_is_redacted_and_keeps_the_last_fifty_lines(self) -> None:
        self.start()
        for number in range(60):
            self.codex.stderr.put(f"line {number} token-value-1\n")
        self.assertTrue(wait_until(lambda: list(self.host.stderr_tail)[-1:] == ["line 59 [hidden]"]))
        self.assertEqual(len(self.host.stderr_tail), 50)
        self.assertEqual(self.host.stderr_tail[0], "line 10 [hidden]")


class WakeAndPauseTests(HostTest):
    def test_a_new_chat_starts_a_thread_and_waits_for_each_tool_program(self) -> None:
        self.start()
        thread_id = self.wake("chat-a")
        self.assertEqual(self.host.chat_for_thread(thread_id), "chat-a")
        self.assertEqual(len(self.codex.methods("thread/start")), 1)
        self.assertEqual(self.host.live_chats(), ["chat-a"])

    def test_a_live_chat_wakes_without_a_request(self) -> None:
        self.start()
        self.wake("chat-a")
        before = len(self.codex.sent)
        self.wake("chat-a")
        self.assertEqual(len(self.codex.sent), before)

    def test_a_paused_chat_wakes_with_its_own_thread(self) -> None:
        self.start()
        thread_id = self.wake("chat-a")
        self.host.pause("chat-a")
        self.assertEqual(self.codex.methods("thread/unsubscribe")[-1]["params"], {"threadId": thread_id})
        self.assertEqual(self.host.live_chats(), [])
        self.assertEqual(self.wake("chat-a"), thread_id)
        self.assertEqual(self.codex.methods("thread/resume")[-1]["params"]["threadId"], thread_id)

    def test_a_tool_program_that_fails_to_start_fails_the_wake(self) -> None:
        self.start()
        self.codex.tool_status = "failed"
        with self.assertRaisesRegex(HostError, "home_assistant_data"):
            self.wake("chat-a")
        self.assertEqual(self.host.live_chats(), [])

    def test_a_pause_with_no_closed_notice_makes_a_restart_due(self) -> None:
        self.start()
        self.wake("chat-a")
        self.codex.no_closed = True
        self.host.pause("chat-a")
        self.assertTrue(self.host.restart_due)

    def test_a_working_chat_is_never_paused(self) -> None:
        self.start()
        self.begin("chat-a")
        with self.assertRaises(HostError):
            self.host.pause("chat-a")
        self.assertEqual(self.host.live_chats(), ["chat-a"])

    def test_the_chat_stays_live_after_its_exchange(self) -> None:
        self.start()
        self.begin("chat-a")
        self.host.detach("chat-a")
        self.assertEqual(self.host.live_chats(), ["chat-a"])


class CapTests(HostTest):
    """The cap is read from the host's one value; each test runs with it at 2, 3 and 4."""

    def each_cap(self):
        for cap in (2, 3, 4):
            with self.subTest(cap=cap):
                self.processes.clear()
                self.host = self.make_host(live_max=cap)
                self.start()
                yield cap

    def test_never_more_live_chats_than_the_cap(self) -> None:
        for cap in self.each_cap():
            self.host.select("chat-0", {"cwd": "/config"}, TOOLS)
            for number in range(cap + 3):
                self.clock.now += 1
                if number % 2:
                    self.begin(f"chat-{number}")  # A message wakes the chat ...
                    self.host.detach(f"chat-{number}")  # ... and its exchange ends.
                else:
                    self.host.select(f"chat-{number}", {"cwd": "/config"}, TOOLS)
                self.assertLessEqual(len(self.host.live_chats()), cap)
            self.assertEqual(len(self.host.live_chats()), cap)

    def test_the_idle_chat_used_longest_ago_is_paused_first(self) -> None:
        for cap in self.each_cap():
            for number in range(cap):
                self.clock.now += 1
                self.wake(f"chat-{number}")
            self.clock.now += 1
            self.wake("chat-0")  # Used again: chat-1 is now the oldest.
            self.clock.now += 1
            self.wake("chat-new")
            self.assertNotIn("chat-1", self.host.live_chats())
            self.assertIn("chat-0", self.host.live_chats())

    def test_the_selected_chat_is_paused_last(self) -> None:
        for cap in self.each_cap():
            self.clock.now += 1
            self.host.select("chat-selected", {"cwd": "/config"}, TOOLS)
            for number in range(1, cap):
                self.clock.now += 1
                self.wake(f"chat-{number}")
            self.clock.now += 1
            self.wake("chat-new")
            self.assertIn("chat-selected", self.host.live_chats())
            self.assertNotIn("chat-1", self.host.live_chats())

    def test_the_selected_chat_is_paused_when_it_is_the_only_idle_one(self) -> None:
        for cap in self.each_cap():
            self.host.select("chat-selected", {"cwd": "/config"}, TOOLS)
            for number in range(1, cap):
                self.begin(f"chat-{number}")
            self.wake("chat-new")
            self.assertNotIn("chat-selected", self.host.live_chats())

    def test_when_every_live_chat_works_a_wake_waits(self) -> None:
        for cap in self.each_cap():
            for number in range(cap):
                self.begin(f"chat-{number}")
            with self.assertRaises(AllChatsBusy):
                self.wake("chat-new")
            self.assertEqual(len(self.host.live_chats()), cap)
            self.host.detach("chat-0")  # One exchange ends: now the new chat can wake.
            self.wake("chat-new")
            self.assertNotIn("chat-0", self.host.live_chats())

    def test_selecting_a_chat_when_every_live_chat_works_leaves_it_paused(self) -> None:
        for cap in self.each_cap():
            for number in range(cap):
                self.begin(f"chat-{number}")
            self.assertFalse(self.host.select("chat-new", {"cwd": "/config"}, TOOLS))
            self.assertNotIn("chat-new", self.host.live_chats())
            self.assertEqual(self.host.selected, "chat-new")
            self.assertEqual(len(self.host.live_chats()), cap)

    def test_a_scheduled_run_wakes_a_chat_that_is_not_selected(self) -> None:
        for cap in self.each_cap():
            self.host.select("chat-selected", {"cwd": "/config"}, TOOLS)
            self.begin("chat-scheduled")
            self.host.detach("chat-scheduled")
            self.assertIn("chat-scheduled", self.host.live_chats())
            self.assertEqual(self.host.selected, "chat-selected")

    def test_a_sub_agent_does_not_count_toward_the_cap(self) -> None:
        for cap in self.each_cap():
            threads = [self.wake(f"chat-{number}") for number in range(cap)]
            for number, thread_id in enumerate(threads):
                self.codex.emit(spawn_item(thread_id, f"child-{cap}-{number}"))
            self.assertTrue(wait_until(lambda cap=cap: self.host.chat_for_thread(f"child-{cap}-{cap - 1}")))
            self.assertEqual(len(self.host.live_chats()), cap)


def spawn_item(parent: str, child: str) -> dict:
    return {"method": "item/started", "params": {"threadId": parent, "turnId": "turn-1", "item": {
        "type": "collabAgentToolCall", "id": "item-1", "senderThreadId": parent, "receiverThreadIds": [child]}}}


class ReaderTests(HostTest):
    def test_a_line_of_one_chat_never_reaches_the_session_of_another(self) -> None:
        self.start()
        first, second = self.begin("chat-a"), self.begin("chat-b")
        thread_a, thread_b = self.host.chat("chat-a").thread_id, self.host.chat("chat-b").thread_id
        for number in range(20):
            thread_id = thread_a if number % 2 else thread_b
            self.codex.emit({"method": "item/completed", "params": {"threadId": thread_id, "n": number}})
        self.assertTrue(wait_until(lambda: len(first.received) + len(second.received) == 20))
        self.assertEqual({m["params"]["threadId"] for m in first.received}, {thread_a})
        self.assertEqual({m["params"]["threadId"] for m in second.received}, {thread_b})
        self.assertEqual([m["params"]["n"] for m in first.received], list(range(1, 20, 2)))

    def test_a_reply_goes_to_the_session_that_sent_the_request(self) -> None:
        self.start()
        first, second = self.begin("chat-a"), self.begin("chat-b")
        self.codex.emit({"id": "worker-chat-b-1", "result": {"turn": {"id": "turn-b"}}})
        self.codex.emit({"id": "worker-chat-a-1", "result": {"turn": {"id": "turn-a"}}})
        self.codex.emit({"id": "worker-gone-1", "result": {}})  # Nobody sent it: dropped.
        self.assertTrue(wait_until(lambda: first.received and second.received))
        time.sleep(0.05)
        self.assertEqual([m["id"] for m in first.received], ["worker-chat-a-1"])
        self.assertEqual([m["id"] for m in second.received], ["worker-chat-b-1"])

    def test_a_question_for_a_chat_with_no_running_exchange_gets_an_error(self) -> None:
        self.start()
        thread_id = self.wake("chat-a")  # Live, but no exchange.
        for params in ({"threadId": thread_id}, {"threadId": "unknown-thread"}, {}):
            with self.subTest(params=params):
                request_id = f"codex-{len(self.codex.sent)}"
                self.codex.emit({"id": request_id, "method": "item/commandExecution/requestApproval", "params": params})
                self.assertTrue(wait_until(lambda request_id=request_id: any(m.get("id") == request_id for m in self.codex.sent)))
                answer = next(m for m in self.codex.sent if m.get("id") == request_id)
                self.assertEqual(answer["error"], codex_host.UNSUPPORTED)

    def test_a_question_after_the_exchange_ended_gets_an_error(self) -> None:
        self.start()
        session = self.begin("chat-a")
        self.host.detach("chat-a")
        thread_id = self.host.chat("chat-a").thread_id
        self.codex.emit({"id": "codex-late", "method": "item/fileChange/requestApproval", "params": {"threadId": thread_id}})
        self.assertTrue(wait_until(lambda: any(m.get("id") == "codex-late" for m in self.codex.sent)))
        self.assertEqual(session.received, [])

    def test_no_line_is_lost_when_a_session_attaches_or_detaches(self) -> None:
        self.start()
        thread_id = self.wake("chat-a")
        sessions = []
        for round_number in range(5):
            session = FakeSession(f"worker-round-{round_number}")
            self.host.attach("chat-a", session)
            sessions.append(session)
            self.codex.emit({"method": "item/completed", "params": {"threadId": thread_id, "n": round_number}})
            self.assertTrue(wait_until(lambda session=session: len(session.received) == 1))
            self.host.detach("chat-a")
        self.assertEqual([s.received[0]["params"]["n"] for s in sessions], list(range(5)))

    def test_a_sub_agent_line_reaches_its_chat_session(self) -> None:
        self.start()
        session = self.begin("chat-a")
        parent = self.host.chat("chat-a").thread_id
        self.codex.emit(spawn_item(parent, "child-1"))
        self.codex.emit({"method": "item/completed", "params": {"threadId": "child-1", "n": 1}})
        self.codex.emit({"id": "codex-child", "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "child-1"}})
        self.assertTrue(wait_until(lambda: len(session.received) == 3))
        self.assertEqual(self.host.chat_for_thread("child-1"), "chat-a")
        self.assertFalse(any(m.get("id") == "codex-child" for m in self.codex.sent))

    def test_a_sub_agent_cannot_take_over_a_thread_of_another_chat(self) -> None:
        self.start()
        self.begin("chat-a")
        thread_b = self.wake("chat-b")
        self.codex.emit(spawn_item(self.host.chat("chat-a").thread_id, thread_b))
        self.codex.emit({"method": "item/completed", "params": {"threadId": "marker"}})
        time.sleep(0.05)
        self.assertEqual(self.host.chat_for_thread(thread_b), "chat-b")

    def test_a_pause_forgets_the_chat_sub_agents(self) -> None:
        self.start()
        self.begin("chat-a")
        self.codex.emit(spawn_item(self.host.chat("chat-a").thread_id, "child-1"))
        self.assertTrue(wait_until(lambda: self.host.chat_for_thread("child-1")))
        self.host.detach("chat-a")
        self.host.pause("chat-a")
        self.assertEqual(self.host.chat_for_thread("child-1"), "")

    def test_a_session_error_ends_that_exchange_only(self) -> None:
        self.start()
        first, second = self.begin("chat-a"), self.begin("chat-b")
        first.raise_on = "item/permissions/bad"
        self.codex.emit({"method": "item/permissions/bad", "params": {"threadId": self.host.chat("chat-a").thread_id}})
        self.codex.emit({"method": "item/completed", "params": {"threadId": self.host.chat("chat-b").thread_id}})
        self.assertTrue(wait_until(lambda: first.failed and second.received))
        self.assertEqual(second.failed, "")
        self.assertTrue(self.host.running())

    def test_lines_that_are_not_json_objects_are_skipped(self) -> None:
        self.start()
        session = self.begin("chat-a")
        self.codex.emit("not json\n")
        self.codex.emit("[1, 2]\n")
        self.codex.emit({"method": "item/completed", "params": {"threadId": self.host.chat("chat-a").thread_id}})
        self.assertTrue(wait_until(lambda: len(session.received) == 1))
        self.assertTrue(self.host.running())


class ExchangeTests(HostTest):
    def test_a_changed_session_file_pauses_and_wakes_the_chat_before_the_turn(self) -> None:
        self.start()
        self.begin("chat-a")
        self.host.detach("chat-a")
        thread_id = self.host.chat("chat-a").thread_id
        Path(self.tmp.name, f"{thread_id}.jsonl").write_text("a turn from another process\n")
        self.host.attach("chat-a", FakeSession("worker-next"))
        self.assertEqual([m["params"]["threadId"] for m in self.codex.methods("thread/unsubscribe")], [thread_id])
        self.assertEqual([m["params"]["threadId"] for m in self.codex.methods("thread/resume")], [thread_id])
        self.assertIn("session file changed", " ".join(self.logs))

    def test_an_unchanged_session_file_wakes_nothing(self) -> None:
        self.start()
        self.begin("chat-a")
        Path(self.tmp.name, f"{self.host.chat('chat-a').thread_id}.jsonl").write_text("this exchange's own turn\n")
        self.host.detach("chat-a")
        self.host.attach("chat-a", FakeSession("worker-next"))
        self.assertEqual(self.codex.methods("thread/unsubscribe"), [])

    def test_attach_to_a_paused_chat_or_a_chat_that_works_is_refused(self) -> None:
        self.start()
        with self.assertRaises(HostError):
            self.host.attach("chat-a", FakeSession("worker-x"))
        self.begin("chat-b")
        with self.assertRaises(HostError):
            self.host.attach("chat-b", FakeSession("worker-y"))

    def test_a_cancel_stops_one_chat_and_the_other_goes_on(self) -> None:
        self.start()
        first, second = self.begin("chat-a"), self.begin("chat-b")
        thread_a = self.host.chat("chat-a").thread_id
        self.codex.emit({"method": "turn/started", "params": {"threadId": thread_a, "turn": {"id": "turn-a"}}})
        self.assertTrue(wait_until(lambda: self.host.chat("chat-a").turn_running))
        threading.Timer(0.1, self.codex.emit, [{"method": "turn/completed", "params": {
            "threadId": thread_a, "turn": {"id": "turn-a", "status": "interrupted"}}}]).start()
        self.assertTrue(self.host.cancel("chat-a", timeout=WAIT))
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertFalse(self.host.restart_due)
        self.assertFalse(self.host.chat("chat-a").working)

    def test_a_canceled_turn_that_does_not_end_makes_a_restart_due(self) -> None:
        self.start()
        self.begin("chat-a")
        thread_a = self.host.chat("chat-a").thread_id
        self.codex.emit({"method": "turn/started", "params": {"threadId": thread_a, "turn": {"id": "turn-a"}}})
        self.assertTrue(wait_until(lambda: self.host.chat("chat-a").turn_running))
        self.assertFalse(self.host.cancel("chat-a", timeout=0.1))
        self.assertTrue(self.host.restart_due)

    def test_a_canceled_turn_that_was_asked_for_but_not_started_is_waited_for(self) -> None:
        self.start()
        session = self.begin("chat-a")
        session.turn_running = True  # turn/start was sent; turn/started has not arrived.
        self.assertFalse(self.host.cancel("chat-a", timeout=0.1))
        self.assertTrue(self.host.restart_due)

    def test_a_sub_agent_turn_does_not_end_its_chat_turn(self) -> None:
        self.start()
        self.begin("chat-a")
        parent = self.host.chat("chat-a").thread_id
        self.codex.emit({"method": "turn/started", "params": {"threadId": parent, "turn": {"id": "turn-a"}}})
        self.codex.emit(spawn_item(parent, "child-1"))
        self.codex.emit({"method": "turn/completed", "params": {"threadId": "child-1", "turn": {"id": "turn-c"}}})
        self.codex.emit({"method": "item/completed", "params": {"threadId": "marker"}})
        time.sleep(0.05)
        self.assertTrue(self.host.chat("chat-a").turn_running)


class FailurePathTests(HostTest):
    """What the host does when a wake, a stop or a start goes wrong part of the way."""

    def test_a_crash_while_the_tools_start_fails_the_wake(self) -> None:
        self.start()
        self.codex.crash_after_thread = True
        with self.assertRaisesRegex(HostError, "stopped while the chat woke"):
            self.wake("chat-a")
        self.assertEqual(self.host.live_chats(), [])
        with self.assertRaises(HostError):
            self.host.attach("chat-a", FakeSession("worker-a"))
        self.assertTrue(wait_until(lambda: len(self.processes) == 2 and self.host.running()))
        self.assertEqual(self.codex.methods("thread/resume"), [])

    def test_a_chat_whose_tools_fail_is_unloaded_so_the_cap_holds(self) -> None:
        self.start()
        self.codex.tool_status = "failed"
        for number in range(5):
            with self.assertRaises(HostError):
                self.wake(f"chat-{number}")
        self.assertEqual(len(self.codex.methods("thread/start")), 5)
        self.assertEqual(self.codex.loaded, set())
        self.assertFalse(self.host.restart_due)

    def test_a_chat_whose_tools_are_late_is_unloaded(self) -> None:
        self.start()
        self.codex.tool_status = "starting"
        with self.assertRaisesRegex(HostError, "not ready in time"):
            self.wake("chat-a")
        self.assertEqual(self.codex.loaded, set())

    def test_a_failed_wake_that_does_not_unload_makes_a_restart_due(self) -> None:
        self.start()
        self.codex.tool_status = "failed"
        self.codex.no_closed = True
        with self.assertRaises(HostError):
            self.wake("chat-a")
        self.assertTrue(self.host.restart_due)

    def test_a_restart_never_fails_an_exchange_that_starts_during_the_check(self) -> None:
        lock = ReleaseHookLock()
        self.host = self.make_host(lock=lock)
        self.start()
        self.wake("chat-a")
        self.rows["rules"] = "edited-agents-md"
        session = FakeSession("worker-a")
        attached: list[str] = []

        def attach_now() -> None:
            # A second message thread starts an exchange the moment the restart check lets go of the lock.
            try:
                self.host.attach("chat-a", session)
                attached.append("attached")
            except HostError as exc:
                attached.append(str(exc))

        def log(line: str) -> None:
            self.logs.append(line)
            if "restart due" in line:
                lock.after_release = attach_now

        self.host.log = log
        self.host.ready_for_message()
        # The check and the stop are one step: the exchange finds the chat paused and takes the start path.
        self.assertEqual(attached, ["the chat is not live"])
        self.assertEqual(session.failed, "")
        self.assertEqual(len(self.processes), 2)

    def test_the_owner_switch_off_waits_for_the_running_exchange(self) -> None:
        self.start()
        session = self.begin("chat-a")
        self.on = False
        self.assertFalse(self.host.ready_for_message())
        self.assertEqual(session.failed, "")
        self.assertTrue(self.host.running())
        self.host.detach("chat-a")  # The exchange ends: now the process stops.
        self.assertFalse(self.host.running())
        self.assertTrue(self.processes[0].reaped)

    def test_a_sign_out_waits_for_the_running_exchange(self) -> None:
        self.start()
        session = self.begin("chat-a")
        self.rows["sign_in"] = None
        self.assertFalse(self.host.ready_for_message())
        self.assertEqual(session.failed, "")
        self.host.detach("chat-a")
        self.assertFalse(self.host.running())
        self.assertEqual(len(self.processes), 1)

    def test_a_stop_that_waits_is_dropped_when_the_switch_is_on_again(self) -> None:
        self.start()
        self.begin("chat-a")
        self.on = False
        self.assertFalse(self.host.ready_for_message())
        self.on = True
        self.assertTrue(self.host.ready_for_message())
        self.host.detach("chat-a")
        self.assertTrue(self.host.running())
        self.assertFalse(self.processes[0].reaped)

    def test_a_failed_initialize_never_stops_the_next_process(self) -> None:
        def first_never_answers(process: FakeCodex) -> None:
            if process.number == 1:
                process.silent.add("initialize")

        self.on_spawn = first_never_answers
        self.assertFalse(self.host.ready_for_message())
        self.assertTrue(wait_until(lambda: len(self.processes) == 2 and self.host.running()))
        time.sleep(0.05)
        self.assertTrue(self.host.running())
        self.assertFalse(self.processes[1].reaped)

    def test_a_broken_pipe_at_the_start_is_a_failed_start(self) -> None:
        self.on_spawn = lambda process: process.broken.add("initialized")
        self.assertFalse(self.host.ready_for_message())
        self.assertFalse(self.host.running())
        self.assertTrue(self.processes[0].reaped)

    def test_a_stop_of_an_old_process_leaves_the_new_one(self) -> None:
        self.start()
        self.host.stop("an old caller", generation=0)
        self.assertTrue(self.host.running())

    def test_a_tool_notice_of_an_unknown_thread_does_not_count(self) -> None:
        self.start()
        self.codex.tool_status = "starting"
        self.codex.after_thread = [{"method": "mcpServer/startupStatus/updated",
                                    "params": {"name": name, "status": "ready", "threadId": "unknown-thread"}}
                                   for name in TOOLS]
        with self.assertRaisesRegex(HostError, "not ready in time"):
            self.wake("chat-a")
        self.assertEqual(self.host.live_chats(), [])

    def test_a_tool_notice_of_the_chat_own_thread_counts(self) -> None:
        self.start()
        self.codex.notice_thread = True
        self.wake("chat-a")
        self.assertEqual(self.host.live_chats(), ["chat-a"])

    def test_a_stop_waits_for_the_reader_threads(self) -> None:
        self.start()
        readers = list(self.host._readers)
        self.host.stop("test")
        self.assertFalse(any(reader.is_alive() for reader in readers))


if __name__ == "__main__":
    unittest.main()
