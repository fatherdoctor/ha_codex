"""The four permission modes of the ChatGPT app: Default, Auto-review, Read only and Full access.

Each mode is only Codex's own settings, passed when the worker starts Codex for a chat turn (policy.MODES). The
worker keeps no check of its own on those settings. These tests hold the exact settings of each mode, the Full
access switch, the managed requirements written for it, and what the chat shows of each Auto-review decision.
"""
import json
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import approvals
import full_access
import ha_call
import tool_threads
import policy
import remote_control as rc
import test_approvals as approval_tests
import test_attachments as attachments
import test_cx_pair_f as admin
import test_ha_call as ha_calls
from app_folders import isolated_app_folders
from test_server import server

SHIPPED = ROOT / "gate" / "requirements.toml"

# The table of plan section 3, written out again here so that a change to policy.MODES fails a test.
EXPECTED_MODES = {
    "default": {"sandbox": "workspace-write", "approval_policy": "on-request", "approvals_reviewer": "user"},
    "auto-review": {"sandbox": "workspace-write", "approval_policy": "on-request", "approvals_reviewer": "auto_review"},
    "read-only": {"sandbox": "read-only", "approval_policy": "on-request", "approvals_reviewer": "user"},
    "full-access": {"sandbox": "danger-full-access", "approval_policy": "never", "approvals_reviewer": "user"},
}


class ModeTableTests(unittest.TestCase):
    """Each mode is exactly three Codex settings; the app's sandbox option can narrow the sandboxed modes."""

    def test_the_table_is_the_plan(self):
        self.assertEqual(policy.MODES, EXPECTED_MODES)
        for mode, settings in EXPECTED_MODES.items():
            with self.subTest(mode=mode):
                self.assertEqual(policy.mode_settings(mode), settings)

    def test_a_read_only_app_option_narrows_default_and_auto_review(self):
        for mode in ("default", "auto-review", "read-only"):
            with self.subTest(mode=mode):
                self.assertEqual(policy.mode_settings(mode, "read-only")["sandbox"], "read-only")
        self.assertEqual(policy.mode_settings("full-access", "read-only")["sandbox"], "danger-full-access")

    def test_an_unknown_mode_gets_default(self):
        for mode in ("", "never", "danger-full-access", "Full access"):
            with self.subTest(mode=mode):
                self.assertEqual(policy.mode_settings(mode), EXPECTED_MODES["default"])

    def test_the_removed_checks_are_gone(self):
        """No add-on check of its own on the mode settings is left (plan section 3, David 18:42)."""
        self.assertFalse(hasattr(approvals, "require_supported_policy"))
        self.assertFalse(hasattr(rc, "requirements_problem"))
        self.assertFalse(hasattr(policy, "effective_policy"))
        source = (ROOT / "policy.py").read_text(encoding="utf-8")
        self.assertNotIn('must keep approvals_reviewer', source)
        self.assertNotIn('may not set approval_policy', source)


