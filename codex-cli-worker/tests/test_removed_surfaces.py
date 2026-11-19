"""Parts the add-on removed stay removed; each test fails if one comes back by mistake.

- 0.3.5 removed the add-on's own grant layer: the action groups, the permission overrides and the
  ``act`` tool (``home_assistant_actions``). No launch approves a house action ahead of Codex's own
  permissions.
- 0.3.6 (CX-SIMPLE.G) removed the add-on's own guards: the command check (hook) and its rules, the
  managed command-prefix rules, the refusal of named paths on the approval card, the check after
  each exchange, and the gate in front of remote control. Codex's modes and the sandbox are the
  protection. The GitHub read-only limit is covered in test_github_access.py
  (test_every_operation_reaches_the_runner_in_a_read_only_chat).
- The image bundles no house rules. The owner's own instructions are read and never changed, and
  the start-up clean-up removes only an exact old bundled copy.
"""

from __future__ import annotations

import hashlib
from itertools import pairwise
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomllib

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER))
import approvals
import policy
import remote_control
import test_approvals as fixtures

server = (
    fixtures.server
)  # the worker module as the approval fixtures loaded it (test_server's copy)

REMOVED_SERVER = "home_assistant_actions"
REMOVED_MODULES = (
    "permission_overrides",
    "permission_overrides_api",
    "permission_overrides_mcp",
)
# gate/managed_config.toml stays, with no hooks: it keeps the tool keys out of commands (test_ha_call_relay.py).
REMOVED_FILES = (
    "gate/gate_hook.py",
    "gate/remote-control-gate.json",
    "gate_rules.py",
    "shell_words.py",
    "policy/refused-domains.json",
)


def configured_servers(args: list[str]) -> set[str]:
    """Return the name of every MCP server that a Codex command line configures."""
    names = set()
    for flag, value in pairwise(args):
        if flag == "--config" and value.startswith("mcp_servers."):
            names.add(value[len("mcp_servers.") :].split(".", 1)[0])
    return names


class GrantLayerLaunchTests(unittest.TestCase):
    """0.3.5: no launch registers the act tool. Each way Codex starts: the chat page (and a schedule run, which starts the same way) and the ChatGPT app."""

    def page_launches(self) -> dict[str, list[str]]:
        task = {
            "task_id": "t",
            "turns": [],
            "chat_settings": server.DEFAULT_CHAT_SETTINGS,
        }
        with (
            patch.object(server, "tasks", {"t": task}),
            patch.object(server, "read_options", return_value={}),
        ):
            return {
                "page": server.build_app_server_args(),
                "schedule run": server.build_app_server_args(attended=False),
            }

    def test_no_page_or_schedule_launch_registers_the_act_tool(self):
        for name, args in self.page_launches().items():
            with self.subTest(launch=name):
                servers = configured_servers(args)
                # The other tool servers are still there, so the parsing above is not what makes this pass.
                self.assertIn("home_assistant", servers)
                self.assertNotIn(REMOVED_SERVER, servers)
                for module in REMOVED_MODULES:
                    self.assertFalse(any(module in arg for arg in args))

    def test_the_app_launch_registers_the_read_tool_and_the_call_tool_only(self):
        self.assertEqual(
            configured_servers(server.relay_tool_args()),
            {"home_assistant_relay", "home_assistant_call"},
        )

    def test_the_page_rules_no_longer_name_the_act_tool(self):
        self.assertNotIn(REMOVED_SERVER, server.PAGE_INSTRUCTIONS)
        self.assertNotIn(".act", server.PAGE_INSTRUCTIONS)


