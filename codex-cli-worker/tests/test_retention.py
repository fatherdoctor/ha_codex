"""Chat retention: when a chat with no message for a while is deleted, and when the page warns about it."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import retention  # noqa: E402

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
DAY = 86400


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def chat(task_id: str, days: float, **extra) -> dict:
    return {
        "task_id": task_id,
        "status": "completed",
        "created_at": ago(days + 1),
        "updated_at": ago(days),
        **extra,
    }


def due(chats, scheduled=None, days=14, active=()):
    return retention.expired_ids(chats, NOW.timestamp(), days, scheduled or {}, active)


class SettingTests(unittest.TestCase):
    def test_default_is_14_and_the_value_is_clamped(self):
        cases = (
            (None, 14),
            ("x", 14),
            ("", 14),
            (0, 1),
            (-5, 1),
            (30, 30),
            ("30", 30),
            (9999, 365),
        )
        for configured, expected in cases:
            with self.subTest(configured=configured):
                self.assertEqual(
                    retention.retention_days({"chat_retention_days": configured}),
                    expected,
                )
        self.assertEqual(retention.retention_days({}), 14)


class ExpiryTests(unittest.TestCase):
    def test_a_chat_with_no_message_for_the_time_is_deleted_and_a_newer_one_is_kept(
        self,
    ):
        chats = [chat("old", 14.01), chat("edge", 13.99), chat("new", 1)]
        self.assertEqual(due(chats), ["old"])

    def test_the_setting_changes_the_time(self):
        chats = [chat("a", 5), chat("b", 2)]
        self.assertEqual(due(chats, days=3), ["a"])

    def test_a_pinned_chat_never_expires(self):
        self.assertEqual(due([chat("p", 400, pinned=True)]), [])
        self.assertIsNone(
            retention.deletion_warning(
                chat("p", 12, pinned=True), NOW.timestamp(), 14, {}
            )
        )

    def test_a_scheduled_run_counts_as_a_message(self):
        run_time = (NOW - timedelta(days=2)).timestamp()
        self.assertEqual(due([chat("s", 30)], {"s": run_time}), [])
        self.assertEqual(
            due([chat("s", 30)], {"s": (NOW - timedelta(days=20)).timestamp()}), ["s"]
        )

    def test_a_chat_with_an_approved_enabled_schedule_is_kept(self):
        schedules = [{"id": "x", "chat_id": "s", "approved": 1, "enabled": 1}]
        scheduled = retention.scheduled_activity(schedules, [])
        self.assertEqual(due([chat("s", 400)], scheduled), [])
        self.assertIsNone(
            retention.deletion_warning(chat("s", 400), NOW.timestamp(), 14, scheduled)
        )

    def test_schedule_facts_come_from_runs_of_the_chats_own_schedules(self):
        schedules = [
            {"id": "a", "chat_id": "c1", "approved": 1, "enabled": 0},
            {"id": "b", "chat_id": "", "approved": 1, "enabled": 1},
            {"id": "c", "chat_id": "c2", "approved": 0, "enabled": 1},
        ]
        runs = [
            {"schedule_id": "a", "created": 100.0},
            {"schedule_id": "a", "created": 300.0},
            {"schedule_id": "b", "created": 999.0},
            {"schedule_id": "gone", "created": 5.0},
            {"schedule_id": "a", "created": True},
        ]
        self.assertEqual(retention.scheduled_activity(schedules, runs), {"c1": 300.0})

    def test_a_running_or_waiting_chat_is_never_deleted(self):
        chats = [
            chat("r", 30, status="running"),
            chat("q", 30, status="queued"),
            chat("a", 30),
            chat("z", 30),
        ]
        self.assertEqual(due(chats, active={"a"}), ["z"])

    def test_a_chat_with_no_readable_time_is_kept(self):
        broken = {
            "task_id": "b",
            "status": "completed",
            "created_at": "soon",
            "updated_at": "",
        }
        self.assertEqual(due([broken]), [])

    def test_a_time_without_a_zone_is_read_as_utc(self):
        naive = {
            "task_id": "n",
            "status": "completed",
            "updated_at": "2026-05-01T00:00:00",
        }
        self.assertEqual(due([naive]), ["n"])

    def test_the_oldest_deletion_comes_first(self):
        self.assertEqual(due([chat("b", 20), chat("a", 40)]), ["a", "b"])


class WarningTests(unittest.TestCase):
    def warn(self, days_idle, **extra):
        return retention.deletion_warning(
            chat("w", days_idle, **extra), NOW.timestamp(), 14, {}
        )

    def test_no_warning_until_the_last_three_days(self):
        self.assertIsNone(self.warn(10.9))
        self.assertEqual(self.warn(11.5)["days_left"], 3)
        self.assertEqual(self.warn(12.5)["days_left"], 2)
        self.assertEqual(self.warn(13.5)["days_left"], 1)

    def test_the_warning_names_the_deletion_time(self):
        warning = self.warn(13)
        self.assertEqual(
            warning["deletes_at"],
            (NOW + timedelta(days=1)).isoformat(timespec="seconds"),
        )

    def test_no_warning_once_the_time_has_passed(self):
        self.assertIsNone(self.warn(14.5))


class ServerTests(unittest.TestCase):
    """The worker deletes an expired chat with its files, keeps the rest, and shows the warning."""

    def setUp(self):
        from test_server import server
        from app_folders import isolated_app_folders

        self.server = server
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(isolated_app_folders(server, root / "app"))
        self.root = root / "tasks"
        stack.enter_context(patch.dict(server.tasks, clear=True))
        for name, value in (
            ("task_activity", {}),
            ("active_task_runners", set()),
            ("message_queue", []),
        ):
            stack.enter_context(patch.object(server, name, value))
        stack.enter_context(patch.object(server, "task_root", return_value=self.root))
        stack.enter_context(
            patch.object(server, "TASK_STATE_FILE", root / "index.json")
        )
        stack.enter_context(patch.object(server, "read_options", return_value={}))
        self.scheduled = {}
        stack.enter_context(
            patch.object(
                server, "scheduled_chat_activity", side_effect=lambda: self.scheduled
            )
        )
        for task_id, days, extra in (
            ("20260101T000000Z-aaaaaaaa", 20, {}),
            ("20260102T000000Z-bbbbbbbb", 20, {"pinned": True}),
            ("20260103T000000Z-cccccccc", 1, {}),
            ("20260104T000000Z-dddddddd", 12, {}),
        ):
            server.tasks[task_id] = chat(task_id, days, **extra)
            (self.root / task_id).mkdir(parents=True)
            (self.root / task_id / "prompt.txt").write_text("hi", encoding="utf-8")
        self.ids = list(server.tasks)

    def test_only_the_expired_unpinned_chat_goes_with_its_files(self):
        deleted = self.server.delete_expired_chats(NOW.timestamp())
        self.assertEqual(deleted, [self.ids[0]])
        self.assertEqual(sorted(self.server.tasks), sorted(self.ids[1:]))
        self.assertFalse((self.root / self.ids[0]).exists())
        for kept in self.ids[1:]:
            self.assertTrue((self.root / kept / "prompt.txt").exists())

    def test_a_chat_with_a_message_waiting_in_the_queue_is_kept(self):
        entry = {"task_id": self.ids[0], "queue_id": "q1", "turn_id": "t1"}
        self.server.message_queue.append(entry)
        self.assertEqual(self.server.delete_expired_chats(NOW.timestamp()), [])
        self.assertIn(self.ids[0], self.server.tasks)
        self.assertEqual(self.server.message_queue, [entry])

    def test_a_chat_that_is_no_longer_due_is_kept_when_the_lock_is_taken(self):
        self.server.tasks[self.ids[0]]["pinned"] = True
        self.assertEqual(
            self.server.delete_chat(self.ids[0], lambda _id: False)[0], "active"
        )
        self.assertTrue((self.root / self.ids[0] / "prompt.txt").exists())

    def test_a_pin_after_the_first_look_stops_the_deletion(self):
        real = self.server.retention.expired_ids
        calls = []

        def pin_after_first_look(*args, **kwargs):
            result = real(*args, **kwargs)
            if not calls:
                self.server.tasks[self.ids[0]]["pinned"] = True
            calls.append(1)
            return result

        with patch.object(
            self.server.retention, "expired_ids", side_effect=pin_after_first_look
        ):
            self.assertEqual(self.server.delete_expired_chats(NOW.timestamp()), [])
        self.assertIn(self.ids[0], self.server.tasks)

    def test_a_message_queued_after_the_first_look_stops_the_deletion(self):
        real = self.server.retention.expired_ids
        calls = []
        entry = {"task_id": self.ids[0], "queue_id": "q1", "turn_id": "t1"}

        def queue_after_first_look(*args, **kwargs):
            result = real(*args, **kwargs)
            if not calls:
                self.server.message_queue.append(entry)
            calls.append(1)
            return result

        with patch.object(
            self.server.retention, "expired_ids", side_effect=queue_after_first_look
        ):
            self.assertEqual(self.server.delete_expired_chats(NOW.timestamp()), [])
        self.assertIn(self.ids[0], self.server.tasks)
        self.assertEqual(self.server.message_queue, [entry])

    def test_the_chats_attachments_go_with_it(self):
        uploads = self.server.ATTACHMENTS_ROOT / self.ids[0] / "turn-1"
        uploads.mkdir(parents=True)
        (uploads / "photo.png").write_bytes(b"x")
        kept = self.server.ATTACHMENTS_ROOT / self.ids[2] / "turn-1"
        kept.mkdir(parents=True)
        (kept / "photo.png").write_bytes(b"x")
        self.assertEqual(
            self.server.delete_expired_chats(NOW.timestamp()), [self.ids[0]]
        )
        self.assertFalse((self.server.ATTACHMENTS_ROOT / self.ids[0]).exists())
        self.assertTrue((kept / "photo.png").exists())

    def test_nothing_is_deleted_when_the_schedules_cannot_be_read(self):
        with patch.object(self.server, "scheduled_chat_activity", return_value=None):
            self.assertEqual(self.server.delete_expired_chats(NOW.timestamp()), [])
        self.assertEqual(len(self.server.tasks), 4)

    def test_a_scheduled_chat_survives(self):
        self.scheduled = {self.ids[0]: float("inf")}
        self.assertEqual(self.server.delete_expired_chats(NOW.timestamp()), [])

    def test_the_payload_carries_the_warning_only_in_the_last_days(self):
        context = (NOW.timestamp(), 14, {})
        warned = self.server.task_payload(
            self.server.tasks[self.ids[3]], summary=True, deletion=context
        )
        self.assertEqual(warned["deletion_warning"]["days_left"], 2)
        for kept in self.ids[1:3]:
            self.assertIsNone(
                self.server.task_payload(
                    self.server.tasks[kept], summary=True, deletion=context
                )["deletion_warning"]
            )
        self.assertIsNone(
            self.server.task_payload(self.server.tasks[self.ids[3]], summary=True)[
                "deletion_warning"
            ]
        )

    def test_the_delete_route_keeps_its_answers(self):
        self.assertEqual(self.server.delete_chat("nope")[0], "missing")
        self.server.tasks[self.ids[2]]["status"] = "running"
        self.assertEqual(self.server.delete_chat(self.ids[2])[0], "active")


if __name__ == "__main__":
    unittest.main()
