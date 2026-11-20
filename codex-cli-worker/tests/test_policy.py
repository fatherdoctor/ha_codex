"""The protected permission policy (CG-1, CG-4): no full access, no approval-free policy, rules put back at start."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server
import policy


class ProtectedPolicyTests(unittest.TestCase):
    """The shipped policy, the rules it cannot be loosened past, and what the worker does with it."""

    def setUp(self) -> None:
        """Give each test its own folder for policy files and a Codex home."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_policy(self, **changes) -> Path:
        """Write the shipped policy with some fields changed and return its path."""
        data = json.loads(policy.POLICY_PATH.read_text(encoding="utf-8"))
        data.update(changes)
        path = self.root / "policy.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_shipped_policy_is_the_strict_one(self) -> None:
        """The policy in the repository loads and allows only the two sandboxed app options."""
        loaded = policy.load_policy()
        self.assertEqual(loaded["allowed_sandbox_modes"], ["workspace-write", "read-only"])
        self.assertEqual(policy.policy_problem({}), "")
        self.assertEqual(policy.policy_problem({"codex_sandbox": "read-only"}), "")

    def test_full_access_is_refused(self) -> None:
        """danger-full-access, or any mode the policy does not list, is a problem with a plain reason."""
        for mode in ("danger-full-access", "unexpected"):
            with self.subTest(mode=mode):
                self.assertIn("not allowed", policy.policy_problem({"codex_sandbox": mode}))

    def test_a_malformed_sandbox_list_is_refused(self) -> None:
        """A policy file without a usable list of sandbox modes is itself refused."""
        for changes in (
            {"allowed_sandbox_modes": []},
            {"allowed_sandbox_modes": "workspace-write"},
            {"allowed_sandbox_modes": [1]},
        ):
            with self.subTest(changes=changes):
                path = self.write_policy(**changes)
                with self.assertRaises(policy.PolicyError):
                    policy.load_policy(path)
                self.assertTrue(policy.policy_problem({}, path))

    def test_missing_or_broken_policy_needs_support(self) -> None:
        """Without a readable policy file every option is refused."""
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        for path in (self.root / "missing.json", broken):
            with self.subTest(path=path.name):
                self.assertTrue(policy.policy_problem({}, path))

    def test_owner_home_instructions_are_not_replaced(self) -> None:
        home = self.root / "codex-home"
        home.mkdir()
        target = home / "AGENTS.md"
        target.write_text("Owner instructions.", encoding="utf-8")
        self.assertFalse(policy.remove_legacy_instructions(home))
        self.assertEqual(target.read_text(), "Owner instructions.")

    def test_runtime_config_never_says_never(self) -> None:
        """The Codex configuration the worker writes asks on request and has no approval-free profile."""
        home = self.root / "codex-home"
        with (
            patch.object(server, "CODEX_HOME", home),
            patch.object(server, "CODEX_CONFIG_PATH", home / "config.toml"),
            patch.object(server, "DATA_ROOT", self.root),
            patch.object(server, "OLD_AUTH_QR_DIR", self.root / "qr"),
            patch.object(server, "task_root", return_value=self.root / "tasks"),
            patch.object(server, "api_token", return_value="x" * 40),
        ):
            server.ensure_runtime_files()
        config = (home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('approval_policy = "on-request"', config)
        self.assertNotIn('"never"', config)
        self.assertNotIn("[profiles.", config)
        self.assertFalse((home / "AGENTS.md").exists())

    def test_startup_stops_on_a_refused_mode(self) -> None:
        """An older option naming full access stops the worker before it prepares or serves anything."""
        with (
            patch.object(server, "read_options", return_value={"codex_sandbox": "danger-full-access"}),
            patch.object(server, "ensure_runtime_files") as prepare,
            patch.object(server.app, "run") as serve,
        ):
            with self.assertRaises(SystemExit) as stopped:
                server.main()
        self.assertEqual(stopped.exception.code, 1)
        prepare.assert_not_called()
        serve.assert_not_called()

    def test_refused_mode_never_launches_codex(self) -> None:
        """A task under a refused mode fails as "needs support" and no Codex process starts."""
        with (
            patch.object(server, "read_options", return_value={"codex_sandbox": "danger-full-access"}),
            patch.object(server, "tasks", {"task": {"task_id": "task", "status": "queued", "turns": []}}),
            patch.object(server, "running_processes", {}),
            patch.object(server, "get_run_dir", return_value=self.root / "run"),
            patch.object(server, "get_task_dir", side_effect=lambda task_id: self.root / "tasks" / task_id),
            patch.object(server, "save_task_index"),
            patch.object(server, "write_task_log"),
            patch.object(server, "record_baseline", return_value={}) as baseline,
            patch.object(server, "prepare_ha_docs"),
            patch.object(server, "page_message", return_value="prompt"),
            patch.object(server, "publish_task_outcome", create=True),
            patch.object(server, "notify", create=True),
            patch.object(server.subprocess, "Popen") as popen,
        ):
            server.run_task("task", "Turn the lamp on")
            status = server.tasks["task"].get("status")
            summary = server.tasks["task"].get("summary", "")
        popen.assert_not_called()
        baseline.assert_not_called()
        self.assertEqual(status, "failed")
        self.assertIn("needs support", summary)


if __name__ == "__main__":
    unittest.main()