class GrantLayerPartsTests(unittest.TestCase):
    """0.3.5: the settings route, the modules and the overrides socket of the grant layer are gone."""

    def test_the_settings_route_is_gone(self):
        rules = {rule.rule for rule in server.app.url_map.iter_rules()}
        self.assertIn("/health", rules)
        self.assertNotIn("/permissions-overrides", rules)

    def test_no_module_of_the_layer_is_in_the_worker_or_the_image(self):
        for module in REMOVED_MODULES:
            with self.subTest(module=module):
                self.assertFalse((WORKER / f"{module}.py").exists())
        self.assertNotIn(
            "permission_overrides", (WORKER / "Dockerfile").read_text(encoding="utf-8")
        )

    def test_the_server_no_longer_opens_the_overrides_socket(self):
        source = (WORKER / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("permission-overrides.sock", source)
        self.assertNotIn("override_service", source)

    def test_the_approval_session_has_no_owner_action_request(self):
        self.assertFalse(hasattr(approvals.AppServerSession, "ask_owner_action"))
        self.assertFalse(hasattr(approvals, "ACTION_ANSWER_SECONDS"))


class GuardImageTests(unittest.TestCase):
    """0.3.6: the image ships no hook, no rule files and no gate marker."""

    def test_the_guard_files_are_gone_from_the_source_and_the_dockerfile(self) -> None:
        dockerfile = (WORKER / "Dockerfile").read_text()
        for relative in REMOVED_FILES:
            with self.subTest(file=relative):
                self.assertFalse((WORKER / relative).exists())
                self.assertNotIn(Path(relative).name, dockerfile)

    def test_the_managed_requirements_hold_the_modes_and_no_rules_or_hooks(
        self,
    ) -> None:
        data = tomllib.loads((WORKER / "gate/requirements.toml").read_text())
        self.assertEqual(
            data["allowed_sandbox_modes"], ["read-only", "workspace-write"]
        )
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


class GuardApprovalCardTests(unittest.TestCase):
    """0.3.6: the card refuses no command by name: a command shown in full can be allowed once."""

    def setUp(self) -> None:
        # A live, initialized turn from test_approvals.py, used here as a fixture.
        self.turn = fixtures.ApprovalTests(
            "test_masked_reason_does_not_hide_the_action"
        )
        self.turn.setUp()
        self.addCleanup(self.turn.doCleanups)

    def test_commands_naming_the_apps_files_can_be_allowed(self) -> None:
        for index, command in enumerate(
            (
                "cat /data/options.json",
                "cat /etc/codex/requirements.toml",
                "ha supervisor info",
                "ls /data/github",
            )
        ):
            with self.subTest(command=command):
                self.turn.request(request_id=index, command=command)
                record = self.turn.records[-1]
                self.assertTrue(record["can_allow"])
                self.assertNotIn("permission policy", record["note"])
        for name in (
            "touches_protected_path",
            "command_names_protected_path",
            "PROTECTED_PREFIXES",
        ):
            self.assertFalse(hasattr(approvals, name), name)


class GuardExchangeTests(unittest.TestCase):
    """0.3.6: no check after an exchange: an edit that adds a shell command stays as Codex wrote it."""

    def setUp(self) -> None:
        # A worker with a fake Codex process from test_approvals.py, used here as a fixture.
        self.run_fixture = fixtures.ApprovalRunTests(
            "test_failed_turn_still_reviews_edits_and_recovery"
        )
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
        fixture.stack.enter_context(
            patch.object(server, "build_manifest", fixture.real_build_manifest)
        )
        proc = fixture.process(
            failure="no-final",
            on_turn=lambda: target.write_text(
                "shell_command:\n  sample: echo fixture\n"
            ),
        )
        fixture.stack.enter_context(
            patch.object(server.subprocess, "Popen", return_value=proc)
        )
        server.run_task("task", "Edit the sample configuration")
        task = server.tasks["task"]
        self.assertIn("shell_command", target.read_text())
        self.assertNotIn("refused_edits", task)
        self.assertNotIn("unchecked_edits", task)
        self.assertNotIn("refused actions were undone", str(task.get("summary")))
        self.assertFalse(hasattr(server, "refuse_planted_edits"))


class GuardRemoteControlTests(unittest.TestCase):
    """0.3.6: turn on starts the process with no check of the app's own in front of it."""

    def test_the_supervisor_has_no_gate_or_watch(self) -> None:
        for name in (
            "gate_problem",
            "hook_problem",
            "requirements_problem",
            "managed_exec_rules_problem",
            "exec_rules_self_test",
            "record_exec_rules_warning",
            "ConfigWatch",
            "GATE_MARKER",
        ):
            self.assertFalse(hasattr(remote_control, name), name)
        self.assertNotIn("review", remote_control.Supervisor.__dict__)


class HouseRulesTests(unittest.TestCase):
    """The image bundles no house rules; the owner's own instructions are read and left unchanged."""

    def test_image_does_not_bundle_house_instructions(self):
        self.assertFalse((WORKER / "policy/house-rules.md").exists())
        self.assertNotIn(
            "COPY policy/house-rules.md", (WORKER / "Dockerfile").read_text()
        )

    def test_startup_preserves_owner_home_instructions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            home.mkdir()
            instructions = home / "AGENTS.md"
            instructions.write_text("Owner instructions, unchanged.")
            with (
                patch.multiple(
                    server,
                    CODEX_HOME=home,
                    CODEX_CONFIG_PATH=home / "config.toml",
                    DATA_ROOT=root,
                    AUTH_QR_DIR=root / "qr",
                ),
                patch.object(server, "task_root", return_value=root / "tasks"),
                patch.object(server, "api_token", return_value="synthetic"),
            ):
                server.ensure_runtime_files()
            self.assertEqual(instructions.read_text(), "Owner instructions, unchanged.")

    def test_migration_removes_only_exact_legacy_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            target = home / "AGENTS.md"
            old = b"synthetic old bundled copy"
            with patch.object(
                policy,
                "LEGACY_INSTRUCTIONS_SHA256",
                frozenset({hashlib.sha256(old).hexdigest()}),
            ):
                target.write_bytes(old)
                target.chmod(0o444)
                self.assertTrue(policy.remove_legacy_instructions(home))
                self.assertFalse(target.exists())
                self.assertFalse(policy.remove_legacy_instructions(home))
                target.write_text("Edited by owner")
                self.assertFalse(policy.remove_legacy_instructions(home))
                self.assertEqual(target.read_text(), "Edited by owner")

    def test_migration_never_follows_a_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "owner.md"
            target.write_text("synthetic owner text")
            home = root / "home"
            home.mkdir()
            (home / "AGENTS.md").symlink_to(target)
            self.assertFalse(policy.remove_legacy_instructions(home))
            self.assertEqual(target.read_text(), "synthetic owner text")

    def test_config_agents_read_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "AGENTS.md"
            target.write_text("Owner workspace instructions")
            with patch.object(server, "AGENTS_PATH", target):
                self.assertEqual(
                    server.read_agents_file(), "Owner workspace instructions"
                )

    def test_task_context_keeps_owner_pointer_and_original_upstream_guidance(self):
        text = server.PAGE_INSTRUCTIONS
        self.assertIn(
            "Owner workspace instructions: if /config/AGENTS.md exists, follow it.",
            text,
        )
        self.assertIn(
            "Treat this as a live Home Assistant config tree. Make focused edits, do not expose secrets, "
            "and validate changed YAML/JSON when practical. After you finish, the worker asks Home "
            "Assistant to check its configuration whenever YAML files changed; if that check fails, the "
            "task is reported as failed, so prefer a change you are confident is valid over a "
            "speculative one.",
            text,
        )

    def test_page_rules_are_the_same_with_and_without_the_owner_file(self):
        # Codex reads the page's rules once for each thread, and the owner can add or remove
        # /config/AGENTS.md between two messages, so the rules must not depend on the file.
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(server, "AGENTS_PATH", Path(folder) / "AGENTS.md"),
            patch.object(server, "attached_image_note", return_value=""),
            patch.object(server, "backup_instructions", return_value=""),
            patch.object(server, "home_assistant_api_instructions", return_value=""),
            patch.object(server, "ha_docs_note", return_value=""),
            patch.object(server, "read_options", return_value={}),
        ):
            absent = server.exchange_info("synthetic", None, include_rules=True)[
                "rules"
            ]
            server.AGENTS_PATH.write_text("Synthetic owner rules")
            present = server.exchange_info("synthetic", None, include_rules=True)[
                "rules"
            ]
        self.assertEqual(absent, present)
        self.assertEqual(present, server.PAGE_INSTRUCTIONS)

    def test_optional_starter_reference_is_plain_published_separately_text(self):
        docs = (WORKER / "DOCS.md").read_text()
        paragraph = next(
            line
            for line in docs.splitlines()
            if line.startswith("**Protected policy.**")
        )
        self.assertIn(
            "the Whole Home Agent starter kit (published separately)", paragraph
        )
        self.assertNotIn("https://", paragraph)
        self.assertNotIn("whole_home", paragraph)

    def test_readmes_describe_defaults_and_owner_instructions(self):
        for path in (WORKER.parent / "README.md", WORKER / "README.md"):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertNotIn("Shipped practice covers", text)
                self.assertIn(
                    "Codex uses its defaults and your workspace instructions", text
                )
                self.assertIn("the app bundles no house rules", text)
        version = next(
            line
            for line in (WORKER.parent / "README.md").read_text().splitlines()
            if line.startswith("| 0.1.81.2 ")
        )
        self.assertNotIn("policy and house rules in the image", version)
        self.assertIn("no bundled house rules", version)

    def test_reviewed_document_sections_have_one_blank_line(self):
        docs = (WORKER / "DOCS.md").read_text()
        paragraph = next(
            line
            for line in docs.splitlines()
            if line.startswith("**Protected policy.**")
        )
        self.assertIn(paragraph + "\n\n**Who can answer.**", docs)
        changelog = (WORKER / "CHANGELOG.md").read_text()
        entry = next(
            line
            for line in changelog.splitlines()
            if line.startswith("- **No bundled house rules")
        )
        self.assertIn(entry + "\n\n- **HAOS AppArmor", changelog)


if __name__ == "__main__":
    unittest.main()
