"""A Home Assistant notice exists only while a chat waits on the owner.

A failure makes no notice; it shows in the chat. The one exception is a block by OpenAI's safety filter, which
makes one notice of its own (see test_safety_block.py). A question or a permission request makes one notice per chat,
under the chat's own id, and the notice is dismissed when the owner answers, the chat is stopped, the chat is
deleted, or the wait ends in another way.
"""

import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import dismiss_wait_notice_async, server
import ha_client

TASK_ID = "20261117T180000Z-1a2b3c4d"
TURN_ID = "0123456789abcdef0123456789abcdef"
NEXT_TURN_ID = "fedcba9876543210fedcba9876543210"
PHONE = "notify.mobile_app_phone"
NOTICE_ID = f"codex_cli_wait_{TASK_ID}"


class WaitNoticeTests(unittest.TestCase):
    """Every Home Assistant service call is recorded; nothing reaches a real Home Assistant."""

    def setUp(self):
        """Give each test an empty worker with temporary storage and a stand-in Home Assistant."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(server.tasks, clear=True))
        for name, value in (("active_task_runners", set()), ("running_processes", {}),
                            ("approval_sessions", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", root / "index.json"))
        self.stack.enter_context(patch.object(server, "read_options", return_value={}))
        self.calls = []
        self.patch_service(lambda service, data: self.calls.append((service, data)) or (True, "ok"))
        # Dismissals run on their own thread in the worker; here they run at once, so the order is certain.
        self.stack.enter_context(patch.object(server, "dismiss_wait_notice_async", side_effect=server.dismiss_wait_notice))

    def patch_service(self, side_effect):
        """Answer service calls with side_effect, both the worker's own and those of the notice helpers in ha_client."""
        for module in (server, ha_client):
            self.stack.enter_context(patch.object(module, "call_ha_service", side_effect=side_effect))

    def created(self):
        """The notification ids of the persistent notifications created so far."""
        return [data["notification_id"] for service, data in self.calls if service == "persistent_notification.create"]

    def dismissed(self):
        """The notification ids dismissed so far."""
        return [data["notification_id"] for service, data in self.calls if service == "persistent_notification.dismiss"]

    def pushes(self):
        """The data of every call to the phone's notify service so far."""
        return [data for service, data in self.calls if service == PHONE]

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

    def test_only_a_safety_block_makes_a_notice_for_a_failed_chat(self):
        """A chat that fails for another reason sends nothing; a safety block sends exactly one notice.

        The safety-block notice is the one exception to the rule above. It has its own id and stays until the
        owner dismisses it, so no wait ends it.
        """
        server.update_task(TASK_ID, status="failed", current_turn_id=TURN_ID)
        server.notify_task_outcome(TASK_ID, "failed", "The entity light.porch does not exist.", TURN_ID)
        self.assertEqual(self.calls, [])
        with patch.object(server, "fire_ha_event", return_value=(True, "ok")), \
                patch.object(server, "write_task_log"):
            server.publish_safety_block(TASK_ID, TURN_ID)
        self.assertEqual(self.created(), [f"codex_safety_block_{TASK_ID}"])
        self.assertEqual(len(self.calls), 1)

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
            server.tasks[TASK_ID]["status"] = "running"  # The owner answers while the notice is on its way.
        dismissal.join(5)
        self.assertEqual(self.dismissed(), [NOTICE_ID])

    def test_the_dismissal_runs_on_its_own_thread(self):
        """The worker's dismissal starts a thread that dismisses the notice; the caller does not wait for it."""
        done = threading.Event()
        self.patch_service(lambda service, data: done.set() or (True, "ok"))
        dismiss_wait_notice_async(TASK_ID)
        self.assertTrue(done.wait(5))

    def test_a_late_dismissal_keeps_the_notice_of_a_chat_that_waits_again(self):
        """A dismissal that runs after the chat asks its next question leaves the new notice in place."""
        late = []
        self.stack.enter_context(patch.object(server, "dismiss_wait_notice_async", side_effect=late.append))
        self.waiting_question()
        server.update_task(TASK_ID, status="running", question="")
        server.update_task(TASK_ID, status="waiting_for_input", current_turn_id=NEXT_TURN_ID, question="And the other?")
        server.notify_question(TASK_ID, NEXT_TURN_ID, "And the other?", [])
        self.assertEqual(late, [TASK_ID])
        server.dismiss_wait_notice(TASK_ID)
        self.assertEqual(self.created(), [NOTICE_ID, NOTICE_ID])
        self.assertEqual(self.dismissed(), [])

    def test_a_question_answered_while_its_push_fails_makes_no_notice(self):
        """The question checks again after a push that failed, before it falls back to a persistent notification."""
        def push_fails_after_the_answer(service, data):
            """The owner answers in the chat while the push to the phone is on its way, and the push fails."""
            self.calls.append((service, data))
            server.tasks[TASK_ID]["status"] = "running"
            return False, "timeout"

        server.update_task(TASK_ID, status="waiting_for_input", current_turn_id=TURN_ID, question="Keep both?")
        self.patch_service(push_fails_after_the_answer)
        for choices in (["Yes", "No"], []):
            server.tasks[TASK_ID]["status"] = "waiting_for_input"
            with patch.object(server, "read_options", return_value={"notify_service": PHONE}):
                server.notify_question(TASK_ID, TURN_ID, "Keep both?", choices)
        self.assertEqual(len(self.pushes()), 2)
        self.assertEqual(self.created(), [])

    def test_a_question_of_an_older_turn_makes_no_notice(self):
        """A question that a newer turn of the same chat replaced is not sent, though the chat waits again."""
        server.update_task(TASK_ID, status="waiting_for_input", current_turn_id=NEXT_TURN_ID, question="And the other?")
        server.notify_question(TASK_ID, TURN_ID, "Keep both?", ["Yes", "No"])
        self.assertEqual(self.calls, [])

    def test_a_phone_push_is_tagged_and_cleared_when_the_wait_ends(self):
        """Every push about a waiting chat carries the chat's tag, and the dismissal clears it on the phone."""
        self.stack.enter_context(patch.object(server, "read_options", return_value={"notify_service": PHONE}))
        self.waiting_question()
        server.update_task(TASK_ID, status="running", question="")
        self.waiting_permission()
        server.update_task(TASK_ID, status="completed", approvals=[])
        pushes = self.pushes()
        self.assertEqual([push["data"]["tag"] for push in pushes], [NOTICE_ID] * 4)
        self.assertEqual([push["message"] for push in pushes][1::2], ["clear_notification", "clear_notification"])
        self.assertIn("actions", pushes[0]["data"])
        self.assertEqual(self.dismissed(), [NOTICE_ID, NOTICE_ID])

    def test_another_notify_service_gets_no_tag_and_no_clear(self):
        """Only the companion app knows tags; any other notify service gets the plain message, and nothing to clear."""
        self.stack.enter_context(patch.object(server, "read_options", return_value={"notify_service": "notify.family"}))
        self.waiting_permission()
        server.update_task(TASK_ID, status="completed", approvals=[])
        sent = [data for service, data in self.calls if service == "notify.family"]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("data", sent[0])
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
