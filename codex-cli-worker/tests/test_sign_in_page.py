"""Sign-in without a terminal, the pause switch, and the model pin."""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
if importlib.util.find_spec("websocket") is None:
    sys.modules["websocket"] = types.ModuleType("websocket")
SPEC = importlib.util.spec_from_file_location("codex_worker_server_sign_in", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

# The format `codex login --device-auth` prints, as the worker's own parser expects it.
# Replace with the lab capture once it is committed.
LOGIN_OUTPUT = [
    "Welcome to Codex\n",
    "1. Open this link in your browser and sign in to your account\n",
    "   https://auth.openai.com/codex/device\n",
    "2. Enter this one-time code (expires in 15 minutes)\n",
    "   ABCD-EFGH2\n",
]
PIN = "gpt-6.1-sol"


class _Worker(unittest.TestCase):
    """A worker with temporary storage, no real Codex, and a signed-out account."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("active_task_runners", set()), ("running_processes", {}),
                            ("message_queue", []), ("auth_state", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "auth_process", None))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "MESSAGE_QUEUE_FILE", self.root / "queue.json"))
        self.stack.enter_context(patch.object(server, "PAUSE_FILE", self.root / "paused.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "fire_ha_event", return_value=(True, "")))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.stack.enter_context(patch.object(server, "create_persistent_notification"))
        self.stack.enter_context(patch.object(server, "dismiss_persistent_notification"))
        self.stack.enter_context(patch.object(server, "write_login_qr", return_value="/local/x.svg"))
        self.stack.enter_context(patch.object(server, "write_task_log"))
        self.stack.enter_context(patch.object(
            server, "codex_login_status", return_value={"status_ok": False, "has_auth_file": False, "message": ""}))
        self.runner = self.stack.enter_context(patch.object(server, "start_background_task"))
        self.options = {"model_pin": PIN}
        self.stack.enter_context(patch.object(server, "read_options", side_effect=lambda: dict(self.options)))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}


class SignInTests(_Worker):
    """The code and QR the page shows stay put until the sign-in ends or is canceled."""

    def read_login(self, lines):
        server.auth_state.update({"login_id": "L1", "status": "starting"})
        server.auth_reader_thread(io.StringIO("".join(lines)))

    def test_code_and_link_are_parsed_and_the_qr_is_served_inline(self):
        self.read_login(LOGIN_OUTPUT)
        auth = self.client.get("/auth/status", headers=self.headers).get_json()["auth"]
        self.assertEqual(auth["user_code"], "ABCD-EFGH2")
        self.assertEqual(auth["verification_url"], "https://auth.openai.com/codex/device")
        # A path relative to the page, so it loads through Ingress; not /local/.
        self.assertEqual(auth["qr_path"], "auth/qr.svg?login=L1")
        response = self.client.get("/auth/qr.svg", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/svg+xml")
        self.assertIn(b"<svg", response.data)
        self.assertIn(b'fill="#fff"', response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_a_later_code_in_the_output_never_replaces_the_one_shown(self):
        self.read_login(LOGIN_OUTPUT + ["Code refreshed: WXYZ-12345\n"])
        self.assertEqual(server.auth_state["user_code"], "ABCD-EFGH2")

    def test_qr_is_404_without_a_waiting_sign_in(self):
        self.assertEqual(self.client.get("/auth/qr.svg", headers=self.headers).status_code, 404)
        server.auth_state["verification_url"] = "http://not-https.example/device"
        self.assertEqual(self.client.get("/auth/qr.svg", headers=self.headers).status_code, 404)

    def test_a_second_tap_on_sign_in_keeps_the_running_sign_in(self):
        class Running:
            def poll(self):
                return None
        with patch.object(server, "auth_process", Running()), patch.object(server.threading, "Thread") as thread:
            server.auth_state.update({"login_id": "L1", "status": "waiting_for_user", "user_code": "ABCD-EFGH2"})
            auth = self.client.post("/auth/start", json={}, headers=self.headers).get_json()["auth"]
        thread.assert_not_called()
        self.assertEqual(auth["user_code"], "ABCD-EFGH2")
        self.assertTrue(auth["pending"])

    def test_cancel_stops_the_sign_in_and_clears_the_code_without_a_failure_notice(self):
        class Proc:
            terminated = False
            def poll(self):
                return None if not self.terminated else -15
            def terminate(self):
                self.terminated = True
            def wait(self, timeout=None):
                return -15
        proc = Proc()
        with patch.object(server, "auth_process", proc):
            server.auth_state.update({"login_id": "L1", "status": "waiting_for_user", "user_code": "ABCD-EFGH2",
                                      "verification_url": "https://auth.openai.com/codex/device"})
            auth = self.client.post("/auth/cancel", json={}, headers=self.headers).get_json()["auth"]
        self.assertTrue(proc.terminated)
        self.assertEqual(auth["status"], "canceled")
        self.assertEqual(auth["user_code"], "")
        self.assertEqual(auth["qr_path"], "")
        self.assertFalse(auth["pending"])
        # The login process then exits non-zero; that is the cancel, not a failure to report.
        with patch.object(server.subprocess, "Popen") as popen, patch.object(server, "update_auth_state") as update:
            popen.return_value.stdout = io.StringIO("")
            popen.return_value.wait.return_value = -15
            server.auth_state.update({"status": "canceled", "login_id": "L1"})
            update.side_effect = lambda **kw: None
            server.run_codex_device_login("L1")
        server.notify.assert_not_called()

    def test_status_reports_the_sign_in_flow_and_pause_for_the_integration(self):
        data = self.client.get("/status", headers=self.headers).get_json()
        self.assertIn("pending", data["auth_flow"])
        self.assertEqual(data["pause"], {"paused": False})
        self.assertEqual(data["model_pin"], PIN)


class PauseTests(_Worker):
    """Pause refuses new messages and holds the queue; resume starts what waited."""

    def test_pause_refuses_new_chats_and_messages_and_resume_allows_them(self):
        self.assertTrue(self.client.post("/pause", json={"paused": True}, headers=self.headers).get_json()["paused"])
        response = self.client.post("/tasks", json={"prompt": "hello"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["paused"])
        response = self.client.post("/tasks/x/continue", json={"message": "hello"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.runner.assert_not_called()
        self.assertFalse(self.client.post("/pause", json={"paused": False}, headers=self.headers).get_json()["paused"])
        response = self.client.post("/tasks", json={"prompt": "hello"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.runner.assert_called_once()

    def test_pause_survives_a_restart_and_rejects_a_bad_body(self):
        self.client.post("/pause", json={"paused": True}, headers=self.headers)
        self.assertTrue((self.root / "paused.json").exists())
        self.assertTrue(self.client.get("/pause", headers=self.headers).get_json()["paused"])
        self.assertEqual(self.client.post("/pause", json={"paused": "yes"}, headers=self.headers).status_code, 400)

    def test_queued_messages_wait_while_paused_and_start_on_resume(self):
        server.message_queue.append({"task_id": "t1"})
        with patch.object(server, "start_queued_message", return_value=None) as start:
            server.set_assistant_paused(True)
            server.start_next_queued()
            start.assert_not_called()
            server.set_assistant_paused(False)
            start.assert_called_once()


class ModelPinTests(_Worker):
    """With the pin set, every run names the pinned model and nothing else runs."""

    def test_shipped_default_is_the_pinned_model(self):
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(config["options"]["model_pin"], server.DEFAULT_MODEL_PIN)
        self.assertEqual(server.DEFAULT_MODEL_PIN, PIN)
        self.assertEqual(config["schema"]["model_pin"], "str?")

    def test_default_resolves_to_the_pin_and_is_passed_to_codex(self):
        execution = server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, self.options)
        self.assertEqual(execution["model"], PIN)
        with patch.object(server, "tasks", {"t": {"turns": [{"execution_settings": execution}]}}):
            args = server.build_codex_args("t", Path("p"), Path("f"), None)
        self.assertEqual(args[args.index("--model") + 1], PIN)

    def test_another_model_is_refused_before_anything_runs(self):
        response = self.client.post("/tasks", json={"prompt": "hi", "chat_settings": {"model": "gpt-6-astra"}},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(PIN, response.get_json()["error"])
        self.runner.assert_not_called()

    def test_a_saved_turn_on_another_model_is_stopped_as_an_error(self):
        execution = {"model": "gpt-6-astra", "reasoning_effort": "medium"}
        self.assertIn("gpt-6-astra", server.model_pin_error(execution, self.options))
        self.assertEqual(server.model_pin_error({"model": PIN}, self.options), "")
        self.assertEqual(server.model_pin_error(execution, {"model_pin": "none"}), "")

    def test_codex_model_option_disagreeing_with_the_pin_is_shown_not_crashed(self):
        self.options["codex_model"] = "gpt-6-luna"
        data = self.client.get("/chat-options", headers=self.headers).get_json()
        self.assertTrue(data["ok"])
        self.assertIn("gpt-6-luna", data["model_error"])
        self.assertEqual([model["id"] for model in data["models"]], [PIN])

    def test_none_turns_the_pin_off(self):
        self.assertEqual(server.model_pin({"model_pin": "none"}), "")
        self.assertEqual(server.model_pin({"model_pin": " OFF "}), "")
        self.assertEqual(server.model_pin({}), "")
        execution = server.resolve_chat_settings({"model": "gpt-6-astra", "reasoning_effort": None}, {"model_pin": "none"})
        self.assertEqual(execution["model"], "gpt-6-astra")


if __name__ == "__main__":
    unittest.main()
