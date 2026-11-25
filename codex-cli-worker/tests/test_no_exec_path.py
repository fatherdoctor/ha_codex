"""Every exchange starts Codex as `codex app-server`, never as `codex exec`.

`codex exec` has no live permission requests: a person cannot answer Codex while it runs. The worker used to keep
that start path from upstream, reachable only by tests. It is removed, and these tests fail if it comes back.
"""

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_server_fixture import StubAppServer
from test_server import server

WORKER = Path(__file__).resolve().parents[1]

THREAD_ID = "019fc242-910a-7c92-a17d-54c014e19fc4"
ANSWER = {"status": "completed", "summary": "Done", "question": "", "details": "", "choices": []}


def worker_sources() -> list[Path]:
    """Return every Python file the app ships: the worker's own modules, not its tests."""
    return sorted(path for path in WORKER.rglob("*.py") if "tests" not in path.relative_to(WORKER).parts)


class SourceTests(unittest.TestCase):
    """No shipped module can build an exec command line."""

    def test_no_module_names_the_exec_subcommand(self) -> None:
        """No string in the shipped Python code is the word exec, so no argument list can carry it."""
        found = []
        for path in worker_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() == "exec":
                    found.append(f"{path.relative_to(WORKER)}:{node.lineno}")
        self.assertEqual(found, [])

    def test_the_exec_helpers_are_gone(self) -> None:
        """The functions and the schema file of the exec path are not in the worker."""
        for name in ("build_codex_args", "uses_user_approvals", "SCHEMA_PATH"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(server, name))


class LaunchTests(unittest.TestCase):
    """run_task starts the app server for every mode the policy allows, attended or not."""

    def launch(self, options: dict, *, background: bool) -> list[str]:
        """Run one exchange with a stub app server and return the command line the worker started."""
        task_id = "no-exec-task"
        proc = StubAppServer(THREAD_ID, ANSWER)
        saved = server.tasks.get(task_id)
        server.tasks[task_id] = {"task_id": task_id, "status": "queued"}
        try:
            with tempfile.TemporaryDirectory() as temp_dir, \
                 patch.object(server.store, "task_dir", return_value=Path(temp_dir) / task_id), \
                 patch.object(server.store, "save_index"), \
                 patch.object(server, "read_options", return_value={**options, "task_timeout_seconds": 30}), \
                 patch.object(server, "task_options", return_value=options), \
                 patch.object(server, "background_task", return_value=background), \
                 patch.object(server, "sandbox_readiness", return_value={"required": False, "ready": True}), \
                 patch.object(server, "codex_env", return_value={}), \
                 patch.object(server, "build_manifest", return_value={}), \
                 patch.object(server, "validate_changed_files", return_value=[]), \
                 patch.object(server, "write_task_log"), \
                 patch.object(server, "fire_ha_event", return_value=(True, "")), \
                 patch.object(server, "notify"), \
                 patch.object(server, "refresh_usage_status_async"), \
                 patch.object(server.subprocess, "Popen", return_value=proc) as popen:
                server.run_task(task_id, "Read the history")
                self.assertEqual(server.tasks[task_id]["status"], "completed", server.tasks[task_id].get("details"))
            popen.assert_called_once()
            return list(popen.call_args.args[0])
        finally:
            server.running_processes.pop(task_id, None)
            server.approval_sessions.pop(task_id, None)
            if saved is None:
                server.tasks.pop(task_id, None)
            else:
                server.tasks[task_id] = saved

    def test_every_allowed_mode_starts_the_app_server(self) -> None:
        """Default, Read only and Auto-review, attended and scheduled, all start `codex app-server`."""
        for sandbox in ("workspace-write", "read-only"):
            for background in (False, True):
                with self.subTest(sandbox=sandbox, background=background):
                    args = self.launch({"codex_sandbox": sandbox}, background=background)
                    self.assertEqual(args[:2], [server.CODEX_BINARY, "app-server"])
                    self.assertNotIn("exec", args)

    def test_a_refused_mode_starts_nothing(self) -> None:
        """A mode the policy refuses stops before any process starts, as before the exec path was removed."""
        task_id = "no-exec-refused"
        saved = server.tasks.get(task_id)
        server.tasks[task_id] = {"task_id": task_id, "status": "queued"}
        options = {"codex_sandbox": "danger-full-access"}
        try:
            with tempfile.TemporaryDirectory() as temp_dir, \
                 patch.object(server.store, "task_dir", return_value=Path(temp_dir) / task_id), \
                 patch.object(server.store, "save_index"), \
                 patch.object(server, "read_options", return_value=options), \
                 patch.object(server, "task_options", return_value=options), \
                 patch.object(server, "write_task_log"), \
                 patch.object(server, "fire_ha_event", return_value=(True, "")), \
                 patch.object(server, "notify"), \
                 patch.object(server.subprocess, "Popen") as popen:
                server.run_task(task_id, "Read the history")
            popen.assert_not_called()
            self.assertEqual(server.tasks[task_id]["status"], "failed")
        finally:
            if saved is None:
                server.tasks.pop(task_id, None)
            else:
                server.tasks[task_id] = saved


if __name__ == "__main__":
    unittest.main()
