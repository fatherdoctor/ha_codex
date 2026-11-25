"""The add-on does not judge a turn after it ends: no changed-file verdicts, no screenshots, no review notes.

Codex checks its own work, as its AGENTS.md says. Other writers (an automation, another app, a person in the
file editor) can change files under /homeassistant while a turn runs; the answer must not blame those changes on the turn.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_server import server
from app_server_fixture import StubAppServer
import test_backups

WORKER = Path(server.__file__).parent


class NoChecksAfterTheTurnTests(unittest.TestCase):
    """A whole exchange in which only someone other than Codex changes configuration files."""

    setUp = test_backups.RunTests.setUp
    steps = test_backups.RunTests.steps

    def codex_that_edits_nothing(self, *, outside_writes):
        """A fake Codex that only answers, while another writer changes files under /homeassistant during the turn."""

        def write_outside():
            for rel, content in outside_writes.items():
                path = self.config / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

        process = StubAppServer(
            "019fc242-910a-7c92-a17d-54c014e19fc4",
            {
                "status": "completed",
                "summary": "Here is the answer",
                "question": "",
                "details": "Notes",
            },
            on_turn=write_outside,
        )
        return patch.object(server.subprocess, "Popen", return_value=process)

    def test_a_config_file_changed_outside_the_turn_gives_no_warning(self):
        """Files another writer changed get no 'previous version' line, no review note and no screenshot."""
        self.options["auto_save_lovelace"] = True
        dashboard = {"data": {"config": {"views": [{"path": "home"}]}}}
        outside = {
            "automations.yaml": "- alias: written by something else\n",
            "scripts_manual.yaml": "b: 2\n",
            ".storage/lovelace.kitchen": json.dumps(dashboard),
        }
        with (
            self.codex_that_edits_nothing(outside_writes=outside),
            patch.object(
                server, "save_lovelace_dashboard", return_value=(True, "Saved")
            ),
            patch.object(
                server.verification,
                "run",
                side_effect=AssertionError("no check may run after the turn"),
            ),
        ):
            server.run_task(self.task_id, "What does the kitchen dashboard show?")
        task = server.tasks[self.task_id]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["summary"], "Here is the answer")
        self.assertEqual(task["details"], "Notes")
        self.assertNotIn("backups", task)
        self.assertNotIn("backups", self.events[-1])
        self.assertEqual(task.get("verification") or [], [])
        self.assertEqual(self.events[-1]["verification"], [])
        self.assertEqual(self.events[-1]["verification_attachments"], [])

    def test_credential_copies_are_still_removed_from_the_backup_folder(self):
        """Dropping the verdicts keeps the one tidy-up that matters: no credential copy stays in the folder."""

        def copy_secret():
            backup = self.run_dir / server.BACKUP_DIR / "secrets.yaml"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text("token: invented-value\n", encoding="utf-8")

        process = StubAppServer(
            "019fc242-910a-7c92-a17d-54c014e19fc4",
            {"status": "completed", "summary": "Done", "question": "", "details": ""},
            on_turn=copy_secret,
        )
        with patch.object(server.subprocess, "Popen", return_value=process):
            server.run_task(self.task_id, "Tidy the secrets file")
        self.assertFalse((self.run_dir / server.BACKUP_DIR / "secrets.yaml").exists())

    def test_the_removed_layer_stays_removed(self):
        """No code path is left that takes screenshots after a turn or words a verdict on it."""
        self.assertFalse(hasattr(server.verification, "after_changes"))
        self.assertFalse(hasattr(server, "review_backups"))
        source = (WORKER / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("Verification needs review", source)
        chat = (WORKER / "web" / "assets" / "chat.js").read_text(encoding="utf-8")
        self.assertNotIn("No previous version saved", chat)
        self.assertNotIn("renderBackups", chat)


if __name__ == "__main__":
    unittest.main()
