"""A scheduled run is a normal chat: the same tools, and the same question before each Home Assistant action.

The owner's rule: a schedule wakes a chat, and that chat can do what a chat on the page can do in the same
permission mode. So a scheduled run starts Codex with the page's command line, the home_assistant_call tool
included, and the worker answers its calls like a page chat's. Nothing runs unasked that a chat on the page would
ask about: Codex's question before a call becomes the same card, and it waits in the run's chat for a person.

KeptDifferenceTests holds the differences that remain between a scheduled run and a chat on the page. Each one is
a limit the app makes; removing one changes a test there.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_server import server  # First: it puts the worker's folder on sys.path.

# isort: split
import policy
import scheduling
import test_ha_call as ha_calls
import tool_threads
from approvals import AppServerSession
from ha_call import HomeAssistantCall
from test_approvals import Transport
from test_tool_card import QUESTION, tool_params

CALL_PREFIX = "mcp_servers.home_assistant_call."
THREAD = "33333333-2222-4333-8444-555555555555"
# What a schedule's mode saves on its run's task (schedule_api.Worker.submit), by the name the page shows.
SCHEDULE_MODES = {"Default": "default", "Read only": "read-only"}
# What a chat on the page saves on its task for the same mode.
PAGE_CHATS = {"Default": {"permission_mode": "default"}, "Read only": {"permission_mode": "read-only"}}


def scheduled_task(background_mode="default", **fields):
    """A task as a schedule starts it: its source is the schedule, and its mode is the schedule's."""
    return {"task_id": "t", "turns": [], "status": "running", "task_source": "schedule",
            "background_mode": background_mode, "chat_settings": server.DEFAULT_CHAT_SETTINGS, **fields}


def page_task(**fields):
    """A task as the chat page starts it."""
    return {"task_id": "t", "turns": [], "status": "running", "chat_settings": server.DEFAULT_CHAT_SETTINGS,
            **fields}


class LaunchParityTests(unittest.TestCase):
    """run_task starts a scheduled run with the command line of a chat on the page."""

    launch = ha_calls.RunTaskLaunchTests.launch

    def test_a_scheduled_run_starts_with_the_page_chats_command_line(self):
        self.assertEqual(self.launch("schedule")["args"], self.launch("page")["args"])

    def test_a_scheduled_run_gets_the_call_tool_and_must_ask_before_each_call(self):
        values = [value for value in ha_calls.configured(self.launch("schedule")["args"])
                  if value.startswith(CALL_PREFIX)]
        self.assertIn(CALL_PREFIX + 'tools.call.approval_mode="prompt"', values)
        self.assertFalse([value for value in values if "default_tools_approval_mode" in value])

    def test_a_scheduled_run_holds_its_key_in_the_worker_only(self):
        started = self.launch("schedule")
        self.assertTrue(started["key_held"])
        self.assertNotIn("HA_CALL_CAPABILITY", started["env"])
        self.assertNotIn("CAPABILITY", " ".join(started["args"]))

    def test_a_scheduled_run_is_told_to_change_the_house_through_the_tool(self):
        options = {"HA_TOKEN": "invented-token"}
        texts = {}
        for name, task in (("schedule", scheduled_task()), ("page", page_task())):
            with patch.dict(server.tasks, {"t": task}, clear=True), patch.object(server, "read_options", return_value=options):
                texts[name] = server.exchange_info("t", None)["home_assistant_api"]
        self.assertIn("home_assistant_call call tool", texts["schedule"])
        self.assertEqual(texts["schedule"], texts["page"])


