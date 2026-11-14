"""Command text the shell splitter cannot read is still checked, never skipped.

A heredoc whose body holds an apostrophe leaves an unfinished quote for Python's
``shlex``, although the shell runs the whole command. The hook reads such text again
line by line, so the lines it can read keep their quoting, and its always-refused
Supervisor rule also has a text floor that sees ``ha``, its global flags and a family
name spelled with quotes or backslashes. The approval check fails closed instead: a
command it cannot read with its quoting intact is never offered Allow once. A ``#``
starts a comment only at the start of a word, as it does for the shell.
"""

from __future__ import annotations

import io
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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


class WrapperAndCommentTests(unittest.TestCase):
    """A quoted script for a shell wrapper and a word holding ``#`` are read as the shell reads them."""

    WRAPPED = "sh -c 'h\\a su info'"
    HASH_IN_WORD = ("echo a#b; h\\a su info", "echo a\\ #b; h\\a su info")

    def test_wrapped_supervisor_command_after_an_unfinished_quote(self) -> None:
        self.assertTrue(gate_rules._ha_supervisor_command(HEREDOC + self.WRAPPED))
        for remote_on in (False, True):
            with self.subTest(remote_on=remote_on):
                self.assertIn("Supervisor", refusal(HEREDOC + self.WRAPPED, remote_on))

    def test_wrapped_write_after_an_unfinished_quote(self) -> None:
        self.assertTrue(approvals._protected_ancestor_operations(HEREDOC + "sh -c 'cp x /etc/'"))

    def test_wrapped_write_whose_script_spans_lines(self) -> None:
        """The script's closing quote on the next line: read whole, or not offered Allow once."""
        command = "sh -c 'cp x /etc/\n'"
        for prefix in ("", HEREDOC):
            with self.subTest(heredoc=bool(prefix)):
                self.assertTrue(approvals._protected_ancestor_operations(prefix + command))
                self.assertTrue(approvals.command_names_protected_path(prefix + command))

    def test_hash_inside_a_word_does_not_hide_the_rest_of_the_line(self) -> None:
        for command in self.HASH_IN_WORD:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_supervisor_command(command))
                self.assertTrue(gate_rules._ha_command(command.replace("su info", "core restart"), True))
        self.assertTrue(approvals._protected_ancestor_operations("echo a#b; cp x /etc/"))

    def test_a_real_comment_is_still_a_comment(self) -> None:
        for prefix in ("", HEREDOC):
            with self.subTest(heredoc=bool(prefix)):
                self.assertFalse(gate_rules._ha_supervisor_command(prefix + "ls # note; h\\a su info"))
        self.assertFalse(approvals._protected_ancestor_operations("ls # note; cp x /etc/"))

    def test_hook_floor_refuses_a_comment_naming_supervisor(self) -> None:
        """Known over-refusal (DOCS): the text floor does not know shell comments, so it refuses this."""
        for prefix in ("", HEREDOC):
            for remote_on in (False, True):
                with self.subTest(heredoc=bool(prefix), remote_on=remote_on):
                    self.assertIn("Supervisor", refusal(prefix + "echo hi # note; h\\a su info", remote_on))

    def test_text_floor_reads_through_backslashes(self) -> None:
        """With both parsers silenced, the floor alone still refuses an escaped spelling."""
        with (
            patch.object(gate_rules, "_ha_supervisor_command", return_value=False),
            patch.object(gate_rules, "_ha_command", return_value=False),
        ):
            for remote_on in (False, True):
                with self.subTest(remote_on=remote_on):
                    self.assertIn("Supervisor", refusal("h\\a s\\u info", remote_on))


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

    def test_unreadable_command_counts_as_protected(self) -> None:
        """Fail closed: after an unfinished quote even an ordinary command is not offered Allow once."""
        for command in ("ls -la", "cp a.yaml b.yaml", "tar -xf backup.tar"):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))
                self.assertFalse(approvals.command_names_protected_path("cat <<EOF\nit is\nEOF\n" + command))
                self.assertTrue(approvals.command_names_protected_path(HEREDOC + command))


class ApprovalRecordTests(unittest.TestCase):
    """The approval record a person sees: no Allow once for a command the check cannot read."""

    def record(self, command: str) -> dict:
        records: list[dict] = []
        session = approvals.AppServerSession(
            io.StringIO(), lock=threading.RLock(),
            thread_params={"sandbox": "workspace-write", "approvalPolicy": "on-request", "approvalsReviewer": "user"},
            turn_params={"input": [{"type": "text", "text": "Copy a file"}]},
            on_event=lambda event: None, on_approvals=lambda latest: records.extend(latest),
            on_final=lambda final: None, is_active=lambda: True, redact=lambda text: text,
        )
        session.start()
        session.receive({"id": "worker-1", "result": {}})
        session.receive({"id": "worker-2", "result": {
            "thread": {"id": "thread"}, "approvalPolicy": "on-request", "approvalsReviewer": "user"}})
        session.receive({"id": "worker-3", "result": {"turn": {"id": "turn"}}})
        session.receive({"id": 0, "method": approvals.COMMAND_APPROVAL, "params": {
            "threadId": "thread", "turnId": "turn", "itemId": "item", "startedAtMs": 1,
            "command": command, "cwd": "/config", "reason": "Copy a file"}})
        return records[-1]

    def test_multiline_wrapped_write_has_no_allow_once(self) -> None:
        for prefix in ("", HEREDOC):
            with self.subTest(heredoc=bool(prefix)):
                record = self.record(prefix + "sh -c 'cp x /etc/\n'")
                self.assertFalse(record["can_allow"])
                self.assertIn("permission policy", record["note"])

    def test_readable_ordinary_command_keeps_allow_once(self) -> None:
        self.assertTrue(self.record("cat <<EOF\nit is\nEOF\ncp a.yaml b.yaml")["can_allow"])


if __name__ == "__main__":
    unittest.main()
