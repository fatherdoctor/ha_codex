"""The four permission modes of the ChatGPT app: Default, Auto-review, Read only and Full access.

Each mode is a Codex setting and nothing more. Auto-review is Default with Codex's own reviewer in place of the
person: the reviewer allows or denies each request and never asks the person (Codex rust-v0.166.0,
protocol/src/approvals.rs). These tests hold the exact settings each mode starts Codex with, the guards that keep
the reviewer to a chat an administrator set, and what the chat shows of each review.
"""
import json
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import approvals
import full_access
import policy
import remote_control as rc
import test_attachments as attachments
import test_cx_pair_f as admin
from test_server import server

SHIPPED = ROOT / "gate" / "requirements.toml"
SHIPPED_POLICY = json.loads((ROOT / "policy" / "policy.json").read_text(encoding="utf-8"))


class PolicyFileTests(unittest.TestCase):
    """The protected policy lists who may answer a request: the person always, Codex's reviewer if listed."""

    def write(self, data):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "policy.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_the_shipped_policy_lists_the_person_and_the_reviewer(self):
        self.assertEqual(SHIPPED_POLICY["allowed_approvals_reviewers"], ["user", "auto_review"])
        self.assertEqual(SHIPPED_POLICY["approvals_reviewer"], "user")
        self.assertEqual(policy.load_policy(self.write(SHIPPED_POLICY))["allowed_approvals_reviewers"],
                         ["user", "auto_review"])

    def test_a_policy_without_the_list_allows_only_the_person(self):
        data = {key: value for key, value in SHIPPED_POLICY.items() if key != "allowed_approvals_reviewers"}
        self.assertEqual(policy.load_policy(self.write(data))["allowed_approvals_reviewers"], ["user"])
        self.assertEqual(policy.STRICT_POLICY["allowed_approvals_reviewers"], ["user"])

    def test_a_list_that_drops_the_person_or_adds_an_unknown_reviewer_is_refused(self):
        for reviewers in ([], ["auto_review"], ["user", "guardian"], ["user", "user"], "user", None,
                          ["user", "auto_review", "auto_review"], [["user"]]):
            with self.subTest(reviewers=reviewers):
                with self.assertRaisesRegex(policy.PolicyError, "allowed_approvals_reviewers"):
                    policy.load_policy(self.write({**SHIPPED_POLICY, "allowed_approvals_reviewers": reviewers}))

    def test_the_default_reviewer_stays_the_person(self):
        with self.assertRaisesRegex(policy.PolicyError, 'approvals_reviewer set to "user"'):
            policy.load_policy(self.write({**SHIPPED_POLICY, "approvals_reviewer": "auto_review"}))


class EffectivePolicyTests(unittest.TestCase):
    """Only a turn the worker marked as Auto-review names Codex's reviewer, and never beside Full access."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.setting = Path(folder.name) / "full_access.json"
        patcher = patch.object(full_access, "SETTING_PATH", self.setting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_auto_review_turn_names_the_reviewer(self):
        options = {"codex_sandbox": "workspace-write", policy.AUTO_REVIEW_OPTION: True}
        self.assertEqual(policy.effective_policy(options, policy.load_policy()), {
            "sandbox": "workspace-write", "approval_policy": "on-request", "approvals_reviewer": "auto_review"})
        self.assertEqual(policy.policy_problem(options), "")

    def test_only_true_marks_a_turn(self):
        for value in ("true", 1, "auto_review", None):
            with self.subTest(value=value):
                options = {"codex_sandbox": "workspace-write", policy.AUTO_REVIEW_OPTION: value}
                self.assertFalse(policy.is_auto_review_turn(options))
                self.assertEqual(policy.effective_policy(options, policy.load_policy())["approvals_reviewer"], "user")

    def test_a_policy_without_the_reviewer_refuses_an_auto_review_turn(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "policy.json"
        path.write_text(json.dumps({**SHIPPED_POLICY, "allowed_approvals_reviewers": ["user"]}), encoding="utf-8")
        options = {"codex_sandbox": "workspace-write", policy.AUTO_REVIEW_OPTION: True}
        self.assertIn('"auto_review" is not allowed', policy.policy_problem(options, path))
        self.assertEqual(policy.policy_problem({"codex_sandbox": "workspace-write"}, path), "")

    def test_full_access_wins_over_the_reviewer_mark(self):
        full_access.set_enabled(True, "admin-fixture")
        options = {"codex_sandbox": "danger-full-access", full_access.CHAT_OPTION: True, policy.AUTO_REVIEW_OPTION: True}
        self.assertFalse(policy.is_auto_review_turn(options))
        self.assertEqual(policy.effective_policy(options, policy.load_policy()), {
            "sandbox": "danger-full-access", "approval_policy": "never", "approvals_reviewer": "user"})


class RequirementsTests(unittest.TestCase):
    """The managed requirements let a client pick the reviewer, with the person first as Codex's default."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patcher = patch.object(full_access, "SETTING_PATH", self.root / "full_access.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text):
        path = self.root / "requirements.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_shipped_file_lists_the_person_first(self):
        data = tomllib.loads(SHIPPED.read_text(encoding="utf-8"))
        # Codex takes the first entry as the default (config/src/config_requirements.rs at rust-v0.166.0).
        self.assertEqual(data["allowed_approvals_reviewers"], ["user", "auto_review"])
        self.assertEqual(rc.requirements_problem(SHIPPED), "")

    def test_full_access_on_keeps_the_reviewer_line(self):
        full_access.set_enabled(True, "admin-fixture")
        text = full_access.requirements_text(True, SHIPPED.read_text(encoding="utf-8"))
        self.assertEqual(tomllib.loads(text)["allowed_approvals_reviewers"], ["user", "auto_review"])
        self.assertEqual(rc.requirements_problem(self.write(text)), "")

    def test_a_file_without_the_person_first_does_not_pass(self):
        shipped = SHIPPED.read_text(encoding="utf-8")
        line = 'allowed_approvals_reviewers = ["user", "auto_review"]'
        self.assertEqual(shipped.count(line), 1)
        for replacement in ("", 'allowed_approvals_reviewers = []', 'allowed_approvals_reviewers = ["auto_review", "user"]',
                            'allowed_approvals_reviewers = ["user", "guardian"]', 'allowed_approvals_reviewers = "user"'):
            with self.subTest(replacement=replacement):
                problem = rc.requirements_problem(self.write(shipped.replace(line, replacement)))
                self.assertIn("approvals reviewer", problem)

    def test_both_modules_know_the_same_reviewers(self):
        self.assertEqual(rc.KNOWN_REVIEWERS, policy.KNOWN_REVIEWERS)


