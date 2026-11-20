"""The guard that keeps tests off the app's real /data, /config and /addon_config folders."""
import os
import re
import tempfile
import unittest
from pathlib import Path

from app_folders import REAL_ROOTS, changes, isolated_app_folders, snapshot
from test_server import server

TESTS = Path(__file__).resolve().parent


def real_paths(module) -> dict[str, Path]:
    """Return the module-level paths of module that are below a real root."""
    return {name: value for name, value in vars(module).items()
            if isinstance(value, Path) and any(value == root or root in value.parents for root in REAL_ROOTS)}


class IsolatedFolders(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def test_every_real_path_of_the_server_moves_below_the_temporary_folder(self):
        before = real_paths(server)
        self.assertIn("DATA_ROOT", before)
        self.assertIn("CONFIG_ROOT", before)
        self.assertIn("OLD_AUTH_QR_DIR", before)
        with isolated_app_folders(server, self.base):
            self.assertEqual(real_paths(server), {})
            self.assertEqual(server.DATA_ROOT, self.base / "data")
            self.assertEqual(server.CODEX_HOME, self.base / "data" / "codex-home")
            self.assertEqual(server.OLD_AUTH_QR_DIR, self.base / "config" / "www" / "codex_cli_auth")
            self.assertEqual(server.task_root(), self.base / "data" / "codex_tasks")
            self.assertEqual(server.interaction_logs.path, self.base / "data" / "interaction-diagnostics.json")
        self.assertEqual(real_paths(server), before)

    def test_the_replacements_end_when_the_stack_closes(self):
        stack = isolated_app_folders(server, self.base)
        self.assertEqual(server.DATA_ROOT, self.base / "data")
        stack.close()
        self.assertEqual(server.DATA_ROOT, Path("/data"))


class Snapshot(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "data"

    def test_a_folder_made_by_a_test_is_a_change(self):
        before = snapshot([self.root])
        self.assertEqual(before, {str(self.root): None})
        (self.root / "codex_tasks").mkdir(parents=True)
        self.assertEqual(changes(before, snapshot([self.root])), [str(self.root)])

    def test_a_new_changed_or_deleted_file_is_a_change(self):
        (self.root / "codex_tasks").mkdir(parents=True)
        saved = self.root / "codex_tasks" / "task.json"
        saved.write_text("{}", encoding="utf-8")
        before = snapshot([self.root])
        self.assertEqual(changes(before, snapshot([self.root])), [])
        saved.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(changes(before, snapshot([self.root])), [str(self.root)])
        before = snapshot([self.root])
        (self.root / "codex_tasks" / "new.txt").write_text("x", encoding="utf-8")
        self.assertEqual(changes(before, snapshot([self.root])), [str(self.root)])
        before = snapshot([self.root])
        os.remove(saved)
        self.assertEqual(changes(before, snapshot([self.root])), [str(self.root)])


class EveryStartTestIsIsolated(unittest.TestCase):
    """A test file that runs server.main() also uses isolated_app_folders(), so a new start test cannot forget it."""

    def test_each_file_that_runs_main_uses_the_temporary_folders(self):
        runs_main = []
        for path in sorted(TESTS.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bserver\.main\(\)", text):
                runs_main.append(path.name)
                self.assertIn("isolated_app_folders(server,", text, f"{path.name} runs server.main() on the real folders")
        self.assertIn("test_agents_home.py", runs_main)


if __name__ == "__main__":
    unittest.main()
