"""Command text the shell splitter cannot read is still checked, never skipped.

A heredoc whose body holds an apostrophe leaves an unfinished quote for Python's
``shlex``, although the shell runs the whole command. Both command checks read such
text again with its quotes removed, and the hook's always-refused Supervisor rule
also has a text floor that sees ``ha``, its global flags and a quoted family name.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import approvals
import gate_rules

RULES = gate_rules.load_rules(ROOT / "policy" / "refused-domains.json")
# The shell runs the lines after this heredoc; shlex stops at the apostrophe.
HEREDOC = "cat <<EOF\nit's\nEOF\n"

SUPERVISOR_COMMANDS = (
    'ha "su" info',
    "ha --log-level debug supervisor restart",
    "/usr/bin/ha 'su' info",
    'ha --log-level "debug" "supervisor" restart',
    'sudo ha "supervisor" info',
    'ha -- "su" info',
)
ETC_WRITES = (
    "cp x /etc/",
    'cp x "/etc/"',
    "cd /etc && cp x .",
    "tar -C /etc -xf a.tar",
)


def refusal(command: str, remote_on: bool) -> str:
    return gate_rules.check_tool_call("PreToolUse", {"tool_input": {"command": command}}, remote_on, RULES)


class SupervisorRuleTests(unittest.TestCase):
    """The always-refused Supervisor rule holds after an unfinished quote, remote control on or off."""

    def test_hook_refuses_supervisor_commands_after_an_unfinished_quote(self) -> None:
        for command in SUPERVISOR_COMMANDS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn("Supervisor", refusal(HEREDOC + command, remote_on))
                    self.assertIn("Supervisor", refusal(command, remote_on))

    def test_parser_reads_the_command_again_without_quotes(self) -> None:
        for command in SUPERVISOR_COMMANDS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_supervisor_command(HEREDOC + command))

    def test_text_floor_sees_flags_and_quoted_family(self) -> None:
        for command in SUPERVISOR_COMMANDS:
            plain = command.replace('"', "").replace("'", "")
            with self.subTest(command=command):
                self.assertTrue(gate_rules.HA_ALWAYS_TEXT_RE.search(plain))

    def test_text_floor_leaves_other_words_alone(self) -> None:
        for text in ("ha core info", "aloha supervisor", "ha supervisors", "sha su", "ha subscribe"):
            with self.subTest(text=text):
                self.assertIsNone(gate_rules.HA_ALWAYS_TEXT_RE.search(text))


class RemoteFamilyTests(unittest.TestCase):
    """The remote-control families keep their answers after an unfinished quote."""

    def test_remote_family_follows_remote_control(self) -> None:
        self.assertEqual(refusal(HEREDOC + 'ha "core" restart', False), "")
        self.assertIn("Supervisor", refusal(HEREDOC + 'ha "core" restart', True))

    def test_hash_inside_quotes_does_not_hide_the_rest_of_the_line(self) -> None:
        command = HEREDOC + 'echo "#"; ha "core" restart'
        self.assertTrue(gate_rules._ha_command(command, True))
        self.assertTrue(gate_rules._ha_supervisor_command(HEREDOC + 'echo "#"; ha "su" info'))

    def test_trailing_backslash_is_still_read(self) -> None:
        self.assertTrue(gate_rules._ha_supervisor_command('ha "su" info \\'))
        self.assertTrue(gate_rules._ha_command('ha "core" restart \\', True))

    def test_harmless_commands_keep_no_opinion(self) -> None:
        for command in ("ls -la", "git log --oneline"):
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertEqual(refusal(HEREDOC + command, remote_on), "")
        self.assertEqual(refusal(HEREDOC + "ha core info", False), "")


class ApprovalProtectedWriteTests(unittest.TestCase):
    """A write into /etc is still named after an unfinished quote, so approval refuses it."""

    def test_etc_writes_after_an_unfinished_quote(self) -> None:
        for command in ETC_WRITES:
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))
                self.assertTrue(approvals._protected_ancestor_operations(HEREDOC + command))
                self.assertTrue(approvals.command_names_protected_path(HEREDOC + command))

    def test_hash_inside_quotes_does_not_hide_a_write(self) -> None:
        self.assertTrue(approvals._protected_ancestor_operations(HEREDOC + 'echo "#"; cp x /etc/'))

    def test_ordinary_commands_after_an_unfinished_quote(self) -> None:
        for command in ("ls -la", "cp a.yaml b.yaml", "tar -xf backup.tar"):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(HEREDOC + command))


if __name__ == "__main__":
    unittest.main()
