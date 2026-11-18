"""Share: a chat downloaded as Markdown, whole or its last lines, with the steps Codex printed."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_activity import command, item
from test_server import server


class ShareChatTests(unittest.TestCase):
    """GET /tasks/<task_id>/share."""

    def setUp(self):
        """Isolate task storage in a temp directory and mock the task runner."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("task_activity", {}), ("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}

    def share(self, task_id, query=""):
        """Request the shared file of a chat."""
        return self.client.get(f"/tasks/{task_id}/share{query}", headers=self.headers)

    def finished_chat(self):
        """Create a chat with one finished exchange: a thinking line, a failed command, and an answer."""
        response = self.client.post("/tasks", json={"prompt": "Why is the porch light on?"}, headers=self.headers)
        task_id = response.json["task_id"]
        turn_id = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        server.record_activity_event(task_id, item("reasoning", "r0", text="Reading the automations\nthen the logbook"))
        server.record_activity_event(task_id, command("c1", "/bin/sh -lc 'grep porch'", exit_code=1, status="failed"))
        server.update_task(task_id, status="completed", summary="A sunset automation turned it on.",
                           completed_at=server.utc_now())
        server.finish_activity(task_id)
        server.active_task_runners.discard(task_id)
        return task_id

    def test_whole_chat_has_message_steps_and_answer(self):
        """The whole chat holds the message, each printed step with its mark, and the answer, as a download."""
        task_id = self.finished_chat()
        response = self.share(task_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/markdown")
        self.assertEqual(response.headers["Content-Disposition"], f'attachment; filename="codex-chat-{task_id}.md"')
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        text = response.get_data(as_text=True)
        self.assertIn("Why is the porch light on?", text)
        self.assertIn("### Steps\n\n✦ Reading the automations\n  then the logbook\n› grep porch (exit 1)", text)
        self.assertIn("## Codex\n\nA sunset automation turned it on.", text)
        self.assertLess(text.index("✦ Reading"), text.index("## Codex"))

    def test_steps_of_every_exchange_and_of_the_running_one(self):
        """An earlier exchange keeps its saved steps, and a running exchange gives the steps so far."""
        task_id = self.finished_chat()
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        (session_dir / f"rollout-2026-09-20T01-00-00-{session_id}.jsonl").write_text("{}\n")
        server.tasks[task_id]["session_id"] = session_id
        response = self.client.post(f"/tasks/{task_id}/continue", json={"message": "And the garage?"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        turn_id = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        server.record_activity_event(task_id, item("reasoning", "r9", text="Checking the garage"))
        text = self.share(task_id).get_data(as_text=True)
        self.assertIn("› grep porch (exit 1)", text)
        self.assertIn("And the garage?\n\n### Steps\n\n✦ Checking the garage", text)

    def test_last_lines(self):
        """lines keeps only the end of the file; a bad number is refused."""
        task_id = self.finished_chat()
        whole = self.share(task_id).get_data(as_text=True).rstrip("\n").split("\n")
        last = self.share(task_id, "?lines=3").get_data(as_text=True)
        self.assertEqual(last, "\n".join(whole[-3:]) + "\n")
        self.assertEqual(self.share(task_id, f"?lines={len(whole) + 50}").get_data(as_text=True).rstrip("\n").split("\n"), whole)
        for bad in ("0", "-2", "x", "100001", "1.5"):
            self.assertEqual(self.share(task_id, f"?lines={bad}").status_code, 400, bad)

    def test_needs_the_token_and_a_known_chat(self):
        """The file is behind the worker's sign-in, like the chat itself, and an unknown chat is not found."""
        task_id = self.finished_chat()
        self.assertEqual(self.client.get(f"/tasks/{task_id}/share").status_code, 401)
        self.assertEqual(self.share("missing").status_code, 404)
        self.assertEqual(self.share("..%2Fx").status_code, 404)


if __name__ == "__main__":
    unittest.main()