class WorkerGateTests(ha_calls.CallTestCase):
    """Behind the socket, the worker sends a scheduled run's call under the same conditions as a page chat's."""

    def setUp(self):
        super().setUp()
        self.worker.tasks["run"] = scheduled_task(task_id="run")
        tool_threads.CHATS.bind("run", THREAD)
        self.addCleanup(tool_threads.CHATS.release, "run")
        self.addCleanup(tool_threads.CHATS.release, "t1")

    def call(self):
        return self.calls.dispatch({"thread_id": THREAD, "arguments": tool_params()})

    def test_a_running_scheduled_run_sends_its_call(self):
        self.calls.begin("run")
        self.assertEqual(self.call()["status"], "ok")
        self.assertEqual(len(self.fake.requests), 1)
        self.assertTrue(self.fake.requests[0]["url"].endswith("/light/turn_off"))

    def test_a_scheduled_run_that_ended_sends_nothing(self):
        self.calls.begin("run")
        self.calls.end("run")
        self.assertEqual(self.call()["status"], "unavailable")
        self.assertEqual(self.fake.requests, [])

    def test_a_cancelled_scheduled_run_sends_nothing(self):
        self.calls.begin("run")
        self.worker.task_cancellation_requested = lambda task_id: task_id == "run"
        self.assertEqual(self.call()["status"], "unavailable")
        self.assertEqual(self.fake.requests, [])


class ModeTestCase(unittest.TestCase):
    """Reads the Codex settings the worker would start a task's exchange with."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.full_access = self.enterContext(patch.object(server.full_access, "is_on", return_value=False))

    def session_settings(self, task, app_options=None):
        """Return the thread and turn settings create_approval_session passes to Codex for this task."""
        process = type("Process", (), {"stdin": Mock(), "poll": lambda _: None})()
        with patch.dict(server.tasks, {"t": task}, clear=True), \
                patch.object(server, "read_options", return_value=app_options or {}), \
                patch.object(server, "AppServerSession") as session:
            server.create_approval_session("t", process, "Turn off the kitchen light", self.root / "final", None, {},
                                           server.task_options("t"))
        return session.call_args.kwargs["thread_params"], session.call_args.kwargs["turn_params"]

    def mode_of(self, task, app_options=None):
        """Return the three settings that make a permission mode (policy.MODES) as Codex receives them."""
        thread, turn = self.session_settings(task, app_options)
        self.assertEqual((thread["approvalPolicy"], thread["approvalsReviewer"]),
                         (turn["approvalPolicy"], turn["approvalsReviewer"]))
        return {"sandbox": thread["sandbox"], "approval_policy": thread["approvalPolicy"],
                "approvals_reviewer": thread["approvalsReviewer"]}


class ModeParityTests(ModeTestCase):
    """Each mode a schedule offers gives its run the settings of a page chat in that mode."""

    def test_each_schedule_mode_runs_with_the_page_chats_settings(self):
        for name, background_mode in SCHEDULE_MODES.items():
            with self.subTest(mode=name):
                scheduled = self.mode_of(scheduled_task(background_mode))
                self.assertEqual(scheduled, self.mode_of(page_task(**PAGE_CHATS[name])))
                self.assertEqual(scheduled, policy.MODES[background_mode])

    def test_each_schedule_mode_leaves_the_question_to_a_person(self):
        """on-request with the user as reviewer: Codex asks, and no reviewer model answers for the person."""
        for name, background_mode in SCHEDULE_MODES.items():
            with self.subTest(mode=name):
                settings = self.mode_of(scheduled_task(background_mode))
                self.assertEqual((settings["approval_policy"], settings["approvals_reviewer"]), ("on-request", "user"))


class CardTests(ModeTestCase):
    """A call from a scheduled run waits for a card, as it does in a chat on the page."""

    def ask(self, task):
        """Start a session with the task's own settings and send Codex's question before a call."""
        thread, turn = self.session_settings(task)
        self.writer = Transport()
        self.saved = []
        session = AppServerSession(
            self.writer, lock=threading.RLock(), thread_params=thread, turn_params=turn,
            on_event=lambda event: None, on_approvals=self.saved.append, on_final=lambda text: None,
            is_active=lambda: True, redact=lambda text: text)
        session.start()
        session.receive({"id": "worker-1", "result": {}})
        session.receive({"id": "worker-2", "result": {
            "thread": {"id": "thread"}, "approvalPolicy": thread["approvalPolicy"],
            "approvalsReviewer": thread["approvalsReviewer"]}})
        session.receive({"id": "worker-3", "result": {"turn": {"id": "turn"}}})
        self.before = len(self.writer.messages())
        session.receive({"id": 7, "method": "mcpServer/elicitation/request", "params": {
            "threadId": "thread", "turnId": "turn", "serverName": "home_assistant_call", "mode": "form",
            "message": QUESTION, "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {"codex_approval_kind": "mcp_tool_call", "persist": ["session", "always"],
                      "tool_params": tool_params()}}})
        return session

    def answers(self):
        """What the worker has answered Codex since the question."""
        return self.writer.messages()[self.before:]

    def test_a_call_from_a_scheduled_run_waits_for_a_card(self):
        for name, background_mode in SCHEDULE_MODES.items():
            with self.subTest(mode=name):
                session = self.ask(scheduled_task(background_mode))
                card = self.saved[-1][-1]
                self.assertEqual((card["kind"], card["status"], card["tool"]),
                                 ("tool", "pending", "home_assistant_call.call"))
                self.assertTrue(session.waiting_for_user())
                self.assertEqual(self.answers(), [])  # Codex has no answer, so the call has not run.

    def test_the_card_of_a_scheduled_run_is_the_page_chats_card(self):
        self.ask(scheduled_task())
        scheduled = self.saved[-1][-1]
        self.ask(page_task())
        page = self.saved[-1][-1]
        identity = {"approval_id", "requested_at"}
        self.assertEqual({key: value for key, value in scheduled.items() if key not in identity},
                         {key: value for key, value in page.items() if key not in identity})

    def test_a_persons_answer_decides_the_call(self):
        for decision, action in (("allow", "accept"), ("deny", "decline")):
            with self.subTest(decision=decision):
                session = self.ask(scheduled_task())
                session.decide(self.saved[-1][-1]["approval_id"], decision)
                self.assertEqual(self.answers(), [{"id": 7, "result": {"action": action, "content": None,
                                                                         "_meta": None}}])

    def test_the_waiting_run_is_marked_like_any_chat_that_needs_permission(self):
        """The page's yellow warning reads permission_required of every chat, whoever started it."""
        waiting = [{"approval_id": "a", "status": "pending"}]
        for name, task in (("schedule", scheduled_task(approvals=waiting)), ("page", page_task(approvals=waiting))):
            with self.subTest(task=name):
                self.assertTrue(server.task_payload(task, summary=True)["permission_required"])