class ChatModeTests(unittest.TestCase):
    """Each of the four modes starts Codex with exactly the settings of the plan's table (section 3)."""

    setUp_base = attachments.AttachmentTests.setUp
    create = attachments.AttachmentTests.create
    ingress = admin.AdministratorPairingTests.ingress

    def setUp(self):
        self.setUp_base()
        self.server = server
        self.stack.enter_context(patch.object(full_access, "SETTING_PATH", self.root / "full_access.json"))
        self.stack.enter_context(patch.object(server, "lookup_ha_users", return_value=[
            {"id": "admin-fixture", "is_active": True, "system_generated": False, "group_ids": ["system-admin"]},
            {"id": "member-fixture", "is_active": True, "system_generated": False, "group_ids": []},
        ]))
        with server.admin_cache_lock:
            server.admin_cache.clear()
        self.task_id = self.create()
        server.tasks[self.task_id]["status"] = "completed"
        server.active_task_runners.clear()

    def choose(self, user, mode):
        request = self.ingress(user)
        request["headers"] = {**request["headers"], **self.headers}
        return self.client.post(f"/tasks/{self.task_id}/settings", json={"permission_mode": mode}, **request)

    def launch(self):
        """Return the thread and turn settings the page would start Codex with for the chat's mode."""
        options = server.task_options(self.task_id)
        self.assertEqual(server.protected_policy.policy_problem(options), "")
        proc = type("Process", (), {"stdin": Mock(), "poll": lambda _: None})()
        with patch.object(server, "AppServerSession") as session:
            server.create_approval_session(self.task_id, proc, "Explain", self.root / "final", None, {}, options)
        kwargs = session.call_args.kwargs
        thread, turn = kwargs["thread_params"], kwargs["turn_params"]
        self.assertEqual(kwargs["approvals_reviewer"], thread["approvalsReviewer"])
        self.assertEqual((thread["approvalPolicy"], thread["approvalsReviewer"]),
                         (turn["approvalPolicy"], turn["approvalsReviewer"]))
        return (thread["sandbox"], turn["sandboxPolicy"]["type"], thread["approvalPolicy"], thread["approvalsReviewer"],
                kwargs["allow_full_access"])

    def test_each_mode_starts_codex_with_its_own_settings(self):
        full_access.set_enabled(True, "admin-fixture")
        expected = {
            "default": ("workspace-write", "workspaceWrite", "on-request", "user", False),
            "auto-review": ("workspace-write", "workspaceWrite", "on-request", "auto_review", False),
            "read-only": ("read-only", "readOnly", "on-request", "user", False),
            "full-access": ("danger-full-access", "dangerFullAccess", "never", "user", True),
        }
        for mode, settings in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(self.choose("admin-fixture", mode).status_code, 200)
                self.assertEqual(server.tasks[self.task_id]["permission_mode"], mode)
                self.assertEqual(self.launch(), settings)

    def test_auto_review_keeps_the_app_sandbox_option(self):
        self.assertEqual(self.choose("admin-fixture", "auto-review").status_code, 200)
        with patch.object(server, "read_options", return_value={"codex_sandbox": "read-only"}):
            self.assertEqual(self.launch(), ("read-only", "readOnly", "on-request", "auto_review", False))

    def test_only_an_administrator_can_choose_auto_review(self):
        response = self.choose("member-fixture", "auto-review")
        self.assertEqual(response.status_code, 400)
        self.assertIn("administrator", response.json["error"])
        self.assertNotEqual(server.tasks[self.task_id].get("permission_mode"), "auto-review")
        self.assertEqual(self.launch()[3], "user")

    def test_a_member_cannot_send_with_auto_review(self):
        request = self.ingress("member-fixture")
        request["headers"] = {**request["headers"], **self.headers}
        response = self.client.post("/tasks", json={"prompt": "Explain", "permission_mode": "auto-review"}, **request)
        self.assertEqual(response.status_code, 400)

    def test_a_policy_without_the_reviewer_refuses_the_choice(self):
        with patch.object(server, "auto_review_allowed", return_value=False):
            response = self.choose("admin-fixture", "auto-review")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not allowed by the protected policy", response.json["error"])

    def test_the_app_options_never_mark_a_turn_for_the_reviewer(self):
        with patch.object(server, "read_options", return_value={"codex_sandbox": "workspace-write",
                                                                policy.AUTO_REVIEW_OPTION: True}):
            options = server.task_options(self.task_id)
        self.assertNotIn(policy.AUTO_REVIEW_OPTION, options)
        self.assertEqual(self.launch()[3], "user")

    def test_full_access_off_returns_the_mode_list_to_three(self):
        self.assertEqual(self.choose("admin-fixture", "full-access").status_code, 400)
        for mode in ("default", "auto-review", "read-only"):
            with self.subTest(mode=mode):
                self.assertEqual(self.choose("admin-fixture", mode).status_code, 200)


