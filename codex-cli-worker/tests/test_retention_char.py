"""Characterize retention decisions and the worker's handling of absent chat folders."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import retention  # noqa: E402

NOW = datetime(2027, 3, 15, 12, tzinfo=timezone.utc)
DAY = 86400


def chat(name, age, **extra):
    return {
        "task_id": name,
        "updated_at": (NOW - timedelta(days=age)).isoformat(),
        **extra,
    }


class RetentionDecisions(unittest.TestCase):
    def test_settings_default_clamp_and_conversion(self):
        for value, expected in (
            (None, 14),
            ("bad", 14),
            ("0", 1),
            ("365", 365),
            (366, 365),
            ("7", 7),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    retention.retention_days({"chat_retention_days": value}), expected
                )

    def test_iso_dates_accept_offsets_and_naive_utc_and_reject_invalid(self):
        instant = datetime(2027, 3, 1, 12, tzinfo=timezone.utc).timestamp()
        for value in (
            "2027-03-01T12:00:00",
            "2027-03-01T12:00:00+00:00",
            "2027-03-01T07:00:00-05:00",
        ):
            with self.subTest(value=value):
                self.assertEqual(retention.seconds_of(value), instant)
        for value in (None, "", "not a date"):
            self.assertIsNone(retention.seconds_of(value))

    def test_schedule_activity_uses_latest_valid_run_and_keeps_enabled_chats(self):
        schedules = [
            {"id": "s1", "chat_id": "a", "approved": True, "enabled": False},
            {"id": "s2", "chat_id": "a", "approved": False, "enabled": True},
            {"id": "s3", "chat_id": "b", "approved": True, "enabled": True},
            {"id": "s4", "chat_id": "", "approved": True, "enabled": True},
        ]
        runs = [
            {"schedule_id": "s1", "created": 10},
            {"schedule_id": "s2", "created": 20.5},
            {"schedule_id": "s1", "created": True},
            {"schedule_id": "s3", "created": 40},
            {"schedule_id": "gone", "created": 100},
        ]
        self.assertEqual(
            retention.scheduled_activity(schedules, runs),
            {"a": 20.5, "b": float("inf")},
        )
        self.assertEqual(retention.scheduled_activity([], []), {})

    def test_last_activity_and_deletion_time_choose_latest_or_keep(self):
        task = chat("a", 10, created_at=(NOW - timedelta(days=20)).isoformat())
        latest_run = (NOW - timedelta(days=2)).timestamp()
        self.assertEqual(retention.last_activity(task, {"a": latest_run}), latest_run)
        self.assertEqual(
            retention.deletion_time(task, 14, {"a": latest_run}), latest_run + 14 * DAY
        )
        self.assertIsNone(
            retention.last_activity({"task_id": "a", "updated_at": "invalid"}, {})
        )
        self.assertIsNone(retention.deletion_time({**task, "pinned": True}, 14, {}))
        self.assertIsNone(retention.deletion_time(task, 14, {"a": float("inf")}))

    def test_expiry_boundary_order_and_busy_chats(self):
        tasks = [
            chat("edge", 14),
            chat("older", 30),
            chat("new", 13.999),
            chat("active", 20),
            chat("running", 20, status="running"),
            chat("queued", 20, status="queued"),
            chat("pin", 20, pinned=True),
            {"task_id": "unreadable", "updated_at": "bad"},
        ]
        self.assertTrue(retention.is_busy(tasks[4]))
        self.assertTrue(retention.is_busy(tasks[3], {"active"}))
        self.assertFalse(retention.is_busy(tasks[0]))
        self.assertEqual(
            retention.expired_ids(tasks, NOW.timestamp(), 14, {}, {"active"}),
            ["older", "edge"],
        )
        self.assertEqual(retention.expired_ids([], NOW.timestamp(), 14, {}), [])

    def test_warning_is_present_at_three_day_edge_and_absent_at_expiry(self):
        task = chat("a", 11)
        self.assertEqual(
            retention.deletion_warning(task, NOW.timestamp(), 14, {}),
            {
                "deletes_at": (NOW + timedelta(days=3)).isoformat(timespec="seconds"),
                "days_left": 3,
            },
        )
        self.assertIsNone(retention.deletion_warning(task, NOW.timestamp() - 1, 14, {}))
        self.assertEqual(
            retention.deletion_warning(task, NOW.timestamp() + 2 * DAY + 1, 14, {})[
                "days_left"
            ],
            1,
        )
        self.assertIsNone(
            retention.deletion_warning(task, NOW.timestamp() + 3 * DAY, 14, {})
        )


class MissingFolders(unittest.TestCase):
    def test_worker_deletes_due_chats_with_empty_or_missing_folders(self):
        from test_server import server
        from app_folders import isolated_app_folders

        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(isolated_app_folders(server, root / "app"))
            task_root = root / "tasks"
            for name, value in (
                ("tasks", {}),
                ("task_activity", {}),
                ("active_task_runners", set()),
                ("message_queue", []),
            ):
                stack.enter_context(patch.object(server, name, value))
            stack.enter_context(
                patch.object(server, "task_root", return_value=task_root)
            )
            stack.enter_context(
                patch.object(server, "TASK_STATE_FILE", root / "index.json")
            )
            stack.enter_context(patch.object(server, "read_options", return_value={}))
            stack.enter_context(
                patch.object(server, "scheduled_chat_activity", return_value={})
            )
            empty = "20270301T000000Z-aaaaaaaa"
            missing = "20270302T000000Z-bbbbbbbb"
            server.tasks.update({empty: chat(empty, 20), missing: chat(missing, 19)})
            (task_root / empty).mkdir(parents=True)
            self.assertFalse((task_root / missing).exists())
            self.assertEqual(
                server.delete_expired_chats(NOW.timestamp()), [empty, missing]
            )
            self.assertFalse((task_root / empty).exists())
            self.assertEqual(server.tasks, {})


if __name__ == "__main__":
    unittest.main()