class KeptDifferenceTests(ModeTestCase):
    """What still differs between a scheduled run and a chat on the page; each is a limit the app makes."""

    def test_a_schedule_offers_default_and_read_only_only(self):
        """A schedule cannot be saved in Auto-review or Full access."""
        spec = {"type": "interval", "minutes": 60}
        for mode in ("auto-review", "full-access"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Mode must be default or read-only"):
                scheduling.validate({"name": "Evening check", "prompt": "Check the doors.", "mode": mode,
                                     "spec": spec}, 0)

    def test_a_scheduled_run_never_runs_in_full_access_or_auto_review(self):
        """Even with the Full access switch on and the mode saved on the task, the run uses Default.

        So a call from a scheduled run always waits for a card. In a Full access chat on the page Codex does not
        ask (approval policy "never"); a scheduled run cannot be put in that mode.
        """
        self.full_access.return_value = True
        for mode in ("full-access", "auto-review"):
            with self.subTest(mode=mode):
                self.assertEqual(self.mode_of(page_task(permission_mode=mode)), policy.MODES[mode])
                self.assertEqual(self.mode_of(scheduled_task(permission_mode=mode)), policy.MODES["default"])

    def test_a_scheduled_runs_question_sends_no_notification(self):
        """A permission request of a scheduled run notifies; a question it asks at the end of its turn does not."""
        for name, task, notified in (("schedule", scheduled_task(), False), ("page", page_task(), True)):
            with self.subTest(task=name), patch.dict(server.tasks, {"t": task}, clear=True), \
                    patch.object(server, "notify_question") as notify:
                server.notify_task_outcome("t", "needs_input", "", question="Which door?")
                self.assertEqual(notify.called, notified)


if __name__ == "__main__":
    unittest.main()