class SessionTests(unittest.TestCase):
    """Codex must start the thread with the reviewer the worker asked for, and the chat shows each review."""

    def session(self, reviewer):
        writer = Mock()
        self.events = []
        thread = {"sandbox": "workspace-write", "approvalPolicy": "on-request", "approvalsReviewer": reviewer}
        turn = {"input": [], "sandboxPolicy": {"type": "workspaceWrite"}, "approvalPolicy": "on-request",
                "approvalsReviewer": reviewer}
        session = approvals.AppServerSession(
            writer, lock=threading.RLock(), thread_params=thread, turn_params=turn,
            on_event=self.events.append, on_approvals=lambda items: None, on_final=lambda text: None,
            is_active=lambda: True, redact=lambda text: text.replace("hunter2", "[redacted]"),
            approvals_reviewer=reviewer)
        session.start()
        return session, writer

    def start_thread(self, session, reviewer):
        session.receive({"id": "worker-1", "result": {}})
        session.receive({"id": "worker-2", "result": {"thread": {"id": "thread"}, "approvalPolicy": "on-request",
                                                      "approvalsReviewer": reviewer}})

    def test_codex_must_keep_the_chosen_reviewer(self):
        for asked, kept, error in (("user", "user", ""), ("auto_review", "auto_review", ""),
                                   ("user", "auto_review", "chosen approvals reviewer"),
                                   ("auto_review", "user", "chosen approvals reviewer"),
                                   ("user", None, "chosen approvals reviewer")):
            with self.subTest(asked=asked, kept=kept):
                session, writer = self.session(asked)
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        self.start_thread(session, kept)
                else:
                    self.start_thread(session, kept)
                    sent = json.loads(writer.write.call_args_list[-1].args[0])
                    self.assertEqual(sent["method"], "turn/start")
                    self.assertEqual(sent["params"]["approvalsReviewer"], asked)

    def review(self, method, thread="thread", turn="turn", **review):
        return {"method": method, "params": {
            "threadId": thread, "turnId": turn, "startedAtMs": 1, "reviewId": "review-1", "targetItemId": "item",
            "review": {"status": "inProgress", "riskLevel": None, "userAuthorization": None, "rationale": None, **review},
            "action": {"type": "command", "source": "shell", "command": "ls", "cwd": "/config"}}}

    def test_the_two_review_events_reach_the_chat(self):
        session, _ = self.session("auto_review")
        self.start_thread(session, "auto_review")
        session.turn_id = "turn"
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
        session, _ = self.session("auto_review")
        self.start_thread(session, "auto_review")
        session.turn_id = "turn"
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


class PageTests(unittest.TestCase):
    """The Permissions button offers the four modes, in the app's order and with its names."""

    def test_the_dialog_lists_the_four_modes(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        dialog = html[html.index('id="permissions-dialog"'):html.index("</dialog>", html.index('id="permissions-dialog"'))]
        modes = [part.split('"', 1)[0] for part in dialog.split('data-mode="')[1:]]
        self.assertEqual(modes, ["default", "auto-review", "read-only", "full-access"])
        self.assertNotIn("Coming", dialog)
        self.assertIn("It does not ask you.", dialog)

    def test_the_button_names_each_mode(self):
        script = (ROOT / "web" / "assets" / "studio.js").read_text(encoding="utf-8")
        self.assertIn('"auto-review": "Auto-review"', script)


if __name__ == "__main__":
    unittest.main()
