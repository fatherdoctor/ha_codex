"""A Home Assistant notice exists only while a chat waits on the owner.

A failure makes no notice; it shows in the chat. A question or a permission request makes one notice per chat,
under the chat's own id, and the notice is dismissed when the owner answers, the chat is stopped, the chat is
deleted, or the wait ends in another way.
"""

import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server

TASK_ID = "20261117T180000Z-1a2b3c4d"
TURN_ID = "0123456789abcdef0123456789abcdef"
NOTICE_ID = f"codex_cli_wait_{TASK_ID}"


class WaitNoticeTests(unittest.TestCase):
    """Every Home Assistant service call is recorded; nothing reaches a real Home Assistant."""

    def setUp(self):
        """Give each test an empty worker with temporary storage and a stand-in Home Assistant."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("active_task_runners", set()), ("running_processes", {}),
                            ("approval_sessions", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", root / "index.json"))
        self.stack.enter_context(patch.object(server, "read_options", return_value={}))
        self.calls = []
        self.stack.enter_context(patch.object(
            server, "call_ha_service", side_effect=lambda service, data: self.calls.append((service, data)) or (True, "ok"),
        ))
        # Dismissals run on their own thread in the worker; here they run at once, so the order is certain.
        self.stack.enter_context(patch.object(server, "dismiss_wait_notice_async", side_effect=server.dismiss_wait_notice))

    def created(self):
        """The notification ids of the persistent notifications created so far."""
        return [data["notification_id"] for service, data in self.calls if service == "persistent_notification.create"]

    def dismissed(self):
        """The notification ids dismissed so far."""
        return [data["notification_id"] for service, data in self.calls if service == "persistent_notification.dismiss"]

    def waiting_question(self):
        """Record a chat whose current turn waits for the owner's answer, and send its question."""
        server.update_task(TASK_ID, status="waiting_for_input", current_turn_id=TURN_ID, question="Keep both?")
        server.notify_task_outcome(TASK_ID, "needs_input", "", TURN_ID, "Keep both?", ["Yes", "No"])

    def waiting_permission(self):
        """Record a running chat with one pending permission request, and send its notice."""
        approval = {"approval_id": "a1", "kind": "command", "status": "pending", "reason": "List the folder"}
        server.update_task(TASK_ID, status="running", current_turn_id=TURN_ID, approvals=[approval])
        server.notify_permission(TASK_ID, approval, threading.Event())

    def test_a_failed_action_makes_no_notice(self):
        """A failed turn calls no Home Assistant service at all, with or without a notify service."""
        server.update_task(TASK_ID, status="failed", current_turn_id=TURN_ID)
        for options in ({}, {"notify_service": "notify.mobile_app_phone"}):
            with patch.object(server, "read_options", return_value=options):
                server.notify_task_outcome(TASK_ID, "failed", "The entity light.porch does not exist.", TURN_ID)
        self.assertEqual(self.calls, [])

    def test_a_waiting_question_makes_one_notice_under_the_chat_id(self):
        """The question goes out once, as a persistent notification with the chat's own id."""
        self.waiting_question()
        self.assertEqual(self.created(), [NOTICE_ID])

    def test_a_waiting_permission_request_makes_one_notice_under_the_chat_id(self):
        """The permission request uses the same id, so a chat never has two notices."""
        self.waiting_permission()
        self.assertEqual(self.created(), [NOTICE_ID])

    def test_answering_the_question_clears_the_notice(self):
        """The reply starts the next turn, so the chat no longer waits and its notice is dismissed."""
        self.waiting_question()
        server.update_task(TASK_ID, status="running", question="")
        self.assertEqual(self.dismissed(), [NOTICE_ID])

    def test_deciding_the_permission_request_clears_the_notice(self):
        """Once the last pending request is decided, the notice is dismissed."""
        self.waiting_permission()
        server.update_task(TASK_ID, approvals=[{"approval_id": "a1", "kind": "command", "status": "approved"}])
        self.assertEqual(self.dismissed(), [NOTICE_ID])

    def test_one_of_two_requests_decided_keeps_the_notice(self):
        """While one request still waits, the notice stays."""
        self.waiting_permission()
        server.update_task(TASK_ID, approvals=[
            {"approval_id": "a1", "kind": "command", "status": "approved"},
            {"approval_id": "a2", "kind": "command", "status": "pending"},
        ])
        self.assertEqual(self.dismissed(), [])

    def test_stopping_the_chat_clears_the_notice(self):
        """Stopping a chat that waits for a permission decision dismisses its notice."""
        self.waiting_permission()
        accepted, _process, error = server.request_task_cancellation(TASK_ID)
        self.assertTrue(accepted, error)
        self.assertEqual(self.dismissed(), [NOTICE_ID])

    def test_stopping_a_chat_that_did_not_wait_dismisses_nothing(self):
        """A running chat with nothing pending has no notice to dismiss."""
        server.update_task(TASK_ID, status="running", current_turn_id=TURN_ID)
        accepted, _process, error = server.request_task_cancellation(TASK_ID)
        self.assertTrue(accepted, error)
        self.assertEqual(self.dismissed(), [])

    def test_deleting_a_waiting_chat_clears_the_notice(self):
        """A chat that waits for an answer can be deleted; its notice goes with it."""
        self.waiting_question()
        with (patch.object(server, "api_token", return_value="test-token"),
              patch.object(server, "remove_session_files")):
            response = server.app.test_client().delete(
                f"/tasks/{TASK_ID}", headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.dismissed(), [NOTICE_ID])

    def test_a_restart_ends_a_permission_wait_and_clears_the_notice(self):
        """After a restart the pending request has expired, so its notice is dismissed; a question keeps its notice."""
        permission = {"status": "running", "approvals": [{"approval_id": "a1", "status": "pending"}]}
        server.restore_saved_task(TASK_ID, permission)
        server.restore_saved_task("other-chat", {"status": "waiting_for_input"})
        self.assertEqual(permission["status"], "failed")
        self.assertEqual(self.dismissed(), [NOTICE_ID])

    def test_a_question_answered_before_its_notice_is_sent_makes_none(self):
        """The notice checks that the chat still waits, so a late notice cannot outlive the wait."""
        server.update_task(TASK_ID, status="running", current_turn_id=TURN_ID)
        for options in ({}, {"notify_service": "notify.mobile_app_phone"}):
            with patch.object(server, "read_options", return_value=options):
                server.notify_question(TASK_ID, TURN_ID, "Keep both?", ["Yes", "No"])
        self.assertEqual(self.calls, [])

    def test_a_dismissal_waits_for_a_notice_being_sent(self):
        """The dismissal takes the notification lock, so it cannot run between the check and the send."""
        self.waiting_question()
        with server.notification_lock:
            dismissal = threading.Thread(target=server.dismiss_wait_notice, args=(TASK_ID,))
            dismissal.start()
            dismissal.join(0.2)
            self.assertTrue(dismissal.is_alive())
            self.assertEqual(self.dismissed(), [])
        dismissal.join(5)
        self.assertEqual(self.dismissed(), [NOTICE_ID])

    def test_waits_on_owner(self):
        """A question or a pending request waits; a finished chat never does, whatever its saved requests say."""
        pending = [{"status": "pending"}]
        self.assertTrue(server.task_waits_on_owner({"status": "waiting_for_input"}))
        self.assertTrue(server.task_waits_on_owner({"status": "running", "approvals": pending}))
        self.assertFalse(server.task_waits_on_owner({"status": "running", "approvals": [{"status": "approved"}]}))
        for status in ("completed", "failed", "cancelled"):
            self.assertFalse(server.task_waits_on_owner({"status": status, "approvals": pending}))
        self.assertFalse(server.task_waits_on_owner(None))


if __name__ == "__main__":
    unittest.main()