class RequirementsTests(unittest.TestCase):
    """Codex's managed requirements list the four modes' values; Full access's two only while the switch is on."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patcher = patch.object(full_access, "SETTING_PATH", self.root / "full_access.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def written(self):
        target = self.root / "etc" / "requirements.toml"
        target.parent.mkdir(exist_ok=True)
        self.assertTrue(full_access.write_requirements(target=target, shipped=SHIPPED))
        return target.read_text(encoding="utf-8")

    def test_off_writes_the_shipped_file_unchanged(self):
        text = self.written()
        self.assertEqual(text, SHIPPED.read_text(encoding="utf-8"))
        data = tomllib.loads(text)
        self.assertEqual(data["allowed_sandbox_modes"], ["read-only", "workspace-write"])
        self.assertEqual(data["allowed_approval_policies"], ["on-request"])
        self.assertEqual(data["allowed_approvals_reviewers"], ["user", "auto_review"])

    def test_on_adds_exactly_full_access(self):
        full_access.set_enabled(True, "admin-fixture")
        data = tomllib.loads(self.written())
        self.assertEqual(data["allowed_sandbox_modes"], ["read-only", "workspace-write", "danger-full-access"])
        self.assertEqual(data["allowed_approval_policies"], ["on-request", "never"])
        self.assertEqual(data["allowed_approvals_reviewers"], ["user", "auto_review"])

    def test_every_mode_value_is_listed_when_on(self):
        full_access.set_enabled(True, "admin-fixture")
        data = tomllib.loads(self.written())
        for mode, settings in EXPECTED_MODES.items():
            with self.subTest(mode=mode):
                self.assertIn(settings["sandbox"], data["allowed_sandbox_modes"])
                self.assertIn(settings["approval_policy"], data["allowed_approval_policies"])
                self.assertIn(settings["approvals_reviewer"], data["allowed_approvals_reviewers"])

    def test_a_setting_saved_under_another_warning_is_off(self):
        path = self.root / "full_access.json"
        path.write_text(json.dumps({"enabled": True, "warning": "full-access-warning-1", "changed_at": 1.0,
                                    "changed_by": "admin-fixture"}), encoding="utf-8")
        self.assertFalse(full_access.is_on())
        self.assertNotIn("danger-full-access", tomllib.loads(self.written())["allowed_sandbox_modes"])


class LaunchTests(unittest.TestCase):
    """The page starts Codex with the chosen mode's settings and nothing else."""

    setUp_base = attachments.AttachmentTests.setUp
    create = attachments.AttachmentTests.create
    ingress = admin.AdministratorPairingTests.ingress

    def setUp(self):
        self.setUp_base()
        self.server = server
        self.stack.enter_context(isolated_app_folders(server, self.root / "app"))  # the interaction log and others
        self.stack.enter_context(patch.object(full_access, "SETTING_PATH", self.root / "full_access.json"))
        (self.root / "etc").mkdir()
        self.stack.enter_context(patch.object(full_access, "REQUIREMENTS_PATH", self.root / "etc" / "requirements.toml"))
        self.stack.enter_context(patch.object(server, "lookup_ha_users", return_value=[
            {"id": "admin-fixture", "is_active": True, "system_generated": False, "group_ids": ["system-admin"]},
            {"id": "member-fixture", "is_active": True, "system_generated": False, "group_ids": []},
        ]))
        with server.admin_cache_lock:
            server.admin_cache.clear()
        self.task_id = self.create()
        server.tasks[self.task_id]["status"] = "completed"
        server.active_task_runners.clear()

    def choose(self, mode):
        return self.client.post(f"/tasks/{self.task_id}/settings", json={"permission_mode": mode},
                                headers=self.headers)

    def switch(self, body, user="admin-fixture"):
        request = self.ingress(user)
        request["headers"] = {**request["headers"], **self.headers}
        return self.client.post("/full-access", json=body, **request)

    def launch(self):
        """Return the thread and turn settings the page would start Codex with for the chat's mode."""
        options = server.task_options(self.task_id)
        proc = type("Process", (), {"stdin": Mock(), "poll": lambda _: None})()
        with patch.object(server, "AppServerSession") as session:
            server.create_approval_session(self.task_id, proc, "Explain", self.root / "final", None, {}, options)
        kwargs = session.call_args.kwargs
        thread, turn = kwargs["thread_params"], kwargs["turn_params"]
        # The approval settings are the same on the thread and the turn, and no other approval key is sent.
        self.assertEqual((thread["approvalPolicy"], thread["approvalsReviewer"]),
                         (turn["approvalPolicy"], turn["approvalsReviewer"]))
        self.assertEqual({key for key in thread if "pprov" in key}, {"approvalPolicy", "approvalsReviewer"})
        self.assertEqual({key for key in turn if "pprov" in key}, {"approvalPolicy", "approvalsReviewer"})
        return {"sandbox": thread["sandbox"], "approval_policy": thread["approvalPolicy"],
                "approvals_reviewer": thread["approvalsReviewer"]}, turn["sandboxPolicy"]["type"]

    def test_each_mode_starts_codex_with_exactly_its_settings(self):
        self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID}).status_code, 200)
        policy_types = {"workspace-write": "workspaceWrite", "read-only": "readOnly",
                        "danger-full-access": "dangerFullAccess"}
        for mode, settings in EXPECTED_MODES.items():
            with self.subTest(mode=mode):
                self.assertEqual(self.choose(mode).status_code, 200)
                self.assertEqual(server.tasks[self.task_id]["permission_mode"], mode)
                self.assertEqual(self.launch(), (settings, policy_types[settings["sandbox"]]))

    def test_read_only_leaves_every_change_to_the_persons_answer(self):
        """In Read only Codex asks the person (on-request, user) before a change, a device change included."""
        self.assertEqual(self.choose("read-only").status_code, 200)
        settings, sandbox_policy = self.launch()
        self.assertEqual(settings, EXPECTED_MODES["read-only"])
        self.assertEqual(sandbox_policy, "readOnly")

    def test_auto_review_needs_no_administrator_and_no_confirmation(self):
        response = self.choose("auto-review")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.launch()[0], EXPECTED_MODES["auto-review"])

    def test_full_access_is_refused_while_the_switch_is_off(self):
        response = self.choose("full-access")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Full access is off", response.get_json()["error"])

    def test_a_saved_full_access_chat_runs_as_default_once_the_switch_is_off(self):
        self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID}).status_code, 200)
        self.assertEqual(self.choose("full-access").status_code, 200)
        self.assertEqual(self.switch({"enabled": False}).status_code, 200)
        self.assertEqual(server.tasks[self.task_id]["permission_mode"], "default")
        server.tasks[self.task_id]["permission_mode"] = "full-access"  # As a chat saved before the switch went off.
        self.assertEqual(self.launch()[0], EXPECTED_MODES["default"])

    def test_scheduled_work_runs_with_default(self):
        server.tasks[self.task_id]["task_source"] = "schedule"
        server.tasks[self.task_id]["permission_mode"] = "auto-review"
        self.assertEqual(self.launch()[0], EXPECTED_MODES["default"])

    def test_the_switch_needs_an_administrator_and_the_warning(self):
        self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID},
                                     user="member-fixture").status_code, 403)
        self.assertEqual(self.switch({"enabled": True}).status_code, 400)
        self.assertEqual(self.switch({"enabled": True, "confirm": "full-access-warning-1"}).status_code, 400)
        self.assertFalse(full_access.is_on())
        response = self.switch({"enabled": True, "confirm": full_access.WARNING_ID})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["full_access"]["enabled"])
        written = tomllib.loads((self.root / "etc" / "requirements.toml").read_text(encoding="utf-8"))
        self.assertIn("danger-full-access", written["allowed_sandbox_modes"])


