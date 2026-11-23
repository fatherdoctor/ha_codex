"""Every way run_task ends, and what each one leaves: the task's state, its log, its result event and its notice.

These tests describe run_task as it is, so that it can be split into smaller functions without a change in
behavior: a split that changes what one of these exits leaves fails here. They run whole exchanges against the
stand-in app server, with the temporary config tree of test_backups.RunTests.
"""
from __future__ import annotations

import functools
import json
import unittest
from unittest.mock import patch

from app_server_fixture import StubAppServer
import test_backups
from test_server import server

SESSION = "019fc242-910a-7c92-a17d-54c014e19fc4"
ANSWER = {"status": "completed", "summary": "Renamed", "question": "", "details": "Notes"}
# The real wait loop, checked often, so a one-second timeout or stall ends in about a second.
QUICK_WAIT = functools.partial(server.wait_for_exchange, poll=0.05)


class DiesBeforeTheTurn(StubAppServer):
    """A Codex that ends its output when the worker sends initialize, before any turn starts."""

    def write(self, line: str) -> int:
        self.requests.append(json.loads(line))
        self.close()
        return len(line)


class RunTaskExitTests(unittest.TestCase):
    """One test for each exit of run_task."""

    setUp = test_backups.RunTests.setUp

    def run_exchange(self, process=None, *, message="Rename the automation"):
        """Run the task with this stand-in as Codex; return the Popen mock, to see whether Codex started."""
        with patch.object(server.subprocess, "Popen", return_value=process) as launch, \
             patch.object(server, "wait_for_exchange", QUICK_WAIT):
            server.run_task(self.task_id, message)
        return launch

    def task(self):
        """The task as run_task left it."""
        with server.lock:
            return dict(server.tasks[self.task_id])

    def log(self):
        """The text of the task's codex.log, or "" when nothing was written to it."""
        path = self.config / "codex_tasks" / self.task_id / "codex.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def result_events(self):
        """The codex_cli_task_result events fired for the task (setUp records every event's data)."""
        return [event for event in self.events if event.get("task_id") == self.task_id and "response" in event]

    def assert_left_no_process(self):
        """Every exit lets go of the process and the permission session of the exchange."""
        self.assertNotIn(self.task_id, server.running_processes)
        self.assertNotIn(self.task_id, server.approval_sessions)

    def assert_finished_failed(self, summary):
        """A failed exchange: the state, the one result event, and no notice (the owner reads it in the chat)."""
        task = self.task()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["summary"], summary)
        self.assertTrue(task["completed_at"])
        self.assertTrue(task["error"])
        self.assertEqual(task["question"], "")
        events = self.result_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["summary"], summary)
        server.notify.assert_not_called()
        self.assert_left_no_process()
        return task

    # -- done -------------------------------------------------------------------------------------------------

    def test_done_records_the_answer_and_the_changes_and_sends_no_notice(self):
        def edit():
            (self.config / "scripts.yaml").write_text("a: 2\n", encoding="utf-8")

        launch = self.run_exchange(StubAppServer(SESSION, ANSWER, on_turn=edit))
        launch.assert_called_once()
        task = self.task()
        self.assertEqual(task["status"], "completed")
        self.assertEqual((task["summary"], task["details"], task["error"]), ("Renamed", "Notes", ""))
        self.assertEqual(task["returncode"], 0)
        self.assertEqual(task["session_id"], SESSION)
        self.assertTrue(task["completed_at"])
        self.assertEqual(task["changes"], {"added": [], "changed": ["scripts.yaml"], "deleted": []})
        self.assertTrue((self.run_dir / "changes.json").exists())
        self.assertIn("worker: Starting Codex: ", self.log())
        events = self.result_events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["status"], events[0]["codex_status"]), ("completed", "completed"))
        self.assertEqual(events[0]["turn_id"], self.turn_id)
        server.notify.assert_not_called()
        self.assert_left_no_process()

    def test_a_question_waits_for_the_owner_and_is_the_one_exit_with_a_notice(self):
        question = {"status": "needs_input", "summary": "Which one?", "question": "Rename which automation?",
                    "details": "", "choices": ["Kitchen", "Hall"]}
        self.run_exchange(StubAppServer(SESSION, question))
        task = self.task()
        self.assertEqual(task["status"], "waiting_for_input")
        self.assertEqual(task["completed_at"], "")
        self.assertEqual(task["question"], "Rename which automation?")
        self.assertEqual(task["error"], "")
        self.assertEqual(self.result_events()[0]["codex_status"], "needs_input")
        server.notify.assert_called_once()
        # The notice carries the question and every choice, so the owner can answer from it.
        notice = " ".join(str(part) for part in server.notify.call_args.args)
        for text in ("Rename which automation?", "Kitchen", "Hall"):
            self.assertIn(text, notice)
        self.assert_left_no_process()

    # -- failed -----------------------------------------------------------------------------------------------

    def test_a_failure_codex_reports_is_failed_with_its_summary(self):
        failed = {"status": "failed", "summary": "The file is locked", "question": "", "details": "Try later"}
        self.run_exchange(StubAppServer(SESSION, failed))
        task = self.assert_finished_failed("The file is locked")
        self.assertEqual(task["returncode"], 0)
        self.assertTrue((self.run_dir / "changes.json").exists())

    def test_a_turn_that_ends_in_error_is_failed_and_still_reviewed(self):
        def edit():
            (self.config / "scripts.yaml").write_text("a: 3\n", encoding="utf-8")

        self.run_exchange(StubAppServer(SESSION, None, on_turn=edit, turn_error="Invented upstream failure"))
        task = self.assert_finished_failed("The Codex exchange could not finish.")
        self.assertEqual(task["returncode"], 1)
        self.assertIn("Invented upstream failure", task["error"])
        # A started turn may have changed files: the review still runs and lists them.
        self.assertEqual(task["changes"]["changed"], ["scripts.yaml"])
        self.assertIn("worker: Codex permission connection failed: ", self.log())

    def test_codex_that_cannot_start_is_failed_with_no_review(self):
        with patch.object(server.subprocess, "Popen", side_effect=OSError("invented: no such file")), \
             patch.object(server, "wait_for_exchange", QUICK_WAIT):
            server.run_task(self.task_id, "Rename the automation")
        task = self.assert_finished_failed("Could not start the Codex CLI executable.")
        self.assertIsNone(task["returncode"])
        self.assertIn("invented: no such file", task["details"])
        self.assertIn("invented: no such file", self.log())
        self.assertFalse((self.run_dir / "changes.json").exists())

    def test_a_final_answer_file_that_cannot_be_reset_is_failed_before_codex_starts(self):
        # A folder where the answer file goes: removing the old answer fails.
        (self.run_dir / "final.json").mkdir(parents=True)
        launch = self.run_exchange(StubAppServer(SESSION, ANSWER))
        launch.assert_not_called()
        task = self.assert_finished_failed("Could not prepare the Codex task output.")
        self.assertIn("Could not reset the Codex final response: ", task["details"])
        self.assertIn("worker: Could not reset the Codex final response: ", self.log())
        self.assertFalse((self.run_dir / "changes.json").exists())

    def test_a_prompt_file_that_cannot_be_written_is_failed_before_codex_starts(self):
        # A folder where the prompt file goes: writing the message fails.
        (self.run_dir / "prompt.txt").mkdir(parents=True)
        launch = self.run_exchange(StubAppServer(SESSION, ANSWER))
        launch.assert_not_called()
        task = self.assert_finished_failed("Could not prepare the Codex task input.")
        self.assertIn("Could not write the Codex task prompt: ", task["details"])
        self.assertIn("worker: Could not write the Codex task prompt: ", self.log())
        self.assertFalse((self.run_dir / "changes.json").exists())

    def test_an_exchange_that_fails_before_the_turn_is_failed_with_no_review(self):
        process = DiesBeforeTheTurn(SESSION, ANSWER)
        launch = self.run_exchange(process)
        launch.assert_called_once()
        self.assertEqual([request.get("method") for request in process.requests], ["initialize"])
        task = self.assert_finished_failed("The Codex exchange could not finish.")
        self.assertIsNone(task["returncode"])
        self.assertIn("Codex permission connection failed: ", task["details"])
        self.assertIn("worker: Codex permission connection failed: ", self.log())
        # No turn ran, so nothing could have changed: no review of changes.
        self.assertFalse((self.run_dir / "changes.json").exists())

    # -- canceled ---------------------------------------------------------------------------------------------

    def test_a_cancel_during_the_turn_leaves_the_cancelled_state_untouched(self):
        def edit_then_cancel():
            (self.config / "scripts.yaml").write_text("a: 2\n", encoding="utf-8")
            accepted, _proc, _error = server.request_task_cancellation(self.task_id)
            self.assertTrue(accepted)

        self.run_exchange(StubAppServer(SESSION, ANSWER, on_turn=edit_then_cancel))
        task = self.task()
        # The cancel route owns the result: run_task neither overwrites it nor fires its own event or notice.
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["summary"], server.CANCELLED_TASK_SUMMARY)
        # The cancel route wrote an empty list of changes; run_task did not replace it with its review.
        self.assertEqual(task["changes"], {"added": [], "changed": [], "deleted": []})
        self.assertEqual(self.result_events(), [])
        self.assertFalse((self.run_dir / "changes.json").exists())
        server.notify.assert_not_called()
        self.assert_left_no_process()

    def test_a_cancel_during_the_review_leaves_the_cancelled_state_and_no_result(self):
        real_assess = server.assess_changes

        def cancel_then_assess(*args, **kwargs):
            self.assertTrue(server.request_task_cancellation(self.task_id)[0])
            return real_assess(*args, **kwargs)

        with patch.object(server, "assess_changes", side_effect=cancel_then_assess):
            self.run_exchange(StubAppServer(SESSION, ANSWER))
        task = self.task()
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["summary"], server.CANCELLED_TASK_SUMMARY)
        # The list of changes was written before the review began; the cancel stops the run after it.
        self.assertTrue((self.run_dir / "changes.json").exists())
        self.assertEqual(self.result_events(), [])
        server.notify.assert_not_called()
        self.assert_left_no_process()

    def test_a_cancel_after_the_last_check_is_kept_by_the_refused_final_update(self):
        # The image collection runs after the last cancel check: only the refused final update stops the run.
        def cancel_then_collect(*_args):
            self.assertTrue(server.request_task_cancellation(self.task_id)[0])
            return []

        with patch.object(server, "collect_generated_images", side_effect=cancel_then_collect):
            self.run_exchange(StubAppServer(SESSION, ANSWER))
        self.assertEqual(self.task()["status"], "cancelled")
        self.assertEqual(self.result_events(), [])
        server.notify.assert_not_called()
        self.assert_left_no_process()

    def test_a_task_cancelled_before_it_starts_is_not_run(self):
        self.assertTrue(server.request_task_cancellation(self.task_id)[0])
        launch = self.run_exchange(StubAppServer(SESSION, ANSWER))
        launch.assert_not_called()
        self.assertEqual(self.task()["status"], "cancelled")
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.log(), "")
        self.assertEqual(self.result_events(), [])

    def test_a_cancel_while_codex_starts_stops_the_new_process(self):
        process = StubAppServer(SESSION, ANSWER)

        def launch_then_cancel(*_args, **_kwargs):
            self.assertTrue(server.request_task_cancellation(self.task_id)[0])
            return process

        with patch.object(server.subprocess, "Popen", side_effect=launch_then_cancel), \
             patch.object(server, "wait_for_exchange", QUICK_WAIT):
            server.run_task(self.task_id, "Rename the automation")
        self.assertEqual(process.returncode, -15)
        self.assertEqual(process.requests, [])
        self.assertEqual(self.task()["status"], "cancelled")
        self.assertEqual(self.result_events(), [])
        self.assert_left_no_process()

    # -- timeout and stalled ----------------------------------------------------------------------------------

    def test_a_timeout_stops_the_turn_and_is_failed(self):
        self.options.update(task_timeout_seconds=1, task_stall_seconds=0)
        process = StubAppServer(SESSION, ANSWER, hold=True)
        self.run_exchange(process)
        task = self.assert_finished_failed("Codex timed out after 1 seconds.")
        self.assertEqual(task["error"], "Codex timed out after 1 seconds.")
        self.assertTrue(process.closed)
        self.assertTrue((self.run_dir / "changes.json").exists())

    def test_a_stall_stops_the_turn_and_is_failed_with_plain_words(self):
        self.options.update(task_timeout_seconds=30, task_stall_seconds=1)
        process = StubAppServer(SESSION, ANSWER, hold=True)
        self.run_exchange(process)
        task = self.assert_finished_failed(server.stall_summary(1))
        self.assertEqual(task["error"], server.stall_summary(1))
        self.assertTrue(process.closed)
        self.assertIn("worker: No activity from Codex for 1 seconds; stopping the task.", self.log())

    # -- refused ----------------------------------------------------------------------------------------------

    def test_a_mode_the_policy_refuses_stops_before_the_task_runs(self):
        self.options["codex_sandbox"] = "danger-full-access"
        launch = self.run_exchange(StubAppServer(SESSION, ANSWER))
        launch.assert_not_called()
        task = self.assert_finished_failed(
            "Codex needs support: the sandbox it must run in is not available, so this task was not started.")
        self.assertIn("Codex sandbox preflight failed: ", task["details"])
        self.assertNotIn("started_at", task)
        # Refused before any baseline: no folder for the turn.
        self.assertFalse(self.run_dir.exists())
        self.assertIn("Codex sandbox preflight failed: ", self.log())

    def test_a_sandbox_that_is_not_ready_stops_before_codex_starts(self):
        with patch.object(server, "sandbox_readiness",
                          return_value={"required": True, "ready": False, "message": "invented: no namespaces"}):
            launch = self.run_exchange(StubAppServer(SESSION, ANSWER))
        launch.assert_not_called()
        task = self.assert_finished_failed(
            "Codex needs support: the sandbox it must run in is not available, so this task was not started.")
        self.assertIn("invented: no namespaces", task["details"])
        self.assertTrue(task["started_at"])

    def test_a_model_the_pin_refuses_stops_before_codex_starts(self):
        # A turn saved on one model, then the owner pins another before it runs.
        self.options["model_pin"] = "invented-pinned-model"
        server.tasks[self.task_id]["turns"][-1]["execution_settings"] = {"model": "invented-other-model"}
        launch = self.run_exchange(StubAppServer(SESSION, ANSWER))
        launch.assert_not_called()
        task = self.assert_finished_failed("Stopped: this run asked for a model the assistant is not set up to use.")
        self.assertIn("Model pin: ", task["details"])
        self.assertIn("invented-other-model", task["details"])


if __name__ == "__main__":
    unittest.main()
