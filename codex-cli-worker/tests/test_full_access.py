"""Full access: off by default, on only after an administrator confirms the warning, and only for chats set to it."""
import json
import os
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
import gate_rules
import policy
import remote_control as rc
import test_attachments as attachments
import test_cx_pair_f as admin
from test_server import server

SHIPPED = ROOT / "gate" / "requirements.toml"


class SettingFileTests(unittest.TestCase):
    """What counts as on: only the file set_enabled() writes."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "full_access.json"

    def test_missing_file_is_off(self):
        self.assertFalse(full_access.is_on(self.path))

    def test_set_enabled_turns_it_on_and_off(self):
        setting = full_access.set_enabled(True, "admin-fixture", self.path)
        self.assertTrue(setting["enabled"])
        self.assertEqual(setting["changed_by"], "admin-fixture")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertFalse(full_access.set_enabled(False, "admin-fixture", self.path)["enabled"])
        self.assertFalse(self.path.exists())

    def test_forged_or_unreadable_setting_is_off(self):
        good = {"enabled": True, "warning": full_access.WARNING_ID, "changed_at": 1.0, "changed_by": "admin-fixture"}
        forged = [
            "not json",
            json.dumps([good]),
            json.dumps({**good, "enabled": "true"}),
            json.dumps({**good, "enabled": 1}),
            json.dumps({**good, "warning": "full-access-warning-0"}),
            json.dumps({**good, "extra": True}),
            json.dumps({key: value for key, value in good.items() if key != "changed_by"}),
            json.dumps({**good, "changed_at": "yesterday"}),
        ]
        for text in forged:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                self.assertFalse(full_access.is_on(self.path))
        self.path.write_text(json.dumps(good), encoding="utf-8")
        self.assertTrue(full_access.is_on(self.path))

    def test_linked_setting_is_off(self):
        real = self.path.with_name("elsewhere.json")
        full_access.set_enabled(True, "admin-fixture", real)
        self.path.symlink_to(real)
        self.assertFalse(full_access.is_on(self.path))


class RequirementsTests(unittest.TestCase):
    """The managed requirements are the shipped file while off, with full access added only while on."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.setting = self.root / "full_access.json"
        self.target = self.root / "etc" / "requirements.toml"
        self.target.parent.mkdir()

    def test_off_writes_the_shipped_file_byte_for_byte(self):
        self.assertTrue(full_access.write_requirements(self.target, SHIPPED, self.setting))
        self.assertEqual(self.target.read_bytes(), SHIPPED.read_bytes())
        with patch.object(full_access, "SETTING_PATH", self.setting):
            self.assertEqual(rc.requirements_problem(self.target), "")

    def test_on_allows_full_access_and_keeps_every_other_rule(self):
        full_access.set_enabled(True, "admin-fixture", self.setting)
        full_access.write_requirements(self.target, SHIPPED, self.setting)
        written = self.target.read_text(encoding="utf-8")
        self.assertIn(full_access.OPEN_MODES_LINE, written)
        self.assertIn(full_access.OPEN_APPROVALS_LINE, written)
        restored = (written.replace(full_access.OPEN_MODES_LINE, full_access.STRICT_MODES_LINE)
                    .replace(full_access.OPEN_APPROVALS_LINE, full_access.STRICT_APPROVALS_LINE))
        self.assertEqual(restored, SHIPPED.read_text(encoding="utf-8"))
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o644)

    def test_on_keeps_every_command_refusal_and_the_managed_hooks(self):
        full_access.set_enabled(True, "admin-fixture", self.setting)
        full_access.write_requirements(self.target, SHIPPED, self.setting)
        written = tomllib.loads(self.target.read_text(encoding="utf-8"))
        shipped = tomllib.loads(SHIPPED.read_text(encoding="utf-8"))
        self.assertEqual(written["rules"], shipped["rules"])
        self.assertTrue(all(rule["decision"] == "forbidden" for rule in written["rules"]["prefix_rules"]))
        self.assertIs(written["allow_managed_hooks_only"], True)

    def test_remote_control_accepts_never_only_beside_full_access_while_on(self):
        shipped = SHIPPED.read_text(encoding="utf-8")
        never_alone = shipped.replace(full_access.STRICT_APPROVALS_LINE, full_access.OPEN_APPROVALS_LINE)
        both = full_access.requirements_text(True, shipped)
        with patch.object(full_access, "SETTING_PATH", self.setting):
            for enabled, text, accepted in ((True, both, True), (True, never_alone, False),
                                            (False, both, False), (False, never_alone, False)):
                with self.subTest(enabled=enabled, text="both" if text == both else "never alone"):
                    if enabled:
                        full_access.set_enabled(True, "admin-fixture", self.setting)
                    else:
                        full_access.set_enabled(False, "admin-fixture", self.setting)
                    self.target.write_text(text, encoding="utf-8")
                    self.assertEqual(rc.requirements_problem(self.target) == "", accepted)

    def test_remote_control_accepts_full_access_requirements_only_while_on(self):
        full_access.set_enabled(True, "admin-fixture", self.setting)
        full_access.write_requirements(self.target, SHIPPED, self.setting)
        with patch.object(full_access, "SETTING_PATH", self.setting):
            self.assertEqual(rc.requirements_problem(self.target), "")
            full_access.set_enabled(False, "admin-fixture", self.setting)
            self.assertIn("full-access", rc.requirements_problem(self.target))

    def test_a_changed_shipped_file_is_never_edited_by_guesswork(self):
        with self.assertRaises(ValueError):
            full_access.requirements_text(True, 'allowed_sandbox_modes = ["read-only"]\n')
        with self.assertRaises(ValueError):
            full_access.requirements_text(True, full_access.STRICT_MODES_LINE + "\n")

    def test_nothing_is_written_outside_the_image(self):
        self.assertFalse(full_access.write_requirements(self.root / "missing" / "r.toml", SHIPPED, self.setting))