class SessionTests(unittest.TestCase):
    """The adapter passes any mode's settings to Codex and does not check what Codex started with."""

    def session(self, thread, turn):
        self.events = []
        self.writer = Mock()
        session = approvals.AppServerSession(
            self.writer, lock=threading.RLock(), thread_params=thread, turn_params=turn,
            on_event=self.events.append, on_approvals=lambda items: None, on_final=lambda text: None,
            is_active=lambda: True, redact=lambda text: text.replace("hunter2", "[redacted]"))
        session.start()
        return session

    def sent(self):
        return [json.loads(call.args[0]) for call in self.writer.write.call_args_list]

    def test_full_access_settings_reach_codex(self):
        thread = {"sandbox": "danger-full-access", "approvalPolicy": "never", "approvalsReviewer": "user"}
        turn = {"input": [], "sandboxPolicy": {"type": "dangerFullAccess"}, "approvalPolicy": "never",
                "approvalsReviewer": "user"}
        session = self.session(thread, turn)
        session.receive({"id": "worker-1", "result": {}})
        session.receive({"id": "worker-2", "result": {"thread": {"id": "thread"}, "approvalPolicy": "never",
                                                      "approvalsReviewer": "user"}})
        methods = [message.get("method") for message in self.sent()]
        self.assertEqual(methods, ["initialize", "initialized", "thread/start", "turn/start"])
        self.assertEqual(self.sent()[2]["params"]["sandbox"], "danger-full-access")
        self.assertEqual(self.sent()[3]["params"]["approvalPolicy"], "never")

    def review(self, method, thread="thread", turn="turn", **review):
        return {"method": method, "params": {
            "threadId": thread, "turnId": turn, "startedAtMs": 1, "reviewId": "review-1", "targetItemId": "item",
            "review": {"status": "inProgress", "riskLevel": None, "userAuthorization": None, "rationale": None, **review},
            "action": {"type": "command", "source": "shell", "command": "ls", "cwd": "/config"}}}

    def auto_review_session(self):
        settings = {"approvalPolicy": "on-request", "approvalsReviewer": "auto_review"}
        session = self.session({"sandbox": "workspace-write", **settings},
                               {"input": [], "sandboxPolicy": {"type": "workspaceWrite"}, **settings})
        session.receive({"id": "worker-1", "result": {}})
        session.receive({"id": "worker-2", "result": {"thread": {"id": "thread"}, **settings}})
        session.turn_id = "turn"
        return session

    def test_the_two_review_events_reach_the_chat(self):
        session = self.auto_review_session()
        session.receive(self.review("item/autoApprovalReview/started"))
        session.receive(self.review("item/autoApprovalReview/completed", status="denied", riskLevel="high",
                                    rationale="The command would send hunter2 off the machine."))
        reviews = [event for event in self.events if event["type"] == "auto_review"]
        self.assertEqual(reviews, [
            {"type": "auto_review", "review_id": "review-1", "status": "inProgress", "risk_level": "",
             "action": "command", "rationale": ""},
            {"type": "auto_review", "review_id": "review-1", "status": "denied", "risk_level": "high",
             "action": "command", "rationale": "The command would send [redacted] off the machine."},
        ])

    def test_reviews_of_another_thread_or_turn_are_ignored(self):
        session = self.auto_review_session()
        session.receive(self.review("item/autoApprovalReview/completed", thread="other", status="approved"))
        session.receive(self.review("item/autoApprovalReview/completed", turn="older", status="approved"))
        self.assertFalse([event for event in self.events if event["type"] == "auto_review"])

    def test_an_unknown_status_or_risk_is_not_read_as_allowed(self):
        for status in ("allowed", "inProgress", None, 1):
            with self.subTest(status=status):
                event = approvals.auto_review_event("item/autoApprovalReview/completed", {
                    "reviewId": "r", "review": {"status": status, "riskLevel": "none"}, "action": {}}, lambda text: text)
                self.assertEqual(event["status"], "unknown")
                self.assertEqual(event["risk_level"], "")
                self.assertEqual(server.auto_review_step(event)["status"], "failed")

    def test_a_long_reason_is_clipped(self):
        event = approvals.auto_review_event("item/autoApprovalReview/completed", {
            "reviewId": "r", "review": {"status": "approved", "rationale": "x" * 5000}, "action": {}}, lambda text: text)
        self.assertEqual(len(event["rationale"]), approvals.AUTO_REVIEW_RATIONALE_LIMIT)


