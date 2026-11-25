"""Live activity steps for the running exchange."""

from __future__ import annotations

from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
from app_server_fixture import StubAppServer


def item(kind, item_id, **fields):
    """Build the event Codex emits when an item of the given kind completes."""
    return {"type": "item.completed", "item": {"id": item_id, "type": kind, **fields}}


def command(item_id, cmd, output="", exit_code=0, status="completed"):
    """Build a command item event: started when in progress, completed otherwise."""
    return {
        "type": "item.completed" if status != "in_progress" else "item.started",
        "item": {
            "id": item_id,
            "type": "command_execution",
            "command": cmd,
            "aggregated_output": output,
            "exit_code": exit_code,
            "status": status,
        },
    }


FINAL_ANSWER = json.dumps(
    {"status": "completed", "summary": "Done", "question": "", "details": ""}
)


class ActivityStepTests(unittest.TestCase):
    """How the events Codex reports become the steps of the running exchange."""

    def setUp(self):
        """Start collecting steps for one running exchange."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(server.tasks, clear=True))
        for name, value in (("task_activity", {}), ("active_task_runners", set())):
            self.stack.enter_context(patch.object(server, name, value))
        server.tasks["t"] = {
            "task_id": "t",
            "status": "running",
            "current_turn_id": "turn-1",
        }
        server.start_activity("t", "turn-1")

    def steps(self, after=0):
        """What the activity endpoint would return now for steps after a sequence number."""
        with server.lock:
            return server.activity_payload_locked(server.task_activity["t"], after)

    def test_items_map_to_steps(self):
        """Each kind of item Codex reports becomes a readable step, without the final answer or empty messages."""
        events = [
            item("reasoning", "item_0", text="**Checking the automation**"),
            item("agent_message", "item_1", text="I'll read the automation first."),
            command(
                "item_2",
                "/bin/sh -lc 'cat /homeassistant/automations.yaml'",
                output="x" * 5000,
            ),
            command(
                "item_3",
                '/bin/sh -lc "rg -n foo /homeassistant"',
                output="",
                exit_code=1,
            ),
            item(
                "file_change",
                "item_4",
                status="completed",
                changes=[
                    {"path": "/homeassistant/automations.yaml", "kind": "update"},
                    {"path": "/homeassistant/new.yaml", "kind": "add"},
                ],
            ),
            item("web_search", "item_5", query="", action={"type": "other"}),
            item(
                "mcp_tool_call",
                "item_6",
                server="codex_apps",
                tool="github.get_repo",
                status="completed",
                error=None,
            ),
            item(
                "todo_list",
                "item_7",
                items=[
                    {"text": "Read config", "completed": True},
                    {"text": "Edit", "completed": False},
                ],
            ),
            item("agent_message", "item_8", text=FINAL_ANSWER),
            item("agent_message", "item_9", text=""),
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ]
        for event in events:
            server.record_activity_event("t", event)
        payload = self.steps()
        self.assertTrue(payload["running"])
        self.assertEqual(payload["turn_id"], "turn-1")
        self.assertEqual(
            [step["kind"] for step in payload["steps"]],
            [
                "reasoning",
                "message",
                "command",
                "command",
                "file_change",
                "web_search",
                "tool_call",
                "plan",
            ],
        )
        self.assertEqual([step["index"] for step in payload["steps"]], list(range(8)))
        reasoning, message, cat, rg, edit, search, tool, plan = payload["steps"]
        self.assertEqual(reasoning["text"], "Checking the automation")
        self.assertEqual(message["text"], "I'll read the automation first.")
        self.assertEqual(cat["text"], "cat /homeassistant/automations.yaml")
        self.assertTrue(cat["output_truncated"])
        self.assertEqual(len(cat["output"]), server.ACTIVITY_OUTPUT_MAX + 1)
        self.assertEqual(cat["status"], "done")
        self.assertEqual(rg["text"], "rg -n foo /homeassistant")
        self.assertEqual((rg["status"], rg["exit_code"]), ("failed", 1))
        self.assertEqual(edit["text"], "Edited automations.yaml, Added new.yaml")
        self.assertEqual(
            edit["files"],
            [
                {"path": "automations.yaml", "kind": "update"},
                {"path": "new.yaml", "kind": "add"},
            ],
        )
        self.assertEqual(search["text"], "Searched the web")
        self.assertEqual(tool["text"], "codex_apps · github.get_repo")
        self.assertEqual(plan["text"], "☑ Read config\n☐ Edit")
        self.assertEqual(reasoning["id"], "item_0")

    def test_plain_text_final_answer_is_not_an_activity_message(self):
        """The final answer belongs to the reply, even if commentary used the same words."""
        for event in (
            item(
                "agent_message",
                "first",
                phase="commentary",
                text="Checking the automation.",
            ),
            item(
                "agent_message",
                "second",
                phase="commentary",
                text="The automation is valid.",
            ),
            item(
                "agent_message",
                "answer",
                phase="final_answer",
                text="The automation is valid.",
            ),
        ):
            server.record_activity_event("t", event)
        messages = self.steps()["steps"]
        self.assertEqual(
            [(step["id"], step["text"]) for step in messages],
            [
                ("first", "Checking the automation."),
                ("second", "The automation is valid."),
            ],
        )
        self.assertEqual(
            [step["phase"] for step in messages], ["commentary", "commentary"]
        )

    def test_file_change_reads_as_made_only_once_it_was_made(self):
        """A change that waits, as for permission, or that was refused is not reported as done."""
        changes = [
            {"path": "/homeassistant/automations.yaml", "kind": "update"},
            {"path": "/homeassistant/old.yaml", "kind": "delete"},
            {"path": "/homeassistant/new.yaml", "kind": "add"},
            {"path": "/homeassistant/moved.yaml", "kind": "other"},
        ]
        server.record_activity_event(
            "t",
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "file_change",
                    "status": "in_progress",
                    "changes": changes,
                },
            },
        )
        waiting = self.steps()["steps"][0]
        self.assertEqual(
            (waiting["status"], waiting["text"]),
            (
                "running",
                "Editing automations.yaml, Deleting old.yaml, Adding new.yaml, Changing moved.yaml",
            ),
        )
        server.record_activity_event(
            "t", item("file_change", "item_1", status="declined", changes=changes)
        )
        refused = self.steps()["steps"][0]
        self.assertEqual(
            (refused["status"], refused["text"]),
            (
                "failed",
                "Did not edit automations.yaml, Did not delete old.yaml, Did not add new.yaml, "
                "Did not change moved.yaml",
            ),
        )
        server.record_activity_event(
            "t", item("file_change", "item_2", status="completed", changes=changes[:1])
        )
        server.record_activity_event(
            "t", item("file_change", "item_3", status="declined", changes=[])
        )
        made, empty = self.steps()["steps"][1:]
        self.assertEqual(
            (made["status"], made["text"]), ("done", "Edited automations.yaml")
        )
        self.assertEqual(empty["text"], "Did not change files")

    def test_started_items_update_in_place_with_duration(self):
        """An item that starts and then completes stays one step, updated with its result and duration."""
        server.record_activity_event(
            "t",
            command(
                "item_1", "/bin/sh -lc 'sleep 1'", status="in_progress", exit_code=None
            ),
        )
        first = self.steps()
        self.assertEqual(first["steps"][0]["status"], "running")
        self.assertIsNone(first["steps"][0]["duration_ms"])
        server.record_activity_event(
            "t", command("item_1", "/bin/sh -lc 'sleep 1'", output="done")
        )
        second = self.steps(after=first["seq"])
        self.assertEqual(second["total"], 1)
        self.assertEqual(len(second["steps"]), 1)
        self.assertEqual(second["steps"][0]["status"], "done")
        self.assertEqual(second["steps"][0]["output"], "done")
        self.assertIsInstance(second["steps"][0]["duration_ms"], int)
        self.assertEqual(self.steps(after=second["seq"])["steps"], [])

    def test_repeated_error_reports_become_one_step(self):
        """An error that Codex reports as an item, an event, and a failed turn shows as one failed step."""
        message = "Your access token could not be refreshed."
        server.record_activity_event("t", item("error", "item_0", message=message))
        server.record_activity_event("t", {"type": "error", "message": message})
        server.record_activity_event(
            "t", {"type": "turn.failed", "error": {"message": message}}
        )
        payload = self.steps()
        self.assertEqual(
            [(s["kind"], s["status"], s["text"]) for s in payload["steps"]],
            [("error", "failed", message)],
        )

    def test_secrets_are_redacted_and_steps_are_capped(self):
        """Tokens are redacted from a step's command and output, and one notice replaces the steps past the limit."""
        server.record_activity_event(
            "t",
            command(
                "item_0",
                "/bin/sh -lc 'curl -H \"Authorization: Bearer abcdefghijklmnop123456\" x'",
                output="token=abcdefghijklmnop123456",
            ),
        )
        step = self.steps()["steps"][0]
        self.assertNotIn("abcdefghijklmnop123456", step["text"] + step["output"])
        for index in range(server.ACTIVITY_MAX_STEPS + 20):
            server.record_activity_event(
                "t", item("reasoning", f"r{index}", text=f"Step {index}")
            )
        payload = self.steps()
        self.assertEqual(payload["total"], server.ACTIVITY_MAX_STEPS + 1)
        self.assertEqual(payload["steps"][-1]["kind"], "notice")
        server.tasks["t"]["status"] = "failed"
        server.tasks["t"]["summary"] = "Codex timed out after 5 seconds."
        with patch.object(server.task_store, "atomic_json_write"):
            with patch.object(
                server, "task_root", return_value=Path(tempfile.gettempdir())
            ):
                server.finish_activity("t")
        self.assertNotIn("t", server.task_activity)

    def test_events_without_a_running_exchange_are_ignored(self):
        """Events for a task with no running exchange, and events that are not objects, add no steps."""
        server.record_activity_event("other", item("reasoning", "r", text="Lost"))
        server.record_activity_event("t", "not a dict")
        self.assertEqual(self.steps()["total"], 0)
        self.assertNotIn("other", server.task_activity)


