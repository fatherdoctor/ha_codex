"""run_task on the live Codex process (design CX-SPEED cut 3, step 5): option on, option off, crash, restart, cancel.

The live process is a fake app-server that answers many exchanges on one connection, as `codex app-server` does.
The host is the worker's own (server.make_live_codex) with short time limits, so a failure ends in seconds.
"""

import json
import queue
import tempfile
import threading
import unittest
import uuid
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from unittest.mock import patch

from app_server_fixture import StubAppServer
from test_server import server

LIMITS = {"reply_timeout": 3.0, "ready_timeout": 3.0}


class LiveCodex:
    """A fake `codex app-server` that keeps its threads between exchanges.

    tool_status is what each tool program reports after a thread loads; tool_statuses, when given, is used up
    first, one per load. hold keeps each turn open until the worker interrupts it. crash_on_turn ends the output
    when a turn starts, as a process that dies would, after it writes error_lines to its error output. deaf ignores
    turn/interrupt. interrupt_delay ends an interrupted turn that many seconds late.
    """

    def __init__(self, *, tool_status: str = "ready", hold: bool = False, crash_on_turn: bool = False,
                 deaf: bool = False, tool_statuses: tuple[str, ...] = (), error_lines: tuple[str, ...] = (),
                 interrupt_delay: float = 0.0) -> None:
        self.tool_status = tool_status
        self.tool_statuses = list(tool_statuses)
        self.hold = hold
        self.crash_on_turn = crash_on_turn
        self.deaf = deaf  # Ignore turn/interrupt, as a turn that hangs would.
        self.interrupt_delay = interrupt_delay
        self.error_lines = error_lines
        self.sent: list[dict] = []
        self.lines: queue.Queue[str] = queue.Queue()
        self.stdin = self
        self.stdout = self
        self.stderr = _Errors()
        self.returncode: int | None = None
        self.turns = 0
        self.open_turns: dict[str, str] = {}  # turn id -> thread id

    def methods(self, name: str) -> list[dict]:
        return [message for message in self.sent if message.get("method") == name]

    def emit(self, **message) -> None:
        self.lines.put(json.dumps(message) + "\n")

    def write(self, line: str) -> int:
        message = json.loads(line)
        self.sent.append(message)
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        if request_id is None:
            return len(line)
        if method in ("thread/start", "thread/resume"):
            thread_id = params.get("threadId") or str(uuid.uuid4())
            self.emit(id=request_id, result={"thread": {"id": thread_id}})
            status = self.tool_statuses.pop(0) if self.tool_statuses else self.tool_status
            for name in server.live_tool_programs():
                self.emit(method="mcpServer/startupStatus/updated",
                          params={"name": name, "status": status, "threadId": thread_id})
        elif method == "turn/start":
            self.start_turn(request_id, params["threadId"])
        elif method == "turn/interrupt" and self.deaf:
            pass
        elif method == "turn/interrupt":
            self.emit(id=request_id, result={})
            thread_id = self.open_turns.pop(params["turnId"], params["threadId"])
            completed = partial(self.emit, method="turn/completed", params={
                "threadId": thread_id, "turn": {"id": params["turnId"], "status": "interrupted"}})
            if self.interrupt_delay:
                threading.Timer(self.interrupt_delay, completed).start()
            else:
                completed()
        elif method == "thread/unsubscribe":
            self.emit(id=request_id, result={})
            self.emit(method="thread/closed", params={"threadId": params["threadId"]})
        else:
            self.emit(id=request_id, result={})
        return len(line)

    def start_turn(self, request_id, thread_id: str) -> None:
        self.turns += 1
        turn = {"id": f"live-turn-{self.turns}"}
        self.emit(id=request_id, result={"turn": turn})
        self.emit(method="turn/started", params={"threadId": thread_id, "turn": turn})
        if self.crash_on_turn:
            threading.Thread(target=self.crash, daemon=True).start()
            return
        if self.hold:
            self.open_turns[turn["id"]] = thread_id
            return
        context = {"threadId": thread_id, "turnId": turn["id"]}
        self.emit(method="item/completed", params={**context, "item": {
            "id": f"answer-{self.turns}", "type": "agentMessage", "phase": "final_answer",
            "text": json.dumps({"status": "completed", "summary": f"Answer {self.turns}"})}})
        self.emit(method="turn/completed", params={"threadId": thread_id, "turn": {**turn, "status": "completed"}})

    def crash(self) -> None:
        """Write error_lines to the error output, then end the output once the host has read them.

        On a thread of its own: the worker writes turn/start under the lock that the host's error reader takes.
        """
        for line in self.error_lines:
            self.stderr.lines.put(line + "\n")
        self.stderr.lines.join()
        self.lines.put("")

    def readline(self) -> str:
        return self.lines.get(timeout=10)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self.lines.put("")
        self.stderr.lines.put("")

    kill = terminate