class ChatStepTests(unittest.TestCase):
    """What the chat shows of a review: checking, then allowed or not allowed, in one step per review."""

    def event(self, status, **extra):
        return {"type": "auto_review", "review_id": "review-1", "status": status, "risk_level": "",
                "action": "command", "rationale": "", **extra}

    def test_the_steps_read_plainly(self):
        cases = (
            (self.event("inProgress"), "running", "Auto-review is checking a command."),
            (self.event("approved", risk_level="low"), "done", "Auto-review allowed a command (low risk)."),
            (self.event("denied", risk_level="critical", rationale="It deletes the configuration."), "failed",
             "Auto-review did not allow a command (critical risk). It deletes the configuration."),
            (self.event("timedOut"), "failed", "Auto-review did not allow a command: it took too long."),
            (self.event("aborted", action="mcpToolCall"), "failed",
             "Auto-review did not allow a tool call: the review stopped."),
            (self.event("approved", action="somethingNew"), "done", "Auto-review allowed a request."),
        )
        for event, status, text in cases:
            with self.subTest(event=event):
                step = server.auto_review_step(event)
                self.assertEqual(step, {"id": "auto-review:review-1", "kind": "notice", "status": status, "text": text})

    def test_a_review_without_an_id_shows_nothing(self):
        self.assertIsNone(server.auto_review_step(self.event("approved", review_id="")))


