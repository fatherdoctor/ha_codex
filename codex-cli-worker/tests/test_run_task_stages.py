"""The first half of run_task as stages: the preflight and the launch, each a function of at most 80 lines.

test_run_task_exits.py checks what each exit of run_task leaves; these tests check the shape that the split gave it.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from test_server import server

STAGES = (
    "check_launch_policy",
    "begin_exchange",
    "record_before_state",
    "make_backup_dir",
    "reset_final_file",
    "write_exchange_message",
    "check_model_pin",
    "prepare_exchange",
    "start_launch",
    "start_codex_process",
    "run_process_exchange",
    "launch_exchange",
    "run_live_exchange",
)
MAX_STAGE_LINES = 80


class StageTests(unittest.TestCase):
    """The stages exist, stay short, and stop the exchange through one exception each."""

    def test_each_stage_is_at_most_80_lines(self):
        tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
        lengths = {
            node.name: node.end_lineno - node.lineno + 1
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in STAGES
        }
        self.assertEqual(set(lengths), set(STAGES))
        for name, length in lengths.items():
            with self.subTest(stage=name):
                self.assertLessEqual(length, MAX_STAGE_LINES)

    def test_checkpoint_raises_cancelled_only_after_a_cancel(self):
        with patch.object(server, "task_cancellation_requested", return_value=False):
            server.checkpoint("task-1")
        with patch.object(server, "task_cancellation_requested", return_value=True):
            with self.assertRaises(server.Cancelled):
                server.checkpoint("task-1")

    def test_a_refused_launch_carries_the_summary_and_prefix_of_its_step(self):
        refused = server.LaunchRefused(
            ValueError("not set up"), "Stopped.", "Model pin"
        )
        self.assertIsInstance(refused.cause, ValueError)
        self.assertEqual(
            (refused.summary, refused.detail_prefix), ("Stopped.", "Model pin")
        )
        plain = server.LaunchRefused(OSError("missing"))
        self.assertEqual(plain.summary, "Could not start the Codex CLI executable.")
        self.assertIsNone(plain.detail_prefix)


if __name__ == "__main__":
    unittest.main()
