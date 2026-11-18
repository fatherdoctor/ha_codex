"""Typed provider-block fixtures from Codex rust-v0.166.0; no live model calls."""

from __future__ import annotations

import copy
import io
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_approvals
from test_server import server
from safety_block import MESSAGE, NEXT_STEP, SUMMARY, SafetyBlockSession, is_safety_block

# The approval harness replaces notify with a mock; these tests need the real one.
REAL_NOTIFY = server.notify


def block_event(method="error", *, code="cyberPolicy", thread="thread", turn="turn"):
    """Use ErrorNotification/TurnError's camelCase fields, not a UI message."""
    error = {"message": "Synthetic provider detail", "codexErrorInfo": code, "additionalDetails": None}
    params = {"threadId": thread}
    if method == "error":
        params.update(turnId=turn, error=error, willRetry=False)
    else:
        params["turn"] = {"id": turn, "status": "failed", "items": [], "error": error}
    return {"method": method, "params": params}


class SignalTests(unittest.TestCase):
    def test_both_pinned_protocol_shapes_match_without_display_text(self):
        for method in ("error", "turn/completed"):
            with self.subTest(method=method):
                self.assertTrue(is_safety_block(block_event(method), "thread", "turn"))

    def test_message_text_and_other_error_codes_do_not_classify_a_block(self):
        for code in (None, "other", "unauthorized", "usageLimitExceeded", "policyCapacityUnavailable"):
            message = block_event(code=code)
            message["params"]["error"]["message"] = "cyberPolicy: This content can't be shown"
            with self.subTest(code=code):
                self.assertFalse(is_safety_block(message, "thread", "turn"))
        for message in ({"type": "turn.failed", "error": {"message": "cyberPolicy"}},
                        {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
                         "item": {"type": "agentMessage", "text": "cyberPolicy"}}}):
            self.assertFalse(is_safety_block(message, "thread", "turn"))

    def test_only_the_current_thread_and_turn_count(self):
        for method in ("error", "turn/completed"):
            for thread, turn in (("other", "turn"), ("thread", "other"), ("", "turn")):
                with self.subTest(method=method, thread=thread, turn=turn):
                    self.assertFalse(is_safety_block(block_event(method, thread=thread, turn=turn), "thread", "turn"))
            self.assertFalse(is_safety_block(block_event(method), "", ""))

    def test_incomplete_notifications_and_successful_turns_do_not_count(self):
        for message in (None, [], {"method": "error", "params": None}):
            self.assertFalse(is_safety_block(message, "thread", "turn"))
        message = block_event()
        del message["params"]["willRetry"]
        self.assertFalse(is_safety_block(message, "thread", "turn"))
        message = block_event("turn/completed")
        message["params"]["turn"]["status"] = "completed"
        self.assertFalse(is_safety_block(message, "thread", "turn"))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.writer = test_approvals.Transport()
        self.finals = []
        self.session = SafetyBlockSession(
            self.writer, lock=threading.RLock(), thread_params={}, turn_params={},
            on_event=lambda event: None, on_approvals=lambda records: None,
            on_final=self.finals.append, is_active=lambda: True, redact=lambda text: text,
        )
        self.session.thread_id, self.session.turn_id = "thread", "turn"

    def test_block_stops_once_and_sends_no_retry(self):
        self.session.receive(block_event())
        self.session.receive(block_event("turn/completed"))
        self.assertTrue(self.session.finished.is_set())
        self.assertTrue(self.session.safety_blocked)
        self.assertEqual(self.session.error, MESSAGE)
        self.assertEqual(self.session.returncode, 1)
        sent = self.writer.messages()
        self.assertEqual([message.get("method") for message in sent], ["turn/interrupt"])
        self.assertEqual(self.finals, [])
        self.assertNotIn("Synthetic provider detail", self.session.error)

    def test_a_block_never_releases_a_partial_final_answer(self):
        self.session._final_text = "An unfinished answer"
        self.session.receive(block_event("turn/completed"))
        self.assertEqual(self.finals, [])
        self.assertTrue(self.session.safety_blocked)

    def test_ordinary_completion_still_uses_the_existing_transport(self):
        self.session._final_text = "Done"
        message = block_event("turn/completed")
        message["params"]["turn"].update(status="completed", error=None)
        self.session.receive(message)
        self.assertEqual(self.finals, ["Done"])
        self.assertEqual(self.session.returncode, 0)
        self.assertFalse(self.session.safety_blocked)

    def test_unrelated_error_does_not_stop_the_exchange(self):
        self.session.receive(block_event(thread="other"))
        self.assertFalse(self.session.finished.is_set())
        self.assertFalse(self.session.safety_blocked)