class SettingFileTests(unittest.TestCase):
    """The setting counts as on only when the file holds exactly what set_enabled() writes (full_access.py)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "full_access.json"

    def saved(self, **changes):
        """Write a setting file as set_enabled() would, with the given keys changed or added."""
        data = {"enabled": True, "warning": full_access.WARNING_ID, "changed_at": 1.0, "changed_by": "admin-fixture"}
        data.update(changes)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_the_file_set_enabled_writes_is_on(self):
        self.saved()
        self.assertTrue(full_access.is_on(self.path))

    def test_a_linked_file_is_off(self):
        self.saved()
        link = self.root / "link.json"
        link.symlink_to(self.path)
        self.assertFalse(full_access.is_on(link))

    def test_a_file_with_an_extra_key_is_off(self):
        self.saved(note="turned on by hand")
        self.assertFalse(full_access.is_on(self.path))

    def test_enabled_must_be_true_itself(self):
        for value in (1, "yes", "true", [True]):
            with self.subTest(enabled=value):
                self.saved(enabled=value)
                self.assertFalse(full_access.is_on(self.path))

    def test_changed_at_must_be_a_finite_number(self):
        for value in (True, False, "1.0", None):
            with self.subTest(changed_at=value):
                self.saved(changed_at=value)
                self.assertFalse(full_access.is_on(self.path))
        for text in ("NaN", "Infinity"):
            with self.subTest(changed_at=text):
                self.path.write_text(json.dumps({"enabled": True, "warning": full_access.WARNING_ID,
                                                 "changed_by": "admin-fixture"})[:-1] + f', "changed_at": {text}}}',
                                     encoding="utf-8")
                self.assertFalse(full_access.is_on(self.path))

    def test_set_enabled_writes_a_file_for_the_worker_only(self):
        full_access.set_enabled(True, "admin-fixture", self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(full_access.is_on(self.path))


class SwitchTests(unittest.TestCase):
    """What turning the switch on or off does beside the setting file: waiting messages, the requirements,
    remote control and the start of the app."""

    setUp_base = attachments.AttachmentTests.setUp
    setUp_launch = LaunchTests.setUp
    create = attachments.AttachmentTests.create
    switch = LaunchTests.switch
    choose = LaunchTests.choose
    ingress = admin.AdministratorPairingTests.ingress

    def setUp(self):
        self.setUp_launch()
        self.queue = []
        self.stack.enter_context(patch.object(server, "message_queue", self.queue))
        self.stack.enter_context(patch.object(server, "MESSAGE_QUEUE_FILE", self.root / "message_queue.json"))
        self.requirements = self.root / "etc" / "requirements.toml"

    def allows_full_access(self):
        return "danger-full-access" in tomllib.loads(self.requirements.read_text(encoding="utf-8"))["allowed_sandbox_modes"]

    def test_a_waiting_message_returns_to_default_when_the_switch_goes_off(self):
        self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID}).status_code, 200)
        self.queue.append({"task_id": self.task_id, "prompt": "Clean up the logs", "permission_mode": "full-access"})
        self.assertEqual(self.switch({"enabled": False}).status_code, 200)
        self.assertEqual(self.queue[0]["permission_mode"], "default")

    def test_a_failed_write_when_turning_on_leaves_it_off(self):
        real_text = full_access.requirements_text

        def refuse_when_on(enabled, shipped):
            if enabled:
                raise ValueError("The shipped requirements do not hold the expected sandbox modes line.")
            return real_text(enabled, shipped)

        with patch.object(full_access, "requirements_text", side_effect=refuse_when_on):
            response = self.switch({"enabled": True, "confirm": full_access.WARNING_ID})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "Could not change Full access. It is off.")
        self.assertFalse(full_access.is_on())
        self.assertFalse(self.allows_full_access())

    def test_a_failed_write_when_turning_off_does_not_say_it_is_off(self):
        self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID}).status_code, 200)
        self.assertTrue(self.allows_full_access())
        with patch.object(full_access, "write_requirements", side_effect=OSError("disk full")):
            response = self.switch({"enabled": False})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], server.FULL_ACCESS_STILL_ALLOWED)
        self.assertNotIn("It is off", response.get_json()["error"])
        # Chats on the page are off at once; the requirements file still allows it, as the answer says.
        self.assertFalse(full_access.is_on())
        self.assertTrue(self.allows_full_access())

    def test_a_retried_write_after_a_failure_puts_the_shipped_file_back(self):
        self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID}).status_code, 200)
        real_write = full_access.write_requirements
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("busy")
            return real_write(*args, **kwargs)

        with patch.object(full_access, "write_requirements", side_effect=fail_once):
            response = self.switch({"enabled": False})
        self.assertEqual(response.get_json()["error"], "Could not change Full access. It is off.")
        self.assertEqual(self.requirements.read_text(encoding="utf-8"), SHIPPED.read_text(encoding="utf-8"))

    def test_remote_control_restarts_on_each_change(self):
        supervisor = server.remote_control_supervisor
        for body in ({"enabled": True, "confirm": full_access.WARNING_ID}, {"enabled": False}):
            with self.subTest(body=body), patch.object(supervisor, "enabled", return_value=True), \
                    patch.object(supervisor, "stop") as stop, patch.object(supervisor, "start") as start:
                self.assertEqual(self.switch(body).status_code, 200)
                stop.assert_called_once_with()
                start.assert_called_once_with()

    def test_remote_control_that_is_off_stays_off(self):
        supervisor = server.remote_control_supervisor
        with patch.object(supervisor, "enabled", return_value=False), patch.object(supervisor, "start") as start:
            self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID}).status_code, 200)
        start.assert_not_called()

    def test_the_start_of_the_app_writes_the_requirements_for_the_saved_setting(self):
        full_access.set_enabled(True, "admin-fixture")
        self.requirements.write_text(SHIPPED.read_text(encoding="utf-8"), encoding="utf-8")  # As a new image ships it.
        server.write_full_access_requirements_at_start()
        self.assertTrue(self.allows_full_access())
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        main = source[source.index("\ndef main() -> None:"):]
        main = main[:main.index("\nif __name__")]
        self.assertIn("write_full_access_requirements_at_start()", main)
        self.assertLess(main.index("write_full_access_requirements_at_start()"), main.index("load_task_index()"))

    def test_a_failed_write_at_start_turns_the_setting_off(self):
        full_access.set_enabled(True, "admin-fixture")
        real_text = full_access.requirements_text

        def refuse_when_on(enabled, shipped):
            if enabled:
                raise ValueError("The shipped requirements do not hold the expected sandbox modes line.")
            return real_text(enabled, shipped)

        with patch.object(full_access, "requirements_text", side_effect=refuse_when_on):
            server.write_full_access_requirements_at_start()
        self.assertFalse(full_access.is_on())
        self.assertFalse(self.allows_full_access())


class StandInCodex:
    """What Codex 0.166.0 does before it runs the call tool, for the device-call contract tests.

    A test double, not Codex. It follows the pinned source, codex-rs/core/src/mcp_tool_call.rs at rust-v0.166.0
    (plan CX-SIMPLE, step 0): with approval policy "never" and the danger-full-access sandbox, a tool runs with
    no question (mcp_permission_prompt_is_auto_approved). With the reviewer auto_review, the reviewer answers and
    the client sees only its two events. Otherwise Codex sends mcpServer/elicitation/request and runs the tool once
    only when the answer is "accept" (request_mcp_tool_user_approval, parse_mcp_tool_approval_elicitation_response).
    Running the tool is a real request through the worker's call service, so the fake Home Assistant records it.
    """

    def __init__(self, session, transport, calls, key, thread):
        self.session, self.transport, self.calls, self.key, self.thread = session, transport, calls, key, thread
        self.asked = None

    def run(self, arguments):
        return self.calls.dispatch({"thread_id": ha_calls.THREAD, "arguments": arguments})

    def call(self, arguments, review_status=None):
        """Ask to run the tool. Return the tool's result when it ran now, else None."""
        if self.thread["approvalPolicy"] == "never" and self.thread["sandbox"] == "danger-full-access":
            return self.run(arguments)
        if self.thread["approvalsReviewer"] == "auto_review":
            review = {"threadId": "thread", "turnId": "turn", "reviewId": "review-1", "targetItemId": "call-1",
                      "action": {"type": "mcpToolCall", "server": "home_assistant_call", "toolName": "call"}}
            self.session.receive({"method": "item/autoApprovalReview/started",
                                  "params": {**review, "review": {"status": "inProgress"}}})
            self.session.receive({"method": "item/autoApprovalReview/completed",
                                  "params": {**review, "review": {"status": review_status, "riskLevel": "high"}}})
            return self.run(arguments) if review_status == "approved" else None
        self.asked = arguments
        self.session.receive({"id": "call-1", "method": "mcpServer/elicitation/request", "params": {
            "threadId": "thread", "turnId": "turn", "serverName": "home_assistant_call", "mode": "form",
            "message": 'Allow the home_assistant_call MCP server to run tool "call"?', "requestedSchema": {},
            "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": arguments}}})
        return None

    def answered(self):
        """Run the asked call when the client's answer to it is accept; return the tool's result, or None."""
        replies = [message for message in self.transport.messages() if message.get("id") == "call-1"]
        if replies and replies[-1].get("result", {}).get("action") == "accept":
            return self.run(self.asked)
        return None


