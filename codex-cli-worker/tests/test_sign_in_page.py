"""Sign-in without a terminal, no pause switch, and the model pin."""

from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from app_server_fixture import StubAppServer
import yaml

SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
if importlib.util.find_spec("websocket") is None:
    sys.modules["websocket"] = types.ModuleType("websocket")
SPEC = importlib.util.spec_from_file_location(
    "codex_worker_server_sign_in", SERVER_PATH
)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

# The format `codex login --device-auth` prints, as the worker's own parser expects it.
# Replace with the lab capture once it is committed.
# The real `codex login --device-auth` output of Codex 0.166.0 (ANSI colors kept), with a test code in place of the
# captured one.
LOGIN_OUTPUT = (
    (Path(__file__).resolve().parent / "fixtures" / "codex-login-device-auth.txt")
    .read_text(encoding="utf-8")
    .splitlines(keepends=True)
)
PIN = "gpt-6.1-sol"


class _Worker(unittest.TestCase):
    """A worker with temporary storage, no real Codex, and a signed-out account."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(server.tasks, clear=True))
        for name, value in (
            ("active_task_runners", set()),
            ("running_processes", {}),
            ("message_queue", []),
            ("auth_state", {}),
        ):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "auth_process", None))
        self.stack.enter_context(
            patch.object(server, "task_root", return_value=self.root / "tasks")
        )
        self.stack.enter_context(
            patch.object(server, "TASK_STATE_FILE", self.root / "index.json")
        )
        self.stack.enter_context(
            patch.object(server, "MESSAGE_QUEUE_FILE", self.root / "queue.json")
        )
        self.stack.enter_context(
            patch.object(server, "CODEX_HOME", self.root / "codex")
        )
        self.stack.enter_context(
            patch.object(server, "api_token", return_value="test-token")
        )
        self.stack.enter_context(
            patch.object(server, "fire_ha_event", return_value=(True, ""))
        )
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.stack.enter_context(patch.object(server, "create_persistent_notification"))
        self.stack.enter_context(
            patch.object(server, "dismiss_persistent_notification")
        )
        self.stack.enter_context(patch.object(server, "write_task_log"))
        self.stack.enter_context(
            patch.object(
                server,
                "codex_login_status",
                return_value={
                    "status_ok": False,
                    "has_auth_file": False,
                    "message": "",
                },
            )
        )
        self.runner = self.stack.enter_context(
            patch.object(server, "start_background_task")
        )
        self.options = {"model_pin": PIN}
        self.stack.enter_context(
            patch.object(server, "read_options", side_effect=lambda: dict(self.options))
        )
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}


class SignInTests(_Worker):
    """The code and QR the page shows stay put until the sign-in ends or is canceled."""

    def read_login(self, lines):
        server.auth_state.update({"login_id": "L1", "status": "starting"})
        server.auth_reader_thread(io.StringIO("".join(lines)))

    @unittest.skipIf(
        importlib.util.find_spec("qrcode") is None,
        "the qrcode package is installed in the app image only",
    )
    def test_code_and_link_are_parsed_and_the_qr_is_served_inline(self):
        self.read_login(LOGIN_OUTPUT)
        auth = self.client.get("/auth/status", headers=self.headers).get_json()["auth"]
        self.assertEqual(auth["user_code"], "ABCD-EFGH2")
        self.assertEqual(
            auth["verification_url"], "https://auth.openai.com/codex/device"
        )
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
        self.assertEqual(
            self.client.get("/auth/qr.svg", headers=self.headers).status_code, 404
        )
        server.auth_state["verification_url"] = "http://not-https.example/device"
        self.assertEqual(
            self.client.get("/auth/qr.svg", headers=self.headers).status_code, 404
        )

    def test_a_second_tap_on_sign_in_keeps_the_running_sign_in(self):
        class Running:
            def poll(self):
                return None

        with (
            patch.object(server, "auth_process", Running()),
            patch.object(server.threading, "Thread") as thread,
        ):
            server.auth_state.update(
                {
                    "login_id": "L1",
                    "status": "waiting_for_user",
                    "user_code": "ABCD-EFGH2",
                }
            )
            auth = self.client.post(
                "/auth/start", json={}, headers=self.headers
            ).get_json()["auth"]
        thread.assert_not_called()
        self.assertEqual(auth["user_code"], "ABCD-EFGH2")
        self.assertTrue(auth["pending"])

    def test_cancel_stops_the_sign_in_and_clears_the_code_without_a_failure_notice(
        self,
    ):
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
            server.auth_state.update(
                {
                    "login_id": "L1",
                    "status": "waiting_for_user",
                    "user_code": "ABCD-EFGH2",
                    "verification_url": "https://auth.openai.com/codex/device",
                }
            )
            auth = self.client.post(
                "/auth/cancel", json={}, headers=self.headers
            ).get_json()["auth"]
        self.assertTrue(proc.terminated)
        self.assertEqual(auth["status"], "canceled")
        self.assertEqual(auth["user_code"], "")
        self.assertEqual(auth["qr_path"], "")
        self.assertFalse(auth["pending"])
        # The login process then exits non-zero; that is the cancel, not a failure to report.
        with (
            patch.object(server.subprocess, "Popen") as popen,
            patch.object(server, "update_auth_state") as update,
        ):
            popen.return_value.stdout = io.StringIO("")
            popen.return_value.wait.return_value = -15
            server.auth_state.update(
                {"status": "canceled", "login_id": "L1", "canceled_login_id": "L1"}
            )
            update.side_effect = lambda **kw: None
            server.run_codex_device_login("L1")
        server.notify.assert_not_called()

    def test_cancel_of_a_real_sign_in_process_raises_no_failure_notice(self):
        """A real child process stands in for `codex login`, so the sign-in thread wakes when Cancel stops it.

        Cancel must be recorded before the process stops; otherwise the thread sees a stopped sign-in that is
        not canceled and reports a failure.
        """
        fixture = (
            Path(__file__).resolve().parent / "fixtures" / "codex-login-device-auth.txt"
        )
        child = [
            sys.executable,
            "-c",
            "import sys, time; sys.stdout.write(open(sys.argv[1]).read()); sys.stdout.flush(); time.sleep(30)",
            str(fixture),
        ]
        real_popen = server.subprocess.Popen

        def start_child(*_args, **kwargs):
            return real_popen(
                child, stdout=kwargs["stdout"], stderr=kwargs["stderr"], text=True
            )

        for attempt in range(3):
            with (
                self.subTest(attempt=attempt),
                patch.object(server.subprocess, "Popen", side_effect=start_child),
            ):
                server.notify.reset_mock()
                before = set(server.threading.enumerate())
                server.start_codex_login_flow()
                for _ in range(250):
                    if server.auth_state.get("user_code"):
                        break
                    server.time.sleep(0.02)
                self.assertEqual(server.auth_state.get("user_code"), "ABCD-EFGH2")
                server.cancel_codex_login()
                for started in set(server.threading.enumerate()) - before:
                    started.join(timeout=10)
                server.notify.assert_not_called()
                self.assertEqual(server.auth_state["status"], "canceled")
                self.assertEqual(server.auth_state["user_code"], "")

    def test_the_code_lifetime_is_read_and_counted_by_the_worker(self):
        """The page gets the seconds left from the worker's clock, never the private monotonic deadline."""

        class Running:
            def poll(self):
                return None

        self.read_login(LOGIN_OUTPUT)
        with patch.object(server, "auth_process", Running()):
            auth = self.client.get("/auth/status", headers=self.headers).get_json()[
                "auth"
            ]
        self.assertTrue(auth["pending"])
        self.assertTrue(895 <= auth["expires_in"] <= 900, auth["expires_in"])
        self.assertTrue(auth["expires_at"])
        self.assertNotIn("code_deadline", auth)

    def test_no_lifetime_in_the_output_gives_no_countdown(self):
        class Running:
            def poll(self):
                return None

        self.read_login(
            [line.replace("(expires in 15 minutes)", "") for line in LOGIN_OUTPUT]
        )
        with patch.object(server, "auth_process", Running()):
            auth = self.client.get("/auth/status", headers=self.headers).get_json()[
                "auth"
            ]
        self.assertEqual(auth["user_code"], "ABCD-EFGH2")
        self.assertIsNone(auth["expires_in"])

    def test_a_failed_sign_in_says_why_without_repeating_the_code(self):
        output = (
            "".join(LOGIN_OUTPUT)
            + "Error: device code ABCD-EFGH2 was denied by the server\n"
        )
        with patch.object(server.subprocess, "Popen") as popen:
            popen.return_value.stdout = io.StringIO(output)
            popen.return_value.wait.return_value = 1
            server.auth_state.update({"login_id": "L1"})
            server.run_codex_device_login("L1")
        self.assertEqual(server.auth_state["status"], "failed")
        self.assertIn("was denied by the server", server.auth_state["error"])
        self.assertNotIn("ABCD-EFGH2", server.auth_state["error"])
        server.notify.assert_called_once_with(
            "Codex sign-in failed", server.auth_state["error"]
        )

    def test_an_expired_code_is_named_as_the_reason(self):
        server.auth_state.update(
            {"code_deadline": server.time.monotonic() - 1, "output": "Error: timed out"}
        )
        self.assertIn("The code expired", server.sign_in_failure_reason(1))
        server.auth_state.update({"code_deadline": None, "output": "nothing useful"})
        self.assertIn("exit code 1", server.sign_in_failure_reason(1))

    def test_get_a_new_code_leaves_the_new_sign_in_alone(self):
        """Cancel then start: the old sign-in's end must not clear the new process or mark the new one failed."""
        newer = object()

        def replaced_while_waiting(*_args, **_kwargs):
            server.auth_state.update({"login_id": "L2", "status": "starting"})
            server.auth_process = newer
            return -15

        with patch.object(server.subprocess, "Popen") as popen:
            popen.return_value.stdout = io.StringIO("")
            popen.return_value.wait.side_effect = replaced_while_waiting
            server.auth_state.update({"login_id": "L1"})
            server.run_codex_device_login("L1")
        self.assertIs(server.auth_process, newer)
        self.assertEqual(server.auth_state["status"], "starting")
        server.notify.assert_not_called()

    def test_the_reader_of_a_replaced_sign_in_stops(self):
        server.auth_state.update({"login_id": "L2", "status": "starting"})
        server.auth_reader_thread(io.StringIO("".join(LOGIN_OUTPUT)), "L1")
        self.assertNotIn("user_code", server.auth_state)
        server.create_persistent_notification.assert_not_called()

    def test_status_reports_the_sign_in_flow_and_pin_for_the_integration(self):
        data = self.client.get("/status", headers=self.headers).get_json()
        self.assertIn("pending", data["auth_flow"])
        self.assertNotIn("pause", data)
        self.assertEqual(data["model_pin"], PIN)


