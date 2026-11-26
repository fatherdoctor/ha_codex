"""Snapshots of what a chat's record looks like to everyone who reads it, for each way an exchange can end.

For a completed answer, a question with choices, a reply that continues the chat, a failed start, a cancel, a timeout
and a chat that waits on a permission request, this
test keeps four things: the codex_cli_task_result event Home Assistant gets (its name and its data), the task as the
API sends it (full and as a list summary), the chat's task.json, and its line in the task index. Times, ids and the
temporary folder are replaced by placeholders, so the snapshot holds only the shape and the values the worker chose.
Turn ids become <turn-1>, <turn-2> and so on in the order they first appear, so a record whose turns share one id, or
swap ids, does not match.

The snapshot is tests/fixtures/task_payload_snapshot.json. A change that moves code must leave it equal. A change that
means to alter one of these records writes it again with CODEX_UPDATE_SNAPSHOTS=1 and shows the difference in its
pull request.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app_server_fixture import StubAppServer
from test_server import server
from approvals import COMMAND_APPROVAL  # noqa: E402 - test_server puts the worker folder on the path

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "task_payload_snapshot.json"
SESSION_ID = "019fc242-910a-7c92-a17d-54c014e19fc4"
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?$")
HEX_ID_RE = re.compile(r"[0-9a-f]{32}")


class TaskPayloadSnapshotTests(unittest.TestCase):
    """The event, the API payload and the saved files of a chat stay as the snapshot says."""

    maxDiff = None

    def setUp(self):
        """Give each test an empty worker with temporary storage and no real runs."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        # The worker's state is emptied in place, so the code under test and this test see the same objects.
        self.stack.enter_context(patch.dict(server.tasks, clear=True))
        saved_runners = set(server.active_task_runners)
        server.active_task_runners.clear()
        self.addCleanup(lambda: (server.active_task_runners.clear(), server.active_task_runners.update(saved_runners)))
        self.stack.enter_context(patch.object(server, "running_processes", {}))
        self.stack.enter_context(patch.object(server, "message_queue", []))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "MESSAGE_QUEUE_FILE", self.root / "queue.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "CONFIG_ROOT", self.root / "config"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "notify_question"))
        self.stack.enter_context(patch.object(server, "write_task_log"))
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.events: list[dict] = []
        self.stack.enter_context(patch.object(
            server, "fire_ha_event",
            side_effect=lambda event_type, data: (self.events.append({"event": event_type, "data": data}), (True, ""))[1]))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}
        session_dir = self.root / "codex" / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        (session_dir / f"rollout-2026-09-20T01-00-00-{SESSION_ID}.jsonl").write_text("{}\n")
        (self.root / "config").mkdir()

    def create(self) -> str:
        """Start a chat through the API and return its id."""
        response = self.client.post("/tasks", json={"prompt": "Tidy up my automations"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def run_codex(self, task_id: str, final: dict | None, *, reply: str | None = None,
                  process: StubAppServer | None = None, timeout: int = 30) -> None:
        """Run one exchange with a stand-in Codex that gives the final answer, or with the stand-in given."""
        if reply is not None:
            response = self.client.post(f"/tasks/{task_id}/reply", json={"reply": reply}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
        options = {"task_timeout_seconds": timeout, "task_stall_seconds": 0, "auto_save_lovelace": False}
        with (
            patch.object(server, "build_manifest", return_value={}),
            patch.object(server, "sandbox_readiness", return_value={"required": True, "ready": True}),
            patch.object(server, "read_options", return_value=options),
            patch.object(server, "validate_changed_files", return_value=[]),
            patch.object(server.subprocess, "Popen", return_value=process or StubAppServer(SESSION_ID, final)),
        ):
            server.run_task(task_id, "Tidy up my automations", SESSION_ID if reply else None, reply)
        server.active_task_runners.discard(task_id)

    def wait_for(self, condition, what: str) -> None:
        """Wait up to ten seconds for a condition on the running chat."""
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)

    def record(self, task_id: str) -> dict:
        """Return the event, the payloads and the saved files of a chat, with placeholders for what varies."""
        task = server.tasks[task_id]
        index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        saved = json.loads((self.root / "tasks" / task_id / "task.json").read_text(encoding="utf-8"))
        with patch.object(server, "retention_context", return_value=None):
            full = server.task_payload(task)
        return self.placeholders({
            "events": self.events,
            "payload": full,
            "summary": server.task_payload(task, summary=True),
            "task_json": saved,
            "index": index[task_id],
        }, task_id, {})

    def placeholders(self, value, task_id: str, turns: dict[str, str]):
        """Replace the chat's id, turn ids, times, the temporary folder and the rules stamp with fixed words.

        `turns` maps each turn id to its numbered placeholder, in the order the ids first appear in the record.
        Every 32-hex-digit id in these records is a turn id (the chat's own id is replaced first). told_rules is a
        fingerprint of rules that name the temporary folder, so only its presence is kept.
        """
        if isinstance(value, dict):
            return {key: "<rules-stamp>" if key == "told_rules" else self.placeholders(item, task_id, turns)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self.placeholders(item, task_id, turns) for item in value]
        if isinstance(value, str):
            if TIME_RE.match(value):
                return "<time>"
            value = value.replace(str(self.root), "<root>").replace(task_id, "<task>")
            return HEX_ID_RE.sub(lambda m: turns.setdefault(m.group(0), f"<turn-{len(turns) + 1}>"), value)
        return value

    def check(self, name: str, record: dict) -> None:
        """Compare a record with its snapshot; write it instead when CODEX_UPDATE_SNAPSHOTS is set."""
        stored = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else {}
        if os.environ.get("CODEX_UPDATE_SNAPSHOTS") == "1":
            stored[name] = record
            SNAPSHOT.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return
        self.assertIn(name, stored, "no snapshot yet: run once with CODEX_UPDATE_SNAPSHOTS=1")
        self.assertEqual(json.loads(json.dumps(record)), stored[name])

    def test_placeholders_keep_turns_apart(self):
        """Two turn ids get two placeholders, numbered in order, and the same id gets the same one each time."""
        first, second = "a" * 32, "b" * 32
        self.assertEqual(self.placeholders([first, second, first], "task", {}), ["<turn-1>", "<turn-2>", "<turn-1>"])

    def test_check_compares_the_event_name(self):
        """A record whose event has another name does not match the snapshot."""
        stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["completed"]
        renamed = json.loads(json.dumps(stored))
        renamed["events"][0]["event"] = "codex_cli_task_renamed"
        with patch.dict(os.environ, {"CODEX_UPDATE_SNAPSHOTS": ""}), self.assertRaises(AssertionError):
            self.check("completed", renamed)

    def test_completed_answer(self):
        """A completed exchange: its event, payload and saved files."""
        task_id = self.create()
        self.run_codex(task_id, {"status": "completed", "summary": "Two duplicates removed.", "question": "",
                                 "details": "Removed the copies.", "choices": []})
        self.check("completed", self.record(task_id))

    def test_question_with_choices(self):
        """An exchange that ends with a question and its choices."""
        task_id = self.create()
        self.run_codex(task_id, {"status": "needs_input", "summary": "Two automations are duplicates.",
                                 "question": "Remove the duplicate?", "details": "",
                                 "choices": ["Go ahead", "Don't change anything"]})
        self.check("question", self.record(task_id))

    def test_reply_continues_the_chat(self):
        """A second exchange after a question: the first turn keeps its result and the second has its own."""
        task_id = self.create()
        self.run_codex(task_id, {"status": "needs_input", "summary": "Two automations are duplicates.",
                                 "question": "Remove the duplicate?", "details": "", "choices": ["Go ahead"]})
        self.run_codex(task_id, {"status": "completed", "summary": "Removed it.", "question": "", "details": "",
                                 "choices": []}, reply="Go ahead")
        self.check("reply", self.record(task_id))

    def test_failed_start(self):
        """A chat whose Codex could not start."""
        task_id = self.create()
        server.fail_task_launch(task_id, OSError("no such file"))
        self.check("failed", self.record(task_id))

    def test_cancelled(self):
        """A queued chat cancelled before it ran."""
        task_id = self.create()
        accepted, _process, error = server.request_task_cancellation(task_id)
        self.assertTrue(accepted, error)
        self.assertTrue(server.publish_cancelled_task_outcome(task_id))
        self.check("cancelled", self.record(task_id))

    def test_timeout(self):
        """An exchange that ends because Codex ran past the time limit."""
        task_id = self.create()
        self.run_codex(task_id, None, process=StubAppServer(SESSION_ID, None, hold=True), timeout=1)
        self.check("timeout", self.record(task_id))

    def test_waiting_on_a_permission_request(self):
        """A running exchange that waits on the owner's answer to a permission request."""
        task_id = self.create()
        process = StubAppServer(SESSION_ID, None, hold=True)
        process.on_turn = lambda: process.emit(id=0, method=COMMAND_APPROVAL, params={
            "threadId": SESSION_ID, "turnId": process.turn_id, "itemId": "item", "startedAtMs": 1,
            "command": "ls /homeassistant", "cwd": "/homeassistant", "reason": "List the configuration folder",
        })
        runner = threading.Thread(target=self.run_codex, args=(task_id, None), kwargs={"process": process})
        runner.start()
        try:
            self.wait_for(lambda: server.task_payload(server.tasks[task_id])["permission_required"],
                          "the permission request")
            record = self.record(task_id)
        finally:
            server.request_task_cancellation(task_id)
            process.close()
            runner.join(timeout=10)
        self.check("permission", record)


if __name__ == "__main__":
    unittest.main()