class WorkerTests(unittest.TestCase):
    def run_blocked_task(self, method):
        """Use the existing bidirectional fake process with a typed block instead of its generic failure."""
        harness = test_approvals.ApprovalRunTests("test_failed_turn_still_reviews_edits_and_recovery")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        process = harness.process(failure="failed")
        original_read = process.stdout.readline

        def read():
            line = original_read()
            if line:
                message = json.loads(line)
                if message.get("method") == "turn/completed":
                    line = json.dumps(block_event(method, thread=message["params"]["threadId"], turn="native-turn")) + "\n"
            return line

        process.stdout.readline = read
        with patch.object(server.subprocess, "Popen", return_value=process) as launch, \
             patch.object(server, "fire_ha_event", return_value=(True, "")) as event, \
             patch.object(server, "notify", REAL_NOTIFY), \
             patch.object(server, "call_ha_service", return_value=(True, "")) as service, \
             patch.object(server, "assess_changes", wraps=server.assess_changes) as review:
            server.run_task("task", "Read history")
        return copy.deepcopy(server.tasks["task"]), launch, event, service, review, process, harness

    def test_notification_block_stops_the_task_and_publishes_one_safe_notice(self):
        self.check_task("error")

    def test_failed_completion_block_has_the_same_owner_visible_result(self):
        self.check_task("turn/completed")

    def check_task(self, method):
        task, launch, event, service, review, process, harness = self.run_blocked_task(method)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["summary"], SUMMARY)
        self.assertEqual(task["error"], MESSAGE)
        self.assertIn(NEXT_STEP, task["details"])
        self.assertFalse(task["question"])
        launch.assert_called_once()
        review.assert_called_once()
        self.assertEqual(sum(message.get("method") == "turn/start" for message in process.stdin.messages()), 1)
        blocks = [call for call in event.call_args_list if call.args[0] == "codex_cli_safety_blocked"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].args[1], {"task_id": "task", "turn_id": "worker-turn"})
        # With the default settings (no notify service) the owner gets one persistent
        # notification for the block, and no second "Codex task failed" one.
        notices = [call.args[1] for call in service.call_args_list if call.args[0] == "persistent_notification.create"]
        self.assertEqual(notices, [{"title": "Codex task blocked by the safety filter",
                                    "message": f"Task task: {MESSAGE}",
                                    "notification_id": "codex_safety_block_task"}])
        self.assertNotIn("Synthetic provider detail", json.dumps(task) + str(service.call_args_list))
        log = (harness.root / "tasks/task/codex.log").read_text()
        self.assertEqual(log.count("No automatic retry."), 1)
        self.assertNotIn("Synthetic provider detail", log)
        self.assertNotIn("task", server.running_processes)
        self.assertNotIn("task", server.approval_sessions)

    def test_failed_event_delivery_still_creates_the_persistent_notification(self):
        with patch.object(server, "write_task_log"), \
             patch.object(server, "fire_ha_event", return_value=(False, "unavailable")), \
             patch.object(server, "read_options", return_value=dict(server.DEFAULT_OPTIONS)), \
             patch.object(server, "call_ha_service", return_value=(True, "")) as service, \
             patch("sys.stdout", io.StringIO()):
            server.publish_safety_block("task", "turn")
        service.assert_called_once()
        self.assertEqual(service.call_args.args[0], "persistent_notification.create")

    def test_a_configured_notify_service_gets_the_block_notice(self):
        options = {**server.DEFAULT_OPTIONS, "notify_service": "notify.example_phone"}
        with patch.object(server, "write_task_log"), \
             patch.object(server, "fire_ha_event", return_value=(True, "")), \
             patch.object(server, "read_options", return_value=options), \
             patch.object(server, "call_ha_service", return_value=(True, "")) as service:
            server.publish_safety_block("task", "turn")
        service.assert_called_once_with("notify.example_phone", {
            "title": "Codex task blocked by the safety filter", "message": f"Task task: {MESSAGE}"})

    def test_module_is_in_the_worker_image(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
        self.assertIn("COPY server.py account_access.py safety_block.py /", dockerfile)


if __name__ == "__main__":
    unittest.main()
