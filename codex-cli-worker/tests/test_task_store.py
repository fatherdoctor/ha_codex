"""TaskStore: the chats in memory, their lock, task.json and the task index."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import task_store  # noqa: E402


def waits(task: dict) -> bool:
    """A chat waits on its owner while it has a question."""
    return task.get("status") == "waiting_for_input"


class TaskStoreTests(unittest.TestCase):
    """What the store keeps, where it writes it, and what it refuses."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ended: list[tuple[str, bool]] = []
        self.clock = iter(f"2026-01-01T00:00:{second:02d}+00:00" for second in range(60))
        self.store = self.make_store()

    def make_store(self) -> task_store.TaskStore:
        """Return a store over this test's folder; wait_ended records whether the lock was held."""
        store = task_store.TaskStore(
            root=lambda: self.root / "tasks",
            index_file=lambda: self.root / "index.json",
            now=lambda: next(self.clock),
            waits_on_owner=waits,
            wait_ended=lambda task_id: self.ended.append((task_id, self.lock_is_held(store))),
        )
        return store

    @staticmethod
    def lock_is_held(store: task_store.TaskStore) -> bool:
        """Return whether some thread holds the store's lock, tried from another thread."""
        result = []

        def try_lock():
            acquired = store.lock.acquire(timeout=0.5)
            result.append(acquired)
            if acquired:
                store.lock.release()

        thread = threading.Thread(target=try_lock)
        thread.start()
        thread.join()
        return not result[0]

    def saved(self, task_id: str) -> dict:
        return json.loads((self.root / "tasks" / task_id / "task.json").read_text(encoding="utf-8"))

    def index(self) -> dict:
        return json.loads((self.root / "index.json").read_text(encoding="utf-8"))

    def test_update_creates_the_task_and_writes_both_files(self):
        self.assertTrue(self.store.update("a", {"status": "queued", "prompt": "Hello", "turns": []}, touch=True))
        self.assertEqual(self.saved("a")["prompt"], "Hello")
        self.assertEqual(self.saved("a")["updated_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(self.index()["a"], {"task_id": "a", "status": "queued",
                                             "updated_at": "2026-01-01T00:00:00+00:00"})
        self.assertFalse((self.root / "tasks" / "a" / "task.tmp").exists())

    def test_evidence_needs_an_existing_task_and_keeps_updated_at(self):
        self.assertFalse(self.store.update("missing", {"verification": []}, touch=False))
        self.assertNotIn("missing", self.store.tasks)
        self.store.update("a", {"status": "completed"}, touch=True)
        self.assertTrue(self.store.update("a", {"verification": ["check"]}, touch=False))
        self.assertEqual(self.store.tasks["a"]["updated_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(self.saved("a")["verification"], ["check"])

    def test_cancellation_wins_until_cancelled_or_cleared(self):
        self.store.update("a", {"status": "running", "cancellation_requested": True}, touch=True)
        self.assertTrue(self.store.cancellation_requested("a"))
        self.assertFalse(self.store.update("a", {"status": "failed"}, touch=True))
        self.assertEqual(self.store.tasks["a"]["status"], "running")
        self.assertTrue(self.store.update("a", {"status": "cancelled"}, touch=True))
        self.assertTrue(self.store.update("a", {"cancellation_requested": False, "status": "queued"}, touch=True))
        self.assertFalse(self.store.cancellation_requested("a"))
        self.assertFalse(self.store.cancellation_requested("missing"))

    def test_the_current_turn_gets_the_result(self):
        turn = {"turn_id": "f" * 32, "message": "Hello"}
        self.store.update("a", {"current_turn_id": turn["turn_id"], "turns": [turn]}, touch=True)
        self.store.update("a", {"status": "completed", "summary": "Done", "title": "Not a result"}, touch=True)
        saved_turn = self.saved("a")["turns"][-1]
        self.assertEqual((saved_turn["status"], saved_turn["summary"]), ("completed", "Done"))
        self.assertNotIn("title", saved_turn)
        self.assertEqual(saved_turn["updated_at"], self.store.tasks["a"]["updated_at"])

    def test_the_turn_owns_the_result_and_the_task_level_fields_copy_it(self):
        turn = {"turn_id": "f" * 32, "message": "Hello"}
        self.store.update("a", {"current_turn_id": turn["turn_id"], "turns": [turn], "status": "running"}, touch=True)
        # A task-level write that bypasses the store is not the result: it never reaches the turn.
        self.store.tasks["a"]["status"] = "stray"
        self.store.update("a", {"summary": "Working"}, touch=True)
        saved = self.saved("a")
        self.assertEqual(saved["turns"][-1]["status"], "running")
        # The copy kept for a rollback to 0.5.x, which reads only the task-level fields, and for the task index.
        self.assertEqual((saved["summary"], self.index()["a"]["summary"]), ("Working", "Working"))

    def test_a_turn_opened_with_a_result_gets_it_and_the_earlier_turn_keeps_its_own(self):
        first, second = {"turn_id": "a" * 32, "message": "One"}, {"turn_id": "b" * 32, "message": "Two"}
        self.store.update("a", {"current_turn_id": first["turn_id"], "turns": [first]}, touch=True)
        self.store.update("a", {"status": "completed", "summary": "First answer"}, touch=True)
        turns = [*self.store.tasks["a"]["turns"], second]
        self.store.update("a", {"current_turn_id": second["turn_id"], "turns": turns, "status": "queued",
                                "summary": ""}, touch=True)
        saved = self.saved("a")
        self.assertEqual([(t["status"], t["summary"]) for t in saved["turns"]],
                         [("completed", "First answer"), ("queued", "")])
        self.assertEqual((saved["status"], saved["summary"]), ("queued", ""))

    def test_a_record_without_turns_keeps_its_result_at_task_level(self):
        self.store.update("a", {"status": "completed", "summary": "Done"}, touch=True)
        saved = self.saved("a")
        self.assertEqual((saved["status"], saved["summary"]), ("completed", "Done"))
        self.assertNotIn("turns", saved)

    def test_record_result_copies_values_and_stamps_the_turn(self):
        task = {"current_turn_id": "c" * 32, "turns": [{"turn_id": "c" * 32}]}
        changes = {"added": ["a.yaml"], "changed": [], "deleted": []}
        task_store.record_result(task, {"changes": changes}, "t1")
        task["changes"]["changed"].append("c.yaml")
        self.assertEqual(task["turns"][0]["changes"], {"added": ["a.yaml"], "changed": [], "deleted": []})
        task_store.record_result(task, {}, "t2")
        self.assertEqual(task["turns"][0]["updated_at"], "t2")
        self.assertIsNone(task_store.current_turn({"current_turn_id": "d" * 32, "turns": task["turns"]}))

    def test_the_end_of_a_wait_is_reported_once_and_outside_the_lock(self):
        self.store.update("a", {"status": "waiting_for_input"}, touch=True)
        self.store.update("a", {"status": "waiting_for_input", "summary": "Still asking"}, touch=True)
        self.assertEqual(self.ended, [])
        self.store.update("a", {"status": "running"}, touch=True)
        self.assertEqual(self.ended, [("a", False)])

    def test_run_dir_follows_the_current_turn(self):
        self.assertEqual(self.store.run_dir("a"), self.root / "tasks" / "a")
        self.store.update("a", {"current_turn_id": "e" * 32}, touch=True)
        self.assertEqual(self.store.run_dir("a"), self.root / "tasks" / "a" / "turns" / ("e" * 32))

    def test_paths_are_read_at_each_use(self):
        self.root = self.root / "moved"
        self.store.update("a", {"status": "queued"}, touch=True)
        self.assertTrue((self.root / "tasks" / "a" / "task.json").is_file())
        self.assertTrue((self.root / "index.json").is_file())

    def test_load_prefers_task_json_and_restores_each_saved_task(self):
        self.store.update("a", {"status": "running", "title": "From task.json"}, touch=True)
        self.store.update("b", {"status": "completed"}, touch=True)
        index = self.index()
        index["a"]["title"] = "From the index"
        index["c"] = {"task_id": "c", "status": "queued"}
        (self.root / "index.json").write_text(json.dumps(index), encoding="utf-8")
        (self.root / "tasks" / "b" / "task.json").unlink()
        restored: list[str] = []
        loaded = self.make_store()
        loaded.load(lambda task_id, task: restored.append(task_id) or False, lambda task: False, str)
        self.assertEqual(loaded.tasks["a"]["title"], "From task.json")
        self.assertEqual(sorted(loaded.tasks), ["a", "b", "c"])
        self.assertEqual(sorted(restored), ["a", "b", "c"])

    def test_load_writes_again_the_files_that_held_a_secret_diff(self):
        self.store.update("a", {"approvals": [{"diff": "secret"}]}, touch=True)

        def forget(_task_id, task):
            had = any(item.get("diff") for item in task.get("approvals") or [])
            for item in task.get("approvals") or []:
                item["diff"] = ""
            return had

        loaded = self.make_store()
        loaded.load(forget, lambda task: any(item.get("diff") for item in task.get("approvals") or []), str)
        self.assertEqual(self.saved("a")["approvals"], [{"diff": ""}])
        self.assertEqual(self.index()["a"]["approvals"], [{"diff": ""}])

    def test_unreadable_files_are_skipped(self):
        folder = self.root / "tasks" / "broken"
        folder.mkdir(parents=True)
        (folder / "task.json").write_text("{", encoding="utf-8")
        (self.root / "index.json").write_text("[", encoding="utf-8")
        self.store.load(lambda task_id, task: False, lambda task: False, str)
        self.assertEqual(self.store.tasks, {})

    def test_legacy_records_get_turns_without_invented_answers(self):
        task = {"prompt": "First", "created_at": "t0", "reply_history": [{"reply": "Second", "at": "t1"}],
                "current_turn_id": "a" * 32, "status": "completed", "summary": "Done"}
        turns = task_store.task_turns(task)
        self.assertEqual([turn["message"] for turn in turns], ["First", "Second"])
        self.assertEqual(turns[-1]["turn_id"], "a" * 32)
        self.assertEqual(turns[-1]["summary"], "Done")
        self.assertNotIn("summary", turns[0])
        task["current_turn_id"] = "not a turn id"
        self.assertEqual(task_store.task_turns(task)[-1]["turn_id"], "legacy-1")


if __name__ == "__main__":
    unittest.main()
