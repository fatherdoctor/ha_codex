"""Full access: off by default, on only after an administrator confirms the warning, and only for chats set to it."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(written.replace(full_access.OPEN_MODES_LINE, full_access.STRICT_MODES_LINE),
                         SHIPPED.read_text(encoding="utf-8"))
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o644)

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