class DeviceCallContractTests(unittest.TestCase):
    """Plan sections 3 and 6: in each mode, what reaches a fake Home Assistant from one device call, and when.

    The mode's settings are the ones the page starts Codex with, the tool's approval mode is the launch's own,
    the exchange is the worker's AppServerSession, and the request is the worker's call service; only Codex is a
    stand-in (StandInCodex).
    """

    setUp_base = attachments.AttachmentTests.setUp
    setUp_launch = LaunchTests.setUp
    create = attachments.AttachmentTests.create
    switch = LaunchTests.switch
    choose = LaunchTests.choose
    ingress = admin.AdministratorPairingTests.ingress
    REQUEST: ClassVar[dict] = {"service": "light.turn_off", "target": {"entity_id": "light.kitchen_ceiling"}}

    def setUp(self):
        self.setUp_launch()
        self.fake = ha_calls.FakeHomeAssistant(answer=[])
        self.stack.enter_context(patch.object(ha_call.requests, "post", self.fake.post))
        self.stack.enter_context(patch.object(ha_call, "fetch_states", lambda path, token: {
            "entity_id": path[8:], "state": "off", "attributes": {}}))
        self.calls = ha_call.HomeAssistantCall(ha_calls.call_worker())
        tool_threads.CHATS.bind("t1", ha_calls.THREAD)
        self.addCleanup(tool_threads.CHATS.release, "t1")
        self.key = self.calls.begin("t1")
        prefix = "mcp_servers.home_assistant_call.tools.call.approval_mode="
        modes = [value.removeprefix(prefix) for value in server.call_mcp_args() if value.startswith(prefix)]
        self.assertEqual(modes, ['"prompt"'])

    def codex(self, mode):
        """Start the chat's exchange in this mode as the page does, with a stand-in Codex in front of it."""
        self.assertEqual(self.choose(mode).status_code, 200)
        options = server.task_options(self.task_id)
        proc = type("Process", (), {"stdin": Mock(), "poll": lambda _: None})()
        with patch.object(server, "AppServerSession") as made:
            server.create_approval_session(self.task_id, proc, "Turn off the kitchen light", self.root / "final",
                                           None, {}, options)
        kwargs = made.call_args.kwargs
        self.transport = approval_tests.Transport()
        self.events, self.records = [], []
        session = approvals.AppServerSession(
            self.transport, lock=threading.RLock(), thread_params=kwargs["thread_params"],
            turn_params=kwargs["turn_params"], on_event=self.events.append, on_approvals=self.records.append,
            on_final=lambda text: None, is_active=lambda: True, redact=lambda text: text)
        session.start()
        session.receive({"id": "worker-1", "result": {}})
        session.receive({"id": "worker-2", "result": {"thread": {"id": "thread"}}})
        session.receive({"id": "worker-3", "result": {"turn": {"id": "turn"}}})
        self.session = session
        return StandInCodex(session, self.transport, self.calls, self.key, kwargs["thread_params"])

    def sent(self):
        """Return (path, body) of each request the fake Home Assistant received."""
        return [(item["url"].removeprefix("http://supervisor/core"), item["json"]) for item in self.fake.requests]

    def card(self):
        return self.records[-1][-1]

    def test_read_only_sends_nothing_while_the_question_waits(self):
        codex = self.codex("read-only")
        self.assertIsNone(codex.call(self.REQUEST))
        self.assertEqual(self.card()["status"], "pending")
        self.assertEqual(self.sent(), [])
        self.session.close()  # The exchange ends with no answer, so the request expires.
        self.assertIsNone(codex.answered())
        self.assertEqual(self.sent(), [])

    def test_read_only_sends_nothing_after_a_denial(self):
        codex = self.codex("read-only")
        codex.call(self.REQUEST)
        self.session.decide(self.card()["approval_id"], "deny")
        self.assertIsNone(codex.answered())
        self.assertEqual(self.sent(), [])

    def test_read_only_sends_the_exact_request_after_a_yes(self):
        codex = self.codex("read-only")
        codex.call(self.REQUEST)
        self.assertEqual(self.sent(), [])
        self.session.decide(self.card()["approval_id"], "allow")
        self.assertEqual(codex.answered()["status"], "ok")
        self.assertEqual(self.sent(), [("/api/services/light/turn_off", {"entity_id": "light.kitchen_ceiling"})])

    def test_full_access_sends_the_exact_request_with_no_question(self):
        self.assertEqual(self.switch({"enabled": True, "confirm": full_access.WARNING_ID}).status_code, 200)
        codex = self.codex("full-access")
        self.assertEqual(codex.call(self.REQUEST)["status"], "ok")
        self.assertEqual(self.records, [])
        self.assertEqual(self.sent(), [("/api/services/light/turn_off", {"entity_id": "light.kitchen_ceiling"})])

    def test_auto_review_sends_nothing_after_a_denied_review(self):
        codex = self.codex("auto-review")
        self.assertIsNone(codex.call(self.REQUEST, review_status="denied"))
        self.assertEqual(self.records, [])
        self.assertEqual(self.sent(), [])
        reviews = [event for event in self.events if event["type"] == "auto_review"]
        self.assertEqual([event["status"] for event in reviews], ["inProgress", "denied"])
        self.assertEqual(server.auto_review_step(reviews[-1])["status"], "failed")


