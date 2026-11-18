"""0.3.6 (CX-SIMPLE.G): the add-on's own guards are gone; Codex's modes and the sandbox are the protection.

Each test proves one removal from the card: the command check (hook) and its rules, the managed
command-prefix rules, the refusal of named paths on the approval card, the check after each
exchange, and the gate in front of remote control. The GitHub read-only limit is covered in
test_github_access.py (test_every_operation_reaches_the_runner_in_a_read_only_chat).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tomllib

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER))
import test_approvals as fixtures

import approvals
import remote_control

server = fixtures.server  # the worker module as the approval fixtures loaded it

# gate/managed_config.toml stays, with no hooks: it keeps the tool keys out of commands (test_ha_call_relay.py).
REMOVED_FILES = ("gate/gate_hook.py", "gate/remote-control-gate.json",
                 "gate_rules.py", "shell_words.py", "policy/refused-domains.json")


class ImageTests(unittest.TestCase):
    """The image ships no hook, no rule files and no gate marker."""

    def test_the_guard_files_are_gone_from_the_source_and_the_dockerfile(self) -> None:
        dockerfile = (WORKER / "Dockerfile").read_text()
        for relative in REMOVED_FILES:
            with self.subTest(file=relative):
                self.assertFalse((WORKER / relative).exists())
                self.assertNotIn(Path(relative).name, dockerfile)

    def test_the_managed_requirements_hold_the_modes_and_no_rules_or_hooks(self) -> None:
        data = tomllib.loads((WORKER / "gate/requirements.toml").read_text())
        self.assertEqual(data["allowed_sandbox_modes"], ["read-only", "workspace-write"])
        self.assertEqual(data["allowed_approval_policies"], ["on-request"])
        # With no managed hooks, this keeps every hook from running: a hook runs outside the sandbox.
        self.assertIs(data["allow_managed_hooks_only"], True)
        self.assertNotIn("rules", data)
        self.assertNotIn("hooks", data)

    def test_no_worker_module_imports_the_removed_checks(self) -> None:
        for path in WORKER.glob("*.py"):
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn("import gate_rules", text)
                self.assertNotIn("shell_words", text)


class ApprovalCardTests(unittest.TestCase):
    """The card refuses no command by name: a command shown in full can be allowed once."""

    def setUp(self) -> None:
        # A live, initialized turn from test_approvals.py, used here as a fixture.
        self.turn = fixtures.ApprovalTests("test_masked_reason_does_not_hide_the_action")
        self.turn.setUp()
        self.addCleanup(self.turn.doCleanups)

    def test_commands_naming_the_apps_files_can_be_allowed(self) -> None:
        for index, command in enumerate(("cat /data/options.json", "cat /etc/codex/requirements.toml",
                                         "ha supervisor info", "ls /data/github")):
            with self.subTest(command=command):
                self.turn.request(request_id=index, command=command)
                record = self.turn.records[-1]
                self.assertTrue(record["can_allow"])
                self.assertNotIn("permission policy", record["note"])
        for name in ("touches_protected_path", "command_names_protected_path", "PROTECTED_PREFIXES"):
            self.assertFalse(hasattr(approvals, name), name)


class ExchangeTests(unittest.TestCase):
    """No check after an exchange: an edit that adds a shell command stays as Codex wrote it."""

    def setUp(self) -> None:
        # A worker with a fake Codex process from test_approvals.py, used here as a fixture.
        self.run_fixture = fixtures.ApprovalRunTests("test_failed_turn_still_reviews_edits_and_recovery")
        self.run_fixture.setUp()
        self.addCleanup(self.run_fixture.doCleanups)

    def test_a_planted_shell_command_is_left_in_place(self) -> None:
        fixture = self.run_fixture
        config = fixture.root / "config"
        config.mkdir()
        target = config / "sample.yaml"
        target.write_text("- action: light.turn_on\n")
        fixture.options["full_snapshot"] = True
        fixture.stack.enter_context(patch.object(server, "CONFIG_ROOT", config))
        fixture.stack.enter_context(patch.object(server, "build_manifest", fixture.real_build_manifest))
        proc = fixture.process(failure="no-final",
                               on_turn=lambda: target.write_text("shell_command:\n  sample: echo fixture\n"))
        fixture.stack.enter_context(patch.object(server.subprocess, "Popen", return_value=proc))
        server.run_task("task", "Edit the sample configuration")
        task = server.tasks["task"]
        self.assertIn("shell_command", target.read_text())
        self.assertNotIn("refused_edits", task)
        self.assertNotIn("unchecked_edits", task)
        self.assertNotIn("refused actions were undone", str(task.get("summary")))
        self.assertFalse(hasattr(server, "refuse_planted_edits"))


class RemoteControlTests(unittest.TestCase):
    """Turn on starts the process with no check of the app's own in front of it."""

    def test_the_supervisor_has_no_gate_or_watch(self) -> None:
        for name in ("gate_problem", "hook_problem", "requirements_problem", "managed_exec_rules_problem",
                     "exec_rules_self_test", "record_exec_rules_warning", "ConfigWatch", "GATE_MARKER"):
            self.assertFalse(hasattr(remote_control, name), name)
        self.assertNotIn("review", remote_control.Supervisor.__dict__)


if __name__ == "__main__":
    unittest.main()