class NoPauseTests(_Worker):
    """The assistant has no pause switch: no route, no refusal and no hold on the queue."""

    def test_the_pause_route_is_gone(self):
        self.assertEqual(
            self.client.get("/pause", headers=self.headers).status_code, 404
        )
        self.assertEqual(
            self.client.post(
                "/pause", json={"paused": True}, headers=self.headers
            ).status_code,
            404,
        )
        self.assertFalse(hasattr(server, "assistant_paused"))

    def test_a_new_chat_starts_without_a_pause_check(self):
        response = self.client.post(
            "/tasks", json={"prompt": "hello"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.runner.assert_called_once()

    def test_a_queued_message_starts_once_nothing_runs(self):
        server.message_queue.append({"task_id": "t1"})
        with patch.object(server, "start_queued_message", return_value=None) as start:
            server.start_next_queued()
            start.assert_called_once()


class ModelPinTests(_Worker):
    """With the pin set, every run names the pinned model and nothing else runs."""

    def test_a_fresh_install_pins_nothing(self):
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("model_pin", config["options"])
        self.assertEqual(config["schema"]["model_pin"], "str?")
        self.assertEqual(server.model_pin(config["options"]), "")
        execution = server.resolve_chat_settings(
            server.DEFAULT_CHAT_SETTINGS, config["options"]
        )
        self.assertEqual(execution["model"], "default")

    def test_an_owner_value_still_pins(self):
        self.assertEqual(server.model_pin({"model_pin": "gpt-6-astra"}), "gpt-6-astra")

    def test_default_resolves_to_the_pin_and_is_passed_to_codex(self):
        execution = server.resolve_chat_settings(
            server.DEFAULT_CHAT_SETTINGS, self.options
        )
        self.assertEqual(execution["model"], PIN)
        with patch.dict(
            server.tasks,
            {"t": {"turns": [{"execution_settings": execution}]}},
            clear=True,
        ):
            session = server.create_approval_session(
                "t",
                StubAppServer("thread"),
                "Prompt",
                Path("f"),
                None,
                {},
                self.options,
            )
        self.assertEqual(session.thread_params["model"], PIN)

    def test_another_model_is_refused_before_anything_runs(self):
        response = self.client.post(
            "/tasks",
            json={"prompt": "hi", "chat_settings": {"model": "gpt-6-astra"}},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(PIN, response.get_json()["error"])
        self.runner.assert_not_called()

    def test_a_saved_turn_on_another_model_is_stopped_as_an_error(self):
        execution = {"model": "gpt-6-astra", "reasoning_effort": "medium"}
        self.assertIn("gpt-6-astra", server.model_pin_error(execution, self.options))
        self.assertEqual(server.model_pin_error({"model": PIN}, self.options), "")
        self.assertEqual(server.model_pin_error(execution, {"model_pin": "none"}), "")

    def test_pin_takes_priority_over_the_inherited_custom_model(self):
        self.options["codex_model"] = "gpt-6-luna"
        data = self.client.get("/chat-options", headers=self.headers).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["model_error"], "")
        self.assertEqual([model["id"] for model in data["models"]], [PIN])

    def test_none_turns_the_pin_off(self):
        self.assertEqual(server.model_pin({"model_pin": "none"}), "")
        self.assertEqual(server.model_pin({"model_pin": " OFF "}), "")
        self.assertEqual(server.model_pin({}), "")
        execution = server.resolve_chat_settings(
            {"model": "gpt-6-astra", "reasoning_effort": None}, {"model_pin": "none"}
        )
        self.assertEqual(execution["model"], "gpt-6-astra")


class OldModelPinMigrationTests(unittest.TestCase):
    """run.sh clears the pin that versions before 0.4 shipped as the default, once; a chosen value stays."""

    RUN_SH = (Path(__file__).resolve().parents[1] / "run.sh").read_text(
        encoding="utf-8"
    )

    def test_the_old_default_is_dropped_only_while_run_sh_says_it_cleared_it(self):
        options = {"model_pin": PIN}
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(server, "OPTIONS_PATH", Path(tmpdir) / "options.json"),
        ):
            server.OPTIONS_PATH.write_text(json.dumps(options), encoding="utf-8")
            self.assertEqual(server.model_pin(server.read_options()), PIN)
            with patch.dict(server.os.environ, {"MODEL_PIN_CLEARED": "1"}):
                self.assertEqual(server.model_pin(server.read_options()), "")
            server.OPTIONS_PATH.write_text(
                json.dumps({"model_pin": "gpt-6-astra"}), encoding="utf-8"
            )
            with patch.dict(server.os.environ, {"MODEL_PIN_CLEARED": "1"}):
                self.assertEqual(server.model_pin(server.read_options()), "gpt-6-astra")

    def test_run_sh_clears_the_old_default_once_and_logs_it(self):
        self.assertIn('[ ! -e "${pin_marker}" ]', self.RUN_SH)
        self.assertIn('= "gpt-6.1-sol" ]', self.RUN_SH)
        self.assertIn("bashio::addon.option 'model_pin'", self.RUN_SH)
        self.assertIn(
            'bashio::log.info "Cleared the old default model pin', self.RUN_SH
        )
        # The marker is written before the call, so a failure is not retried on every start.
        self.assertLess(
            self.RUN_SH.index(': > "${pin_marker}"'),
            self.RUN_SH.index("bashio::addon.option 'model_pin'"),
        )


if __name__ == "__main__":
    unittest.main()