class ReviewEventTypeTests(unittest.TestCase):
    """A review event whose status or risk is not text reads as unknown and does not stop the turn."""

    def test_a_list_or_an_object_reads_as_unknown(self):
        for value in (["approved"], {"status": "approved"}):
            with self.subTest(value=value):
                event = approvals.auto_review_event("item/autoApprovalReview/completed", {
                    "reviewId": "r", "review": {"status": value, "riskLevel": value}, "action": {}}, lambda text: text)
                self.assertEqual((event["status"], event["risk_level"]), ("unknown", ""))
                self.assertEqual(server.auto_review_step(event)["status"], "failed")


class PageTests(unittest.TestCase):
    """The Permissions button offers the four modes, in the app's order and with its names."""

    def test_the_dialog_lists_the_four_modes(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        start = html.index('id="permissions-dialog"')
        dialog = html[start:html.index("</dialog>", start)]
        modes = [part.split('"', 1)[0] for part in dialog.split('data-mode="')[1:]]
        self.assertEqual(modes, ["default", "auto-review", "read-only", "full-access"])
        self.assertNotIn("Coming", dialog)
        self.assertIn("It does not ask you.", dialog)
        self.assertNotIn("Administrators only", dialog)

    def test_the_button_names_each_mode(self):
        script = (ROOT / "web" / "assets" / "studio.js").read_text(encoding="utf-8")
        for name in ('"auto-review": "Auto-review"', '"full-access": "Full access"'):
            self.assertIn(name, script)


if __name__ == "__main__":
    unittest.main()