class ActivityApiTests(unittest.TestCase):
    """The activity endpoint: live steps while an exchange runs and the stored steps afterwards."""

    def setUp(self):
        """Isolate task storage and CODEX_HOME in a temp directory, save a Codex session, and mock the task runner."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(server.tasks, clear=True))
        for name, value in (
            ("task_activity", {}),
            ("active_task_runners", set()),
            ("running_processes", {}),
        ):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(
            patch.object(server, "task_root", return_value=self.root / "tasks")
        )
        self.stack.enter_context(
            patch.object(server, "TASK_STATE_FILE", self.root / "index.json")
        )
        self.stack.enter_context(
            patch.object(server, "CODEX_HOME", self.root / "codex")
        )
        self.stack.enter_context(
            patch.object(server, "api_token", return_value="test-token")
        )
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}
        self.session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        (
            session_dir / f"rollout-2026-09-20T01-00-00-{self.session_id}.jsonl"
        ).write_text("{}\n")

    def get(self, task_id, after=None):
        """Request a task's activity, optionally only the steps after a sequence number."""
        query = "" if after is None else f"?after={after}"
        return self.client.get(
            f"/tasks/{task_id}/activity{query}", headers=self.headers
        )

    def create(self):
        """Create a queued task through the API and return its id."""
        response = self.client.post(
            "/tasks", json={"prompt": "Review my automation"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def test_live_steps_then_stored_steps_then_new_turn(self):
        """Activity shows live steps during a run, the stored steps after it, and none once a new exchange starts."""
        task_id = self.create()
        turn_id = server.tasks[task_id]["current_turn_id"]
        self.assertEqual(
            self.get(task_id).json,
            {
                "ok": True,
                "turn_id": turn_id,
                "seq": 0,
                "running": False,
                "total": 0,
                "steps": [],
            },
        )
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        server.record_activity_event(task_id, item("reasoning", "r0", text="Looking"))
        server.record_activity_event(
            task_id,
            command("c1", "/bin/sh -lc 'ls'", status="in_progress", exit_code=None),
        )
        live = self.get(task_id).json
        self.assertTrue(live["running"])
        self.assertEqual([step["text"] for step in live["steps"]], ["Looking", "ls"])
        self.assertEqual(self.get(task_id, after=live["seq"]).json["steps"], [])
        self.assertEqual(self.get(task_id, after="x").status_code, 400)
        self.assertEqual(self.get("missing").status_code, 404)

        server.update_task(
            task_id,
            status="cancelled",
            summary=server.CANCELLED_TASK_SUMMARY,
            session_id=self.session_id,
            completed_at=server.utc_now(),
        )
        server.finish_activity(task_id)
        server.active_task_runners.discard(task_id)
        stored_file = (
            self.root / "tasks" / task_id / "turns" / turn_id / server.ACTIVITY_FILE
        )
        self.assertTrue(stored_file.is_file())
        self.assertNotIn(task_id, server.task_activity)
        stored = self.get(task_id).json
        self.assertFalse(stored["running"])
        self.assertEqual(
            [(s["kind"], s["status"]) for s in stored["steps"]],
            [("reasoning", "done"), ("command", "failed"), ("outcome", "failed")],
        )
        self.assertEqual(stored["steps"][-1]["text"], "Stopped")
        self.assertEqual(
            len(self.get(task_id, after=stored["seq"] - 1).json["steps"]), 1
        )

        server.tasks[task_id]["cancellation_requested"] = True
        response = self.client.post(
            f"/tasks/{task_id}/continue",
            json={"message": "Go on"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        fresh = self.get(task_id).json
        self.assertNotEqual(fresh["turn_id"], turn_id)
        self.assertEqual(fresh["steps"], [])

    def test_an_earlier_turn_returns_its_own_saved_steps(self):
        """With `turn`, an earlier answer gets its saved steps; only a turn id of this chat is read."""
        task_id = self.create()
        first = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, first)
        server.record_activity_event(task_id, command("c1", "/bin/sh -lc 'ls'"))
        server.update_task(
            task_id,
            status="completed",
            session_id=self.session_id,
            summary="Done",
            completed_at=server.utc_now(),
        )
        server.finish_activity(task_id)
        server.active_task_runners.discard(task_id)
        response = self.client.post(
            f"/tasks/{task_id}/continue",
            json={"message": "Go on"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        second = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, second)
        server.record_activity_event(
            task_id, item("reasoning", "r0", text="Looking again")
        )

        def get_turn(turn_id, after=0):
            return self.client.get(
                f"/tasks/{task_id}/activity?turn={turn_id}&after={after}",
                headers=self.headers,
            )

        earlier = get_turn(first).json
        self.assertEqual((earlier["turn_id"], earlier["running"]), (first, False))
        self.assertEqual([step["text"] for step in earlier["steps"]], ["ls"])
        self.assertEqual(get_turn(first, after=earlier["seq"]).json["steps"], [])
        live = get_turn(second).json
        self.assertTrue(live["running"])
        self.assertEqual([step["text"] for step in live["steps"]], ["Looking again"])
        self.assertEqual(
            [step["text"] for step in self.get(task_id).json["steps"]],
            ["Looking again"],
        )
        other = "0" * 32
        for wanted in ("../" + first, first.upper(), first[:-1], "legacy", other, ""):
            with self.subTest(turn=wanted):
                self.assertEqual(get_turn(wanted).status_code, 404)
        # An id of the right form that belongs to this chat but saved no steps reads as no steps.
        server.tasks[task_id]["turns"][0]["turn_id"] = other
        self.assertEqual(get_turn(other).json["steps"], [])
        # A turn id that is not of the turn id form is never made into a path, even when the chat lists it.
        server.tasks[task_id]["turns"][0]["turn_id"] = "../escape"
        self.assertEqual(get_turn("../escape").status_code, 404)

    def test_failed_run_records_its_summary(self):
        """A failed run stores an outcome step that carries the task's failure summary."""
        task_id = self.create()
        server.update_task(task_id, status="running")
        server.start_activity(task_id, server.tasks[task_id]["current_turn_id"])
        server.update_task(
            task_id,
            status="failed",
            summary="Codex timed out after 5 seconds.",
            completed_at=server.utc_now(),
        )
        server.finish_activity(task_id)
        steps = self.get(task_id).json["steps"]
        self.assertEqual(
            [step["text"] for step in steps],
            ["Failed: Codex timed out after 5 seconds."],
        )

    def test_recovered_chat_keeps_its_turn_id_and_saved_steps(self):
        """A chat rebuilt from the task index after task.json was lost keeps the turn id of its saved steps."""
        task_id = self.create()
        turn_id = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        server.record_activity_event(task_id, command("c1", "/bin/sh -lc 'ls'"))
        server.update_task(
            task_id,
            status="completed",
            session_id=self.session_id,
            summary="Done",
            completed_at=server.utc_now(),
        )
        server.finish_activity(task_id)
        server.active_task_runners.discard(task_id)
        saved = self.get(task_id).json
        self.assertEqual([step["text"] for step in saved["steps"]], ["ls"])

        (self.root / "tasks" / task_id / "task.json").unlink()
        server.tasks.clear()
        server.load_task_index()
        recovered = server.tasks[task_id]
        self.assertNotIn("turns", recovered)
        self.assertEqual(
            [turn["turn_id"] for turn in server.task_turns(recovered)], [turn_id]
        )

        response = self.client.post(
            f"/tasks/{task_id}/continue",
            json={"message": "Go on"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        turns = server.tasks[task_id]["turns"]
        self.assertEqual(turns[0]["turn_id"], turn_id)
        self.assertNotEqual(turns[1]["turn_id"], turn_id)
        self.assertEqual(
            server.load_stored_activity(task_id, turns[0]["turn_id"])["steps"],
            saved["steps"],
        )

    def test_a_turn_id_from_the_index_is_used_only_when_it_has_the_turn_id_form(self):
        """An index entry with a missing or malformed turn id still gets the legacy turn."""
        for current in (None, "", "turn-1", "../other", "A" * 32, 32):
            with self.subTest(current=current):
                task = {"task_id": "t", "prompt": "Hello", "current_turn_id": current}
                self.assertEqual(
                    [turn["turn_id"] for turn in server.task_turns(task)], ["legacy"]
                )

    def test_plan_updates_from_codex_are_one_redacted_step_at_its_first_place(self):
        """Codex's plan becomes one to-do step that keeps its place, with credentials redacted."""
        task_id = self.create()
        turn_id = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        stdin = type("Transport", (io.StringIO,), {"close": lambda self: None})()
        proc = type("Process", (), {"stdin": stdin, "poll": lambda _: None})()
        session = server.create_approval_session(
            task_id,
            proc,
            "Review",
            self.root / "final.json",
            None,
            {},
            server.DEFAULT_OPTIONS,
        )
        session.thread_id, session.turn_id = "thread", "turn"

        def update(*steps, turn="turn"):
            session.receive(
                {
                    "method": "turn/plan/updated",
                    "params": {
                        "threadId": "thread",
                        "turnId": turn,
                        "plan": [
                            {"step": text, "status": status} for text, status in steps
                        ],
                    },
                }
            )

        update(
            ("Read the automation", "pending"),
            ("Call the API with test-token", "pending"),
        )
        first = self.get(task_id).json["steps"]
        self.assertEqual(
            [step["text"] for step in first],
            ["☐ Read the automation\n☐ Call the API with [redacted]"],
        )
        server.record_activity_event(task_id, command("c1", "/bin/sh -lc 'ls'"))
        update(("Read the automation", "completed"), turn="old")
        session.receive(
            {
                "method": "turn/plan/updated",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "plan": [{"step": "Read the automation"}],
                },
            }
        )
        self.assertEqual(self.get(task_id).json["steps"][0]["text"], first[0]["text"])
        update(
            ("Read the automation", "completed"),
            ("Call the API with test-token", "inProgress"),
        )
        steps = self.get(task_id).json["steps"]
        self.assertEqual(
            [(step["kind"], step["text"]) for step in steps],
            [
                ("plan", "☑ Read the automation\n☐ Call the API with [redacted]"),
                ("command", "ls"),
            ],
        )
        self.assertEqual(steps[0]["index"], first[0]["index"])

    def test_streamed_reasoning_summary_writes_one_log_line_for_its_item(self):
        """Live summary updates reach the step, but codex.log holds the reasoning item only once, when it completes."""
        task_id = self.create()
        turn_id = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        stdin = type("Transport", (io.StringIO,), {"close": lambda self: None})()
        proc = type("Process", (), {"stdin": stdin, "poll": lambda _: None})()
        session = server.create_approval_session(
            task_id,
            proc,
            "Review",
            self.root / "final.json",
            None,
            {},
            server.DEFAULT_OPTIONS,
        )
        session.thread_id, session.turn_id = "thread", "turn"
        log = server.get_task_dir(task_id) / "codex.log"

        def receive(method, **fields):
            session.receive(
                {
                    "method": method,
                    "params": {"threadId": "thread", "turnId": "turn", **fields},
                }
            )

        def reasoning_lines():
            text = log.read_text(encoding="utf-8") if log.exists() else ""
            return [line for line in text.splitlines() if '"reasoning"' in line]

        receive(
            "item/started", item={"id": "thought", "type": "reasoning", "summary": []}
        )
        started = len(reasoning_lines())
        for _ in range(100):
            receive(
                "item/reasoning/summaryTextDelta",
                itemId="thought",
                summaryIndex=0,
                delta="word",
            )
        self.assertEqual(self.get(task_id).json["steps"][0]["text"], "word" * 100)
        self.assertEqual(len(reasoning_lines()), started)
        receive(
            "item/completed",
            item={"id": "thought", "type": "reasoning", "summary": ["word" * 100]},
        )
        lines = reasoning_lines()
        self.assertEqual(len(lines), started + 1)
        self.assertEqual(sum(line.count("word") for line in lines), 100)

    def test_deleting_a_chat_forgets_its_steps(self):
        """Deleting a chat also drops the steps held in memory for it."""
        task_id = self.create()
        server.start_activity(task_id, server.tasks[task_id]["current_turn_id"])
        server.update_task(
            task_id,
            status="completed",
            session_id=self.session_id,
            completed_at=server.utc_now(),
        )
        server.active_task_runners.discard(task_id)
        response = self.client.delete(f"/tasks/{task_id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(task_id, server.task_activity)


class ReasoningSummaryOptionTests(unittest.TestCase):
    """The add-on option for how detailed Codex's reasoning summaries are."""

    def test_option_is_passed_to_codex_and_validated(self):
        """The reasoning summary option reaches Codex, and a missing or unsupported value falls back to concise."""
        for configured, expected in (
            ("detailed", "detailed"),
            ("none", "none"),
            (None, "concise"),
            ("auto", "concise"),
            ("  Concise ", "concise"),
        ):
            with self.subTest(configured=configured):
                self.assertEqual(
                    server.reasoning_summary({"reasoning_summary": configured}),
                    expected,
                )
        with patch.dict(
            server.tasks,
            {
                "t": {
                    "task_id": "t",
                    "turns": [],
                    "chat_settings": server.DEFAULT_CHAT_SETTINGS,
                }
            },
            clear=True,
        ):
            session = server.create_approval_session(
                "t",
                StubAppServer("thread"),
                "Prompt",
                Path("final"),
                None,
                {},
                {"reasoning_summary": "detailed"},
            )
        self.assertEqual(session.turn_params["summary"], "detailed")
        self.assertEqual(session.turn_params["effort"], "medium")


if __name__ == "__main__":
    unittest.main()
