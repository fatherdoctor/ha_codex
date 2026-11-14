"""A sign-in OpenAI refused reads as signed out, and a silent exchange is stopped by the stall watchdog."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from test_server import server

# Error texts seen on a real revoked sign-in (worker log and task error), and refresh-token codes.
REJECTED = [
    "workspace routing discovery unauthorized (401)",
    'refresh failed: {"error": {"code": "token_revoked"}}',
    "Encountered invalidated oauth token for user",
    "refresh_token_reused",
]
# Failures that are not about the sign-in: the network, a timeout, a line number that happens to be 401.
NOT_REJECTED = [
    "unexpected status 401 Unauthorized: Your session has ended",
    "stream disconnected before completion: error sending request",
    "Codex did not answer the usage read in time",
    "automations.yaml line 401: unknown key",
    "",
    None,
]


class SignInRejectedTests(unittest.TestCase):
    """Which error texts mean OpenAI refused the sign-in."""

    def test_markers(self) -> None:
        for text in REJECTED:
            with self.subTest(text=text):
                self.assertTrue(server.sign_in_rejected(text))
        for text in NOT_REJECTED:
            with self.subTest(text=text):
                self.assertFalse(server.sign_in_rejected(text))


class SignInStateTests(unittest.TestCase):
    """OpenAI's last answer overrules the local `codex login status`, until auth.json changes."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        (self.home / "auth.json").write_text('{"tokens": "placeholder"}', encoding="utf-8")
        stack = [
            patch.object(server, "CODEX_HOME", self.home),
            patch.object(server, "codex_local_login_status",
                         side_effect=lambda: {"has_auth_file": True, "status_ok": True, "message": "Logged in using ChatGPT"}),
            patch.dict(server.sign_in_check, {"result": "", "auth_signature": None, "checked_at": ""}),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    def test_unchecked_keeps_the_local_answer(self) -> None:
        status = server.codex_login_status()
        self.assertTrue(status["status_ok"])
        self.assertEqual(status["server_check"], "unchecked")

    def test_rejected_reads_as_signed_out(self) -> None:
        server.record_sign_in_check("rejected")
        status = server.codex_login_status()
        self.assertFalse(status["status_ok"])
        self.assertEqual(status["server_check"], "rejected")
        self.assertEqual(status["message"], server.SIGN_IN_EXPIRED_MESSAGE)
        self.assertTrue(status["server_checked_at"])

    def test_a_new_sign_in_clears_the_refusal(self) -> None:
        server.record_sign_in_check("rejected")
        (self.home / "auth.json").write_text('{"tokens": "a newer placeholder"}', encoding="utf-8")
        status = server.codex_login_status()
        self.assertTrue(status["status_ok"])
        self.assertEqual(status["server_check"], "unchecked")

    def test_sign_out_clears_it_too(self) -> None:
        server.record_sign_in_check("rejected")
        (self.home / "auth.json").unlink()
        self.assertEqual(server.sign_in_server_state()["result"], "unchecked")

    def test_accepted_is_reported(self) -> None:
        server.record_sign_in_check("ok")
        status = server.codex_login_status()
        self.assertTrue(status["status_ok"])
        self.assertEqual(status["server_check"], "ok")

    def test_usage_read_refused_marks_the_sign_in(self) -> None:
        """A usage read OpenAI refuses for the sign-in marks it, with no estimated fallback claimed."""
        refused = RuntimeError("unexpected status 401 Unauthorized: token_revoked")
        with (patch.object(server, "active_task_id", return_value=None),
              patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
              patch.object(server, "read_codex_rate_limits", side_effect=refused),
              patch.object(server, "fetch_codex_usage_status",
                           return_value={"status": "unavailable", "error": "Not logged in"})):
            usage = server.fetch_codex_usage()
        self.assertEqual(server.sign_in_server_state()["result"], "rejected")
        self.assertEqual((usage["status"], usage["note"]), ("unavailable", ""))
        self.assertEqual(usage["rate_limits_error_category"], "sign_in_rejected")
        self.assertEqual(usage["rate_limits_error"], server.USAGE_FAILURE_MESSAGES["sign_in_rejected"])

    def test_usage_read_success_marks_it_accepted(self) -> None:
        with (patch.object(server, "active_task_id", return_value=None),
              patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
              patch.object(server, "read_codex_rate_limits", return_value={"rateLimits": {}}),
              patch.object(server, "fetch_codex_usage_status", return_value={"status": "ok", "error": ""})):
            server.fetch_codex_usage()
        self.assertEqual(server.sign_in_server_state()["result"], "ok")

    def test_a_network_failure_does_not_sign_out(self) -> None:
        with (patch.object(server, "active_task_id", return_value=None),
              patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
              patch.object(server, "read_codex_rate_limits", side_effect=TimeoutError("slow")),
              patch.object(server, "fetch_codex_usage_status", return_value={"status": "ok", "error": ""})):
            server.fetch_codex_usage()
        self.assertEqual(server.sign_in_server_state()["result"], "unchecked")
        self.assertTrue(server.codex_login_status()["status_ok"])

    def test_task_failure_on_the_sign_in_marks_it(self) -> None:
        """The task's own log is read, since the HTTP error lands on Codex's stderr."""
        tasks = tempfile.TemporaryDirectory()
        self.addCleanup(tasks.cleanup)
        with patch.object(server, "get_task_dir", return_value=Path(tasks.name)):
            self.assertFalse(server.task_failed_on_sign_in("t1", "Codex finished without a final answer."))
            self.assertEqual(server.sign_in_server_state()["result"], "unchecked")
            (Path(tasks.name) / "codex.log").write_text(
                "[t] stderr: ERROR workspace routing discovery unauthorized (401)\n", encoding="utf-8")
            self.assertTrue(server.task_failed_on_sign_in("t1", "Codex finished without a final answer."))
        self.assertEqual(server.sign_in_server_state()["result"], "rejected")

    def test_command_output_in_the_task_log_is_not_a_sign_in_failure(self) -> None:
        """A 401 that a command printed reaches the log as a stdout event, not as Codex's own error."""
        tasks = tempfile.TemporaryDirectory()
        self.addCleanup(tasks.cleanup)
        event = {"type": "item.completed", "item": {"type": "commandExecution",
                                                     "aggregated_output": "401: Unauthorized"}}
        (Path(tasks.name) / "codex.log").write_text(
            f"[t] stdout: {json.dumps(event)}\n"
            "[t] worker: No activity from Codex for 600 seconds; stopping the task.\n", encoding="utf-8")
        with patch.object(server, "get_task_dir", return_value=Path(tasks.name)):
            self.assertFalse(server.task_failed_on_sign_in("t1", "Codex stopped: no activity."))
        self.assertEqual(server.sign_in_server_state()["result"], "unchecked")

    def test_task_log_keeps_continuation_lines_of_stderr(self) -> None:
        tasks = tempfile.TemporaryDirectory()
        self.addCleanup(tasks.cleanup)
        (Path(tasks.name) / "codex.log").write_text(
            "[t] stdout: {}\n[t] stderr: request failed\n  workspace routing discovery unauthorized (401)\n",
            encoding="utf-8")
        with patch.object(server, "get_task_dir", return_value=Path(tasks.name)):
            self.assertTrue(server.task_failed_on_sign_in("t1"))
        self.assertEqual(server.sign_in_server_state()["result"], "rejected")

    def test_task_refusal_after_a_new_sign_in_is_dropped(self) -> None:
        """A task started with the old sign-in fails after the user signed in again: the new one stands."""
        tasks = tempfile.TemporaryDirectory()
        self.addCleanup(tasks.cleanup)
        (Path(tasks.name) / "codex.log").write_text(
            "[t] stderr: ERROR workspace routing discovery unauthorized (401)\n", encoding="utf-8")
        with patch.object(server, "get_task_dir", return_value=Path(tasks.name)):
            server.note_task_sign_in("t1")
            (self.home / "auth.json").write_text('{"tokens": "a newer placeholder"}', encoding="utf-8")
            self.assertTrue(server.task_failed_on_sign_in("t1"))
        self.assertEqual(server.sign_in_server_state()["result"], "unchecked")
        self.assertTrue(server.codex_login_status()["status_ok"])

    def test_task_refusal_for_the_same_sign_in_is_kept(self) -> None:
        tasks = tempfile.TemporaryDirectory()
        self.addCleanup(tasks.cleanup)
        (Path(tasks.name) / "codex.log").write_text(
            "[t] stderr: ERROR workspace routing discovery unauthorized (401)\n", encoding="utf-8")
        with patch.object(server, "get_task_dir", return_value=Path(tasks.name)):
            server.note_task_sign_in("t1")
            self.assertTrue(server.task_failed_on_sign_in("t1"))
        self.assertEqual(server.sign_in_server_state()["result"], "rejected")

    def test_usage_answer_after_a_new_sign_in_is_dropped(self) -> None:
        def read(_codex):
            (self.home / "auth.json").write_text('{"tokens": "a newer placeholder"}', encoding="utf-8")
            raise RuntimeError("unexpected status 401 Unauthorized")

        with (patch.object(server, "active_task_id", return_value=None),
              patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
              patch.object(server, "read_codex_rate_limits", side_effect=read),
              patch.object(server, "fetch_codex_usage_status", return_value={"status": "ok", "error": ""})):
            usage = server.fetch_codex_usage()
        self.assertEqual(server.sign_in_server_state()["result"], "unchecked")
        self.assertEqual(usage["source"], "status_screen")

    def test_auth_status_page_says_expired(self) -> None:
        server.record_sign_in_check("rejected")
        with patch.dict(server.auth_state, {"status": "authenticated", "message": "Logged in using ChatGPT"}):
            payload = server.auth_status_payload()
        self.assertEqual(payload["status"], "expired")
        self.assertEqual(payload["message"], server.SIGN_IN_EXPIRED_MESSAGE)
        self.assertFalse(payload["codex_login"]["status_ok"])


class FakeSession:
    """The parts of AppServerSession the watchdog reads."""

    def __init__(self, clock: list[float]) -> None:
        self.finished = threading.Event()
        self.last_activity = clock[0]
        self.pending = False
        self.clock = clock

    def waiting_for_user(self) -> bool:
        return self.pending


class StallWatchdogTests(unittest.TestCase):
    """wait_for_exchange with a fake clock: each poll moves time on by the poll length."""

    def run_wait(self, session: FakeSession, clock: list[float], timeout: float, stall: float, on_poll=None) -> str:
        real_wait = session.finished.wait

        def wait(timeout=None):
            clock[0] += timeout
            if on_poll:
                on_poll(clock[0])
            return real_wait(0)

        session.finished.wait = wait
        return server.wait_for_exchange(session, timeout, stall, poll=5, clock=lambda: clock[0])

    def test_silence_stops_the_exchange(self) -> None:
        clock = [0.0]
        session = FakeSession(clock)
        self.assertEqual(self.run_wait(session, clock, 3600, 600), "stalled")
        self.assertGreaterEqual(clock[0], 600)
        self.assertLess(clock[0], 700)

    def test_activity_keeps_it_going_until_it_finishes(self) -> None:
        clock = [0.0]
        session = FakeSession(clock)

        def poll(now: float) -> None:
            session.last_activity = now  # Codex keeps sending events
            if now >= 2000:
                session.finished.set()

        self.assertEqual(self.run_wait(session, clock, 3600, 600, poll), "finished")

    def test_a_waiting_permission_request_is_not_a_stall(self) -> None:
        clock = [0.0]
        session = FakeSession(clock)
        session.pending = True
        self.assertEqual(self.run_wait(session, clock, 1200, 600), "timeout")

    def test_zero_turns_the_check_off(self) -> None:
        clock = [0.0]
        session = FakeSession(clock)
        self.assertEqual(self.run_wait(session, clock, 900, 0), "timeout")

    def test_summary_is_plain(self) -> None:
        self.assertIn("10 minutes", server.stall_summary(600))
        self.assertIn("1 minute,", server.stall_summary(30))


class SessionActivityTests(unittest.TestCase):
    """The real session moves its activity clock on every message and on every answer."""

    def test_receive_and_decide_move_the_clock(self) -> None:
        from test_approvals import ApprovalTests

        case = ApprovalTests("test_initialize_then_thread_then_turn")
        case.setUp()
        session = case.session
        session.last_activity = 0.0
        approval_id = case.request()
        self.assertGreater(session.last_activity, 0.0)
        self.assertTrue(session.waiting_for_user())
        session.last_activity = 0.0
        session.decide(approval_id, "deny")
        self.assertGreater(session.last_activity, 0.0)
        self.assertFalse(session.waiting_for_user())


class AddonConfigTests(unittest.TestCase):
    """The app starts at boot and offers the stall limit."""

    def test_boot_auto_and_stall_option(self) -> None:
        config = yaml.safe_load((Path(server.__file__).parent / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["boot"], "auto")
        self.assertEqual(config["options"]["task_stall_seconds"], 600)
        self.assertEqual(config["schema"]["task_stall_seconds"], "int(0,86400)")
        self.assertEqual(server.DEFAULT_OPTIONS["task_stall_seconds"], 600)



class TaskSignInRepairTests(unittest.TestCase):
    """Task errors use explicit sign-in signals, including the stderr boundary."""
    setUp = SignInStateTests.setUp

    def test_generic_task_http_errors_leave_the_signin_repair_clear(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'get_task_dir', return_value=Path(folder)):
            for text in ('401 Unauthorized', 'MCP tool: HTTP 401 Unauthorized', 'curl failed: 401 unauthorized'):
                with self.subTest(text=text):
                    (Path(folder) / 'codex.log').write_text('[t] stderr: ' + text + '\n')
                    server.note_task_sign_in('task-http')
                    self.assertFalse(server.task_failed_on_sign_in('task-http', text))
                    self.assertFalse(server.account_access.snapshot(server.codex_login_status())['needs_sign_in'])

    def test_explicit_revocation_in_task_stderr_raises_the_signin_repair(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'get_task_dir', return_value=Path(folder)):
            (Path(folder) / 'codex.log').write_text('[t] stderr: token_revoked\n')
            server.note_task_sign_in('task-revoked')
            self.assertTrue(server.task_failed_on_sign_in('task-revoked', 'Task failed'))
            self.assertTrue(server.account_access.snapshot(server.codex_login_status())['needs_sign_in'])


if __name__ == "__main__":
    unittest.main()