class PolicyTests(unittest.TestCase):
    """Full access passes the protected policy only for a chat turn that asked for it, while the setting is on."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.setting = Path(folder.name) / "full_access.json"
        patcher = patch.object(full_access, "SETTING_PATH", self.setting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_chat_turn_passes_only_while_on(self):
        options = {"codex_sandbox": "danger-full-access", full_access.CHAT_OPTION: True}
        self.assertIn("not allowed", policy.policy_problem(options))
        full_access.set_enabled(True, "admin-fixture")
        self.assertEqual(policy.policy_problem(options), "")

    def test_app_option_never_turns_on_full_access(self):
        full_access.set_enabled(True, "admin-fixture")
        self.assertIn("not allowed", policy.policy_problem({"codex_sandbox": "danger-full-access"}))
        self.assertIn("not allowed", policy.policy_problem(
            {"codex_sandbox": "danger-full-access", full_access.CHAT_OPTION: "yes"}))

    def test_integration_requests_refuse_full_access_unless_the_worker_allows_it(self):
        for params in ({"sandbox": "danger-full-access"}, {"sandboxPolicy": {"type": "dangerFullAccess"}}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    approvals.require_supported_policy(params)
                approvals.require_supported_policy(params, allow_full_access=True)
        with self.assertRaises(ValueError):
            approvals.require_supported_policy({"approvalPolicy": "never"}, allow_full_access=True)


class NeverApprovalTests(unittest.TestCase):
    """Full access runs with the never approval policy, as the ChatGPT app's Full access; nothing else does."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.setting = Path(folder.name) / "full_access.json"
        patcher = patch.object(full_access, "SETTING_PATH", self.setting)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.full = {"codex_sandbox": "danger-full-access", full_access.CHAT_OPTION: True}

    def test_a_full_access_turn_runs_with_never_while_on(self):
        full_access.set_enabled(True, "admin-fixture")
        effective = policy.effective_policy(self.full, policy.load_policy())
        self.assertEqual(effective["approval_policy"], "never")
        self.assertEqual(policy.policy_problem(self.full), "")

    def test_never_is_refused_with_the_setting_off(self):
        effective = policy.effective_policy(self.full, policy.load_policy())
        self.assertEqual(effective["approval_policy"], "on-request")
        self.assertIn("not allowed", policy.policy_problem(self.full))

    def test_every_sandboxed_mode_keeps_on_request_while_on(self):
        full_access.set_enabled(True, "admin-fixture")
        for options in ({}, {"codex_sandbox": "workspace-write"}, {"codex_sandbox": "read-only"},
                        {"codex_sandbox": "workspace-write", full_access.CHAT_OPTION: True},
                        {"codex_sandbox": "read-only", full_access.CHAT_OPTION: True},
                        {"codex_sandbox": "danger-full-access", full_access.CHAT_OPTION: "yes"}):
            with self.subTest(options=options):
                effective = policy.effective_policy(options, policy.load_policy())
                self.assertEqual(effective["approval_policy"], "on-request")

    def test_the_policy_file_still_cannot_set_never(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.json"
            path.write_text(json.dumps({"approval_policy": "never"}), encoding="utf-8")
            full_access.set_enabled(True, "admin-fixture")
            with self.assertRaises(policy.PolicyError):
                policy.load_policy(path)
            self.assertIn("never", policy.policy_problem(self.full, path))

    def test_codex_requests_with_never_start_only_beside_full_access(self):
        refused = [
            {"sandbox": "workspace-write", "approvalPolicy": "never", "approvalsReviewer": "user"},
            {"sandbox": "read-only", "approvalPolicy": "never", "approvalsReviewer": "user"},
            {"sandbox": "workspace-write", "approvalPolicy": "never", "approvalsReviewer": "auto_review"},
            {"sandboxPolicy": {"type": "workspaceWrite"}, "approvalPolicy": "never"},
            {"approvalPolicy": "never"},
            {"config": {"approval_policy": "never"}},
        ]
        for params in refused:
            for allow in (False, True):
                with self.subTest(params=params, allow=allow):
                    with self.assertRaises(ValueError):
                        approvals.require_supported_policy(params, allow_full_access=allow)
        for params in ({"sandbox": "danger-full-access", "approvalPolicy": "never"},
                       {"sandboxPolicy": {"type": "dangerFullAccess"}, "approvalPolicy": "never"},
                       {"sandbox": {"type": "dangerFullAccess"}, "approvalPolicy": "never"}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    approvals.require_supported_policy(params)
                approvals.require_supported_policy(params, allow_full_access=True)

    def session(self, allow_full_access):
        writer = Mock()
        thread = {"sandbox": "danger-full-access" if allow_full_access else "workspace-write",
                  "approvalPolicy": "never" if allow_full_access else "on-request", "approvalsReviewer": "user"}
        turn = {"input": [], "sandboxPolicy": {"type": "dangerFullAccess"} if allow_full_access else
                {"type": "workspaceWrite"}, "approvalPolicy": thread["approvalPolicy"], "approvalsReviewer": "user"}
        session = approvals.AppServerSession(
            writer, lock=threading.RLock(), thread_params=thread, turn_params=turn,
            on_event=lambda event: None, on_approvals=lambda items: None, on_final=lambda text: None,
            is_active=lambda: True, redact=lambda text: text, allow_full_access=allow_full_access)
        session.start()
        return session, writer

    def reply(self, session, request_id, result):
        session.receive({"id": request_id, "result": result})

    def test_codex_must_confirm_the_policy_the_worker_asked_for(self):
        cases = (
            (True, {"approvalPolicy": "never", "sandbox": {"type": "dangerFullAccess"}}, ""),
            (True, {"approvalPolicy": "on-request", "sandbox": {"type": "dangerFullAccess"}}, "never approval"),
            (True, {"approvalPolicy": "never", "sandbox": {"type": "workspaceWrite"}}, "never approval policy is refused"),
            (False, {"approvalPolicy": "on-request"}, ""),
            (False, {"approvalPolicy": "never", "sandbox": {"type": "dangerFullAccess"}}, "did not enable user approval"),
        )
        for allow, result, error in cases:
            with self.subTest(allow=allow, result=result):
                session, writer = self.session(allow)
                self.reply(session, "worker-1", {})
                thread_result = {"thread": {"id": "thread"}, "approvalsReviewer": "user", **result}
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        self.reply(session, "worker-2", thread_result)
                else:
                    self.reply(session, "worker-2", thread_result)
                    sent = json.loads(writer.write.call_args_list[-1].args[0])
                    self.assertEqual(sent["method"], "turn/start")
                    self.assertEqual(sent["params"]["approvalPolicy"], "never" if allow else "on-request")


class StaticRefusalTests(unittest.TestCase):
    """In Full access Codex asks nothing, so only refusals that never ask are left; they hold the same way.

    A turn with the never approval policy sends no permission request, so the
    command check (hook) sees each command at PreToolUse only. The plain
    Home Assistant action tool of the four-mode plan is not built yet; when it
    is, its own refusals get the same test.
    """

    def test_the_command_check_refuses_the_static_cases_before_any_tool_runs(self):
        # The literal forms of the app settings and of full access itself are the managed command rules' (the
        # requirements test above keeps them unchanged while on); the hook refuses these in every mode.
        for command in ("ha su info", "ha supervisor info", "cat /proc/1/environ", "printenv SUPERVISOR_TOKEN",
                        "cat /config/.storage/auth", "cat /config/secrets.yaml"):
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertTrue(gate_rules.check_tool_call(
                        "PreToolUse", {"hook_event_name": "PreToolUse", "tool_input": {"command": command}},
                        remote, gate_rules.STRICT_RULES))

    def test_an_ordinary_command_is_not_refused(self):
        self.assertEqual(gate_rules.check_tool_call(
            "PreToolUse", {"hook_event_name": "PreToolUse", "tool_input": {"command": "ls /config"}},
            False, gate_rules.STRICT_RULES), "")


class ChatModeTests(unittest.TestCase):
    """A chat set to Full access runs without the sandbox only while the setting is on, set by an administrator."""

    setUp_base = attachments.AttachmentTests.setUp
    create = attachments.AttachmentTests.create
    ingress = admin.AdministratorPairingTests.ingress

    def setUp(self):
        self.setUp_base()
        self.server = server
        self.setting = self.root / "full_access.json"
        self.stack.enter_context(patch.object(full_access, "SETTING_PATH", self.setting))
        self.stack.enter_context(patch.object(server, "lookup_ha_users", return_value=[
            {"id": "admin-fixture", "is_active": True, "system_generated": False, "group_ids": ["system-admin"]},
            {"id": "member-fixture", "is_active": True, "system_generated": False, "group_ids": []},
        ]))
        with server.admin_cache_lock:
            server.admin_cache.clear()
        self.task_id = self.create()
        server.tasks[self.task_id]["status"] = "completed"
        server.active_task_runners.clear()

    def choose(self, user):
        request = self.ingress(user)
        request["headers"] = {**request["headers"], **self.headers}
        return self.client.post(f"/tasks/{self.task_id}/settings", json={"permission_mode": "full-access"}, **request)

    def test_off_refuses_the_choice(self):
        response = self.choose("admin-fixture")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Full access is off", response.json["error"])

    def test_on_lets_only_an_administrator_choose_it(self):
        full_access.set_enabled(True, "admin-fixture")
        self.assertEqual(self.choose("member-fixture").status_code, 400)
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        options = server.task_options(self.task_id)
        self.assertEqual(options["codex_sandbox"], "danger-full-access")
        self.assertIs(options[full_access.CHAT_OPTION], True)
        self.assertEqual(server.protected_policy.policy_problem(options), "")

    def test_turning_it_off_returns_the_chat_to_the_sandbox(self):
        full_access.set_enabled(True, "admin-fixture")
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        full_access.set_enabled(False, "admin-fixture")
        options = server.task_options(self.task_id)
        self.assertNotEqual(options.get("codex_sandbox"), "danger-full-access")
        self.assertNotIn(full_access.CHAT_OPTION, options)

    def as_admin(self):
        request = self.ingress("admin-fixture")
        return {"headers": {**request["headers"], **self.headers},
                **{key: value for key, value in request.items() if key != "headers"}}

    def chat_set_then_turned_off(self):
        """An administrator sets the chat to Full access, then turns the setting off; the page has not reloaded."""
        full_access.set_enabled(True, "admin-fixture")
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        server.tasks[self.task_id]["session_id"] = attachments.SESSION_ID
        full_access.set_enabled(False, "admin-fixture")

    def test_next_message_after_turning_it_off_runs_with_default(self):
        self.chat_set_then_turned_off()
        for payload in ({"message": "Next", "permission_mode": "full-access"}, {"message": "Again"}):
            with self.subTest(payload=payload):
                server.tasks[self.task_id]["status"] = "completed"
                server.active_task_runners.clear()
                response = self.client.post(f"/tasks/{self.task_id}/continue", json=payload, **self.as_admin())
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(server.tasks[self.task_id]["turns"][-1]["permission_mode"], "default")
                self.assertEqual(server.tasks[self.task_id]["permission_mode"], "default")
                self.assertNotIn(full_access.CHAT_OPTION, server.task_options(self.task_id))

    def test_turning_it_on_again_does_not_bring_back_a_chat_that_ran_with_default(self):
        self.chat_set_then_turned_off()
        response = self.client.post(f"/tasks/{self.task_id}/continue",
                                    json={"message": "Next", "permission_mode": "full-access"}, **self.as_admin())
        self.assertEqual(response.status_code, 200, response.json)
        full_access.set_enabled(True, "admin-fixture")
        self.assertNotIn(full_access.CHAT_OPTION, server.task_options(self.task_id))

    def test_new_chat_sent_with_full_access_after_turning_it_off_runs_with_default(self):
        response = self.client.post("/tasks", json={"prompt": "Hello", "permission_mode": "full-access"},
                                    **self.as_admin())
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(server.tasks[response.json["task_id"]]["permission_mode"], "default")

    def test_model_change_after_turning_it_off_is_not_refused(self):
        self.chat_set_then_turned_off()
        response = self.client.post(f"/tasks/{self.task_id}/settings",
                                    json={"chat_settings": {"model": None, "reasoning_effort": None}},
                                    **self.as_admin())
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["permission_mode"], "default")

    def test_member_message_with_full_access_is_refused_while_on(self):
        self.chat_set_then_turned_off()
        full_access.set_enabled(True, "admin-fixture")
        request = self.ingress("member-fixture")
        request["headers"] = {**request["headers"], **self.headers}
        response = self.client.post(f"/tasks/{self.task_id}/continue",
                                    json={"message": "Next", "permission_mode": "full-access"}, **request)
        self.assertEqual(response.status_code, 400)

    def turn_setting(self, enabled):
        """Turn the setting on or off the way the Settings page does, as an administrator."""
        for name in ("REQUIREMENTS_PATH", "IMAGE_REQUIREMENTS"):
            self.stack.enter_context(patch.object(full_access, name, self.root / name.lower()))
        self.stack.enter_context(patch.object(server.remote_control_supervisor, "enabled", return_value=False))
        body = {"enabled": True, "confirm": full_access.WARNING_ID} if enabled else {"enabled": False}
        return self.client.post("/full-access", json=body, **self.as_admin())

    def saved_mode(self):
        task_file = self.root / "tasks" / self.task_id / "task.json"
        return json.loads(task_file.read_text(encoding="utf-8"))["permission_mode"]

    def test_turning_it_off_and_on_again_starts_the_chat_at_default(self):
        self.assertEqual(self.turn_setting(True).status_code, 200)
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        self.assertEqual(self.turn_setting(False).status_code, 200)
        self.assertEqual(server.tasks[self.task_id]["permission_mode"], "default")
        self.assertEqual(self.saved_mode(), "default")
        self.assertEqual(self.turn_setting(True).status_code, 200)
        self.assertNotIn(full_access.CHAT_OPTION, server.task_options(self.task_id))
        server.tasks[self.task_id]["session_id"] = attachments.SESSION_ID
        response = self.client.post(f"/tasks/{self.task_id}/continue", json={"message": "Next"}, **self.as_admin())
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(server.tasks[self.task_id]["turns"][-1]["permission_mode"], "default")

    def test_the_owner_can_choose_it_again_after_turning_it_on(self):
        self.turn_setting(True)
        self.choose("admin-fixture")
        self.turn_setting(False)
        self.turn_setting(True)
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        self.assertIs(server.task_options(self.task_id)[full_access.CHAT_OPTION], True)

    def test_turning_it_off_returns_a_waiting_message_to_default(self):
        queue_file = self.root / "queue.json"
        self.stack.enter_context(patch.object(server, "MESSAGE_QUEUE_FILE", queue_file))
        self.stack.enter_context(patch.object(server, "message_queue", [
            {"task_id": self.task_id, "permission_mode": "full-access"},
            {"task_id": self.task_id, "permission_mode": "read-only"},
        ]))
        self.turn_setting(True)
        self.assertEqual(self.turn_setting(False).status_code, 200)
        self.assertEqual([entry["permission_mode"] for entry in server.message_queue], ["default", "read-only"])
        saved = json.loads(queue_file.read_text(encoding="utf-8"))
        self.assertEqual([entry["permission_mode"] for entry in saved], ["default", "read-only"])

    def test_a_failed_change_returns_the_chat_to_default(self):
        self.turn_setting(True)
        self.choose("admin-fixture")
        with patch.object(full_access, "write_requirements", side_effect=OSError("disk full")):
            self.assertEqual(self.turn_setting(False).status_code, 503)
        self.assertFalse(full_access.is_on())
        self.assertEqual(server.tasks[self.task_id]["permission_mode"], "default")

    def test_turning_it_on_changes_no_chat(self):
        self.turn_setting(True)
        self.choose("admin-fixture")
        self.assertEqual(self.turn_setting(True).status_code, 200)
        self.assertEqual(server.tasks[self.task_id]["permission_mode"], "full-access")

    def test_api_token_cannot_choose_it(self):
        full_access.set_enabled(True, "admin-fixture")
        response = self.client.post(f"/tasks/{self.task_id}/settings",
                                    json={"permission_mode": "full-access"}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_app_option_alone_never_reaches_a_chat(self):
        full_access.set_enabled(True, "admin-fixture")
        with patch.object(server, "read_options", return_value={"codex_sandbox": "danger-full-access",
                                                               full_access.CHAT_OPTION: True}):
            options = server.task_options(self.task_id)
            self.assertNotIn(full_access.CHAT_OPTION, options)
            self.assertIn("not allowed", server.protected_policy.policy_problem(options))


    def launch(self):
        """Build the app-server exchange of the chat's next turn, as run_task does, with a fake process."""
        proc = Mock()
        proc.stdin = Mock()
        return server.create_approval_session(self.task_id, proc, "Next", self.root / "final.txt", None, {},
                                              server.task_options(self.task_id))

    def test_the_full_access_launch_carries_never_and_no_sandbox(self):
        full_access.set_enabled(True, "admin-fixture")
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        session = self.launch()
        self.assertEqual(session.thread_params["sandbox"], "danger-full-access")
        self.assertEqual(session.thread_params["approvalPolicy"], "never")
        self.assertEqual(session.turn_params["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(session.turn_params["approvalPolicy"], "never")
        self.assertIs(session.allow_full_access, True)

    def test_every_other_launch_carries_on_request(self):
        full_access.set_enabled(True, "admin-fixture")
        for mode in ("default", "read-only"):
            with self.subTest(mode=mode):
                server.tasks[self.task_id]["permission_mode"] = mode
                session = self.launch()
                self.assertEqual(session.thread_params["approvalPolicy"], "on-request")
                self.assertEqual(session.turn_params["approvalPolicy"], "on-request")
                self.assertIs(session.allow_full_access, False)
        # A chat saved with Full access while the setting is off launches sandboxed, asking the user.
        server.tasks[self.task_id]["permission_mode"] = "full-access"
        full_access.set_enabled(False, "admin-fixture")
        session = self.launch()
        self.assertEqual(session.thread_params["sandbox"], "workspace-write")
        self.assertEqual(session.turn_params["approvalPolicy"], "on-request")

    def send(self, payload):
        """Send the next message of the chat from the page as an administrator."""
        server.tasks[self.task_id]["status"] = "completed"
        server.tasks[self.task_id]["session_id"] = attachments.SESSION_ID
        server.active_task_runners.clear()
        return self.client.post(f"/tasks/{self.task_id}/continue", json=payload, **self.as_admin())

    def test_a_tab_that_missed_an_off_and_on_cycle_sends_with_default(self):
        """CX-FULLACCESS.pr3 F1: the page's old choice survives in a tab that slept through the cycle."""
        self.turn_setting(True)
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        self.turn_setting(False)
        self.turn_setting(True)
        response = self.send({"message": "Next", "permission_mode": "full-access"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(server.tasks[self.task_id]["turns"][-1]["permission_mode"], "default")
        self.assertEqual(server.tasks[self.task_id]["permission_mode"], "default")
        self.assertNotIn(full_access.CHAT_OPTION, server.task_options(self.task_id))

    def test_a_choice_made_after_the_cycle_sends_with_full_access(self):
        self.turn_setting(True)
        self.choose("admin-fixture")
        self.turn_setting(False)
        self.turn_setting(True)
        self.assertEqual(self.choose("admin-fixture").status_code, 200)
        for payload in ({"message": "Next", "permission_mode": "full-access"}, {"message": "Again"}):
            with self.subTest(payload=payload):
                response = self.send(payload)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(server.tasks[self.task_id]["turns"][-1]["permission_mode"], "full-access")

    def new_chat_mode(self, payload):
        """Start a new chat from the page as an administrator; return the mode it was saved with."""
        response = self.client.post("/tasks", json={"prompt": "Hello", **payload}, **self.as_admin())
        self.assertEqual(response.status_code, 200, response.json)
        task = server.tasks[response.json["task_id"]]
        task["status"] = "completed"
        server.active_task_runners.clear()
        return task["permission_mode"]

    def test_a_new_chat_needs_the_time_the_setting_was_turned_on(self):
        self.turn_setting(True)
        since = full_access.read_setting()["changed_at"]
        cases = ((since, "full-access"), (None, "default"), (since - 1, "default"), (True, "default"),
                 (str(since), "default"))
        for value, expected in cases:
            with self.subTest(full_access_since=value):
                self.assertEqual(self.new_chat_mode({"permission_mode": "full-access", "full_access_since": value}),
                                 expected)
        self.assertEqual(self.new_chat_mode({"permission_mode": "full-access"}), "default")

    def test_a_new_chat_chosen_before_an_off_and_on_cycle_starts_with_default(self):
        self.turn_setting(True)
        before = full_access.read_setting()["changed_at"]
        self.turn_setting(False)
        with patch.object(full_access.time, "time", return_value=before + 5):
            self.turn_setting(True)
        self.assertEqual(self.new_chat_mode({"permission_mode": "full-access", "full_access_since": before}),
                         "default")


class SettingRouteTests(unittest.TestCase):
    """Only an administrator on the app page changes the setting, and turning it on needs the confirmed warning."""

    setUp_base = admin.AdministratorPairingTests.setUp
    ingress = admin.AdministratorPairingTests.ingress

    def setUp(self):
        self.setUp_base()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.setting = Path(folder.name) / "full_access.json"
        self.target = Path(folder.name) / "etc" / "requirements.toml"
        self.target.parent.mkdir()
        for name, value in (("SETTING_PATH", self.setting), ("REQUIREMENTS_PATH", self.target),
                            ("IMAGE_REQUIREMENTS", Path(folder.name) / "missing.toml")):
            patcher = patch.object(full_access, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.server.remote_control_supervisor, "enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body, user="admin-fixture"):
        return self.client.post("/full-access", json=body, **self.ingress(user))

    def test_non_administrator_and_api_token_cannot_change_it(self):
        confirm = {"enabled": True, "confirm": full_access.WARNING_ID}
        for user in ("", "member-fixture", "unknown-fixture"):
            with self.subTest(user=user):
                self.assertEqual(self.post(confirm, user).status_code, 403)
        response = self.client.post("/full-access", json=confirm, headers={"Authorization": "Bearer fixture"})
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(full_access.is_on())

    def test_turning_it_on_needs_the_confirmed_warning(self):
        for body in ({"enabled": True}, {"enabled": True, "confirm": "yes"}, {"enabled": "true"}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(full_access.is_on())
        response = self.post({"enabled": True, "confirm": full_access.WARNING_ID})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["full_access"]["enabled"])
        self.assertIn(full_access.OPEN_MODES_LINE, self.target.read_text(encoding="utf-8"))

    def test_turning_it_off_restores_the_shipped_requirements(self):
        self.post({"enabled": True, "confirm": full_access.WARNING_ID})
        response = self.post({"enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(full_access.is_on())
        self.assertEqual(self.target.read_bytes(), SHIPPED.read_bytes())

    def test_remote_control_restarts_so_its_codex_reads_the_new_file(self):
        with patch.object(self.server.remote_control_supervisor, "enabled", return_value=True), \
                patch.object(self.server.remote_control_supervisor, "stop") as stop, \
                patch.object(self.server.remote_control_supervisor, "start") as start:
            self.post({"enabled": True, "confirm": full_access.WARNING_ID})
        stop.assert_called_once()
        start.assert_called_once()

    def test_anyone_signed_in_can_read_it(self):
        response = self.client.get("/full-access", headers={"Authorization": "Bearer fixture"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["full_access"]["enabled"])
        self.assertEqual(response.json["full_access"]["warning_id"], full_access.WARNING_ID)


class AppArmorTests(unittest.TestCase):
    def test_the_worker_may_write_only_the_requirements_file_in_etc(self):
        rules = [line.strip() for line in (ROOT / "apparmor.txt").read_text(encoding="utf-8").splitlines()]
        etc_writes = [rule for rule in rules if rule.startswith("/etc/") and rule.rstrip(",").split()[-1] != "r"]
        self.assertEqual(etc_writes, ["/etc/codex/requirements.toml w,", "/etc/codex/requirements.toml.tmp w,"])
        self.assertEqual(full_access.REQUIREMENTS_PATH, Path("/etc/codex/requirements.toml"))


class PageTests(unittest.TestCase):
    """The page marks the setting and asks for confirmation before turning it on."""

    def test_page_has_the_warning_dialog_banner_and_choice(self):
        page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "assets" / "full_access.js").read_text(encoding="utf-8")
        for marker in ('id="full-access-banner"', 'id="full-access-dialog"', 'data-mode="full-access" disabled',
                       'assets/full_access.js'):
            self.assertIn(marker, page)
        self.assertNotIn("Full access · Coming", page)
        self.assertIn("confirm: fullAccess.warning_id", script)
        self.assertIn('$("full-access-dialog").showModal()', script)


if __name__ == "__main__":
    unittest.main()