class _Errors:
    """The error output of the fake: the lines put in it, then an end when the process stops."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.taken = False

    def readline(self) -> str:
        # A line counts as read (lines.join) when the reader comes back for the next one: it has stored it then.
        if self.taken:
            self.taken = False
            self.lines.task_done()
        try:
            line = self.lines.get(timeout=10)
        except queue.Empty:
            return ""
        self.taken = True
        return line


# An invented live model list: (model id, display name, reasoning levels).
INVENTED_LIVE_MODELS = (("invented-default-model", "Invented Default", ("low", "medium", "high")),)


class LiveExchangeTests(unittest.TestCase):
    """Run the real run_task with the worker's host on a fake live process."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(server.tasks, clear=True))
        for name in ["running_processes", "approval_sessions", "task_activity", "admin_cache"]:
            self.stack.enter_context(patch.object(server, name, {}))
        self.options = {**server.DEFAULT_OPTIONS, "local_docs": False, "task_timeout_seconds": 30,
                        "config_check": False, "auto_save_lovelace": False}
        self.stack.enter_context(patch.object(server, "read_options", side_effect=lambda: dict(self.options)))
        self.stack.enter_context(patch.object(server.store, "task_dir", return_value=self.root / "tasks" / "task"))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.agents = self.root / "AGENTS.md"
        self.agents.write_text("House rules, version 1.\n", encoding="utf-8")
        self.stack.enter_context(patch.object(server, "LEGACY_AGENTS_PATH", self.agents))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server.store, "save_index"))
        self.stack.enter_context(patch.object(server, "build_manifest", return_value={}))
        self.stack.enter_context(patch.object(server, "sandbox_readiness", return_value={"required": True, "ready": True}))
        self.stack.enter_context(patch.object(server, "fire_ha_event", return_value=(True, "")))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "notify_permission_async"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        (self.root / "codex").mkdir()
        (self.root / "codex" / "auth.json").write_text("{}", encoding="utf-8")  # Signed in: invented content.
        self.host = server.make_live_codex(**LIMITS)
        self.addCleanup(self.host.close)
        self.stack.enter_context(patch.object(server, "live_codex", self.host))
        self.spawned: list[LiveCodex] = []
        self.next_live = LiveCodex
        self.stack.enter_context(patch.object(server, "spawn_live_process", side_effect=self.spawn))
        self.started_alone: list[StubAppServer] = []
        self.stack.enter_context(patch.object(server.subprocess, "Popen", side_effect=self.start_alone))
        self.turn = 0

    def spawn(self, args, env):
        process = self.next_live()
        self.spawned.append(process)
        return process

    def start_alone(self, *args, **kwargs):
        """The start path of today: a process for one exchange."""
        process = StubAppServer("019fc242-910a-7c92-a17d-54c014e19fc4",
                                {"status": "completed", "summary": "Answer on its own process"})
        self.started_alone.append(process)
        return process

    def message(self, session_id=None, model=None) -> dict:
        """Queue one message in the chat and run it, as start_background_task does; model as the turn resolved it."""
        self.turn += 1
        turn_id = f"worker-turn-{self.turn}"
        task = server.tasks.setdefault("task", {"task_id": "task", "turns": []})
        task.update(status="queued", current_turn_id=turn_id, cancellation_requested=False)
        turn = {"turn_id": turn_id, "message": f"Message {self.turn}"}
        if model:
            turn["execution_settings"] = {"model": model, "reasoning_effort": "medium"}
        task["turns"].append(turn)
        server.run_task("task", f"Message {self.turn}", session_id)
        return dict(server.tasks["task"])

    def log(self) -> str:
        path = server.get_task_dir("task") / "codex.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_follow_up_is_one_turn_on_the_same_process(self):
        first = self.message()
        self.assertEqual(first["status"], "completed", first.get("error"))
        session_id = first["session_id"]
        second = self.message(session_id)
        self.assertEqual(second["status"], "completed", second.get("error"))
        self.assertEqual(len(self.spawned), 1)
        live = self.spawned[0]
        self.assertEqual(len(live.methods("initialize")), 1)
        self.assertEqual(len(live.methods("thread/start")), 1)
        self.assertEqual(live.methods("thread/resume"), [])
        self.assertEqual([turn["params"]["threadId"] for turn in live.methods("turn/start")], [session_id] * 2)
        self.assertEqual(self.started_alone, [])
        self.assertIsNone(live.returncode, "the live process stays up after the exchange")
        self.assertEqual(self.host.live_chats(), ["task"])
        self.assertNotIn("task", server.running_processes)

    def test_option_off_starts_codex_for_each_message(self):
        self.options["warm_start"] = False
        for _ in range(2):
            self.assertEqual(self.message()["status"], "completed")
        self.assertEqual(self.spawned, [])
        self.assertEqual(len(self.started_alone), 2)
        self.assertFalse(self.host.running())

    def test_signed_out_starts_codex_for_the_message(self):
        (self.root / "codex" / "auth.json").unlink()
        self.assertEqual(self.message()["status"], "completed")
        self.assertEqual(self.spawned, [])
        self.assertEqual(len(self.started_alone), 1)

    def test_failed_tool_programs_fall_back_to_a_process_for_the_message(self):
        self.next_live = lambda: LiveCodex(tool_status="failed")
        task = self.message()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(self.started_alone), 1)
        self.assertIn("The live Codex process could not take this message (tool programs failed", self.log())
        self.assertEqual(self.spawned[0].methods("turn/start"), [], "no turn ran on the live process")

    def test_changed_agents_file_restarts_the_process_before_the_next_message(self):
        session_id = self.message()["session_id"]
        self.agents.write_text("House rules, version 2.\n", encoding="utf-8")
        with patch("builtins.print") as printed:
            self.assertEqual(self.message(session_id)["status"], "completed")
        self.assertEqual(len(self.spawned), 2)
        old, new = self.spawned
        self.assertIsNotNone(old.returncode, "the old process was stopped")
        self.assertEqual(old.methods("turn/start")[1:], [], "the second message did not run on the old process")
        self.assertEqual(new.methods("thread/resume")[0]["params"]["threadId"], session_id)
        self.assertEqual(len(new.methods("turn/start")), 1)
        lines = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("restart due, changed: rules", lines)
        self.assertNotIn("version 2", lines, "the log names the row, never its value")

    def test_crash_fails_the_running_message_and_the_next_one_gets_a_new_process(self):
        # Only the first process crashes: the host starts the next one at once, before this test goes on.
        self.next_live = lambda: LiveCodex(crash_on_turn=not self.spawned)
        crashed = self.message()
        self.assertEqual(crashed["status"], "failed")
        self.assertEqual(self.started_alone, [], "a turn that started is never run a second time")
        self.assertEqual(self.message()["status"], "completed")
        self.assertEqual(len(self.spawned), 2)

    def test_timeout_interrupts_only_the_turn(self):
        self.options["task_timeout_seconds"] = 1
        self.next_live = lambda: LiveCodex(hold=True)
        task = self.message()
        self.assertEqual(task["status"], "failed")
        self.assertIn("timed out", task["summary"])
        live = self.spawned[0]
        self.assertEqual(len(live.methods("turn/interrupt")), 1)
        self.assertIsNone(live.returncode, "the process stays up for the other chats")
        self.assertFalse(self.host.restart_due)
        self.assertEqual(self.host.live_chats(), ["task"])

    def test_a_turn_that_does_not_stop_gets_a_new_process(self):
        self.options["task_timeout_seconds"] = 1
        self.next_live = lambda: LiveCodex(hold=True, deaf=True)
        # codex_host.cancel waits 10 seconds for the interrupted turn; the test waits half a second.
        self.stack.enter_context(patch.object(self.host, "cancel", partial(self.host.cancel, timeout=0.5)))
        self.assertEqual(self.message()["status"], "failed")
        self.assertEqual(len(self.spawned[0].methods("turn/interrupt")), 1)
        # The restart was due when the exchange ended, and no other chat worked: it ran at once.
        self.assertIsNotNone(self.spawned[0].returncode, "the process with the hung turn was stopped")
        self.assertEqual(len(self.spawned), 2)

    def wait_for(self, condition, seconds: float = 5.0) -> None:
        stop = threading.Event()
        for _ in range(int(seconds / 0.02)):
            if condition():
                return
            stop.wait(0.02)
        self.fail("the condition did not come true in time")

    def test_cancel_interrupts_only_the_turn_and_tidies_after_it_ends(self):
        self.next_live = lambda: LiveCodex(hold=True, interrupt_delay=0.5)
        tidied: list[bool] = []  # For each tidy of secret copies: whether the chat's turn was still running.
        drop = server.drop_credential_backups
        self.stack.enter_context(patch.object(server, "drop_credential_backups", side_effect=lambda path: (
            tidied.append(self.host.chat("task").turn_running), drop(path))[1]))
        runner = threading.Thread(target=self.message)
        runner.start()
        self.wait_for(lambda: self.spawned and self.spawned[0].open_turns)
        response = server.app.test_client().post("/tasks/task/cancel", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(tidied, [], "the cancel route leaves the tidy to the end of the turn")
        runner.join(10)
        self.assertFalse(runner.is_alive())
        live = self.spawned[0]
        self.assertEqual([call["params"]["threadId"] for call in live.methods("turn/interrupt")],
                         [live.methods("turn/start")[0]["params"]["threadId"]])
        self.assertEqual(tidied, [False], "one tidy, after the turn ended")
        self.assertEqual(server.tasks["task"]["status"], "cancelled")
        self.assertIsNone(live.returncode, "the process stays up for the other chats")
        self.assertFalse(self.host.restart_due)
        self.assertEqual(self.host.live_chats(), ["task"])
        self.assertEqual(self.started_alone, [])

    def test_stall_interrupts_only_the_turn(self):
        self.options["task_stall_seconds"] = 1
        self.next_live = lambda: LiveCodex(hold=True)
        self.stack.enter_context(patch.object(server, "wait_for_exchange",
                                              partial(server.wait_for_exchange, poll=0.1)))
        task = self.message()
        self.assertEqual(task["status"], "failed")
        self.assertIn("stopped responding", task["summary"])
        live = self.spawned[0]
        self.assertEqual(len(live.methods("turn/interrupt")), 1)
        self.assertIsNone(live.returncode, "the process stays up for the other chats")
        self.assertFalse(self.host.restart_due)
        self.assertEqual(self.host.live_chats(), ["task"])
        self.assertIn("No activity from Codex for 1 seconds", self.log())

    def test_a_failed_message_gets_the_error_output_of_the_live_process(self):
        self.next_live = lambda: LiveCodex(crash_on_turn=True, error_lines=("invented failure: model gone",))
        self.assertEqual(self.message()["status"], "failed")
        lines = [line for line in self.log().splitlines() if "invented failure: model gone" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("stderr", lines[0])

    def test_after_one_failed_start_the_next_message_runs_on_the_live_process(self):
        self.next_live = lambda: LiveCodex(tool_statuses=("failed",))
        first = self.message()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(len(self.started_alone), 1, "the first message took the start path")
        second = self.message(first["session_id"])
        self.assertEqual(second["status"], "completed", second.get("error"))
        self.assertEqual(len(self.started_alone), 1, "the second message ran on the live process")
        live = self.spawned[0]
        self.assertEqual([turn["params"]["threadId"] for turn in live.methods("turn/start")], [first["session_id"]])
        self.assertNotIn("holds another conversation", self.log())

    def test_a_chat_in_another_conversation_never_runs_on_the_loaded_thread(self):
        loaded = self.message()["session_id"]
        other = "019fc242-910a-7c92-a17d-54c014e19fc5"  # Invented: the chat went on in this thread elsewhere.
        self.assertEqual(self.message(other)["status"], "completed")
        live = self.spawned[0]
        self.assertEqual([turn["params"]["threadId"] for turn in live.methods("turn/start")], [loaded, other])
        self.assertIn(loaded, [call["params"]["threadId"] for call in live.methods("thread/unsubscribe")])
        self.assertEqual(live.methods("thread/resume")[-1]["params"]["threadId"], other)
        self.assertEqual(self.started_alone, [])

    def test_a_model_change_reaches_a_chat_that_is_live(self):
        session_id = self.message(model="invented-model-a")["session_id"]
        second = self.message(session_id, model="invented-model-b")
        self.assertEqual(second["status"], "completed", second.get("error"))
        live = self.spawned[0]
        self.assertEqual([turn["params"]["model"] for turn in live.methods("turn/start")],
                         ["invented-model-a", "invented-model-b"])
        self.assertEqual(live.methods("thread/resume"), [], "the chat stays loaded")
        self.assertEqual(self.started_alone, [])

    def test_a_chat_set_back_to_the_default_model_names_it(self):
        session_id = self.message(model="invented-model-a")["session_id"]
        with patch.dict(server.usage_state, {"_default_model": "invented-default-model"}), \
                patch.object(server, "live_models", return_value=INVENTED_LIVE_MODELS):
            self.assertEqual(self.message(session_id, model="default")["status"], "completed")
        turns = self.spawned[0].methods("turn/start")
        self.assertEqual([turn["params"]["model"] for turn in turns], ["invented-model-a", "invented-default-model"])

    def test_the_default_names_no_model_before_the_cli_named_its_pick(self):
        with patch.dict(server.usage_state, {"_default_model": ""}):
            self.assertEqual(self.message(model="default")["status"], "completed")
        self.assertNotIn("model", self.spawned[0].methods("turn/start")[0]["params"])

    def test_the_default_names_no_model_when_the_worker_does_not_know_the_cli_pick(self):
        # /status gives a display name, not a model id, for a model that neither model list has.
        with patch.dict(server.usage_state, {"_default_model": "Invented Display Name"}), \
                patch.object(server, "live_models", return_value=INVENTED_LIVE_MODELS):
            self.assertEqual(self.message(model="default")["status"], "completed")
        self.assertNotIn("model", self.spawned[0].methods("turn/start")[0]["params"])


if __name__ == "__main__":
    unittest.main()
