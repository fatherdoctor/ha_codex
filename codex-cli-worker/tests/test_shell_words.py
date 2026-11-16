"""Both command checks read words with one copy of the code, and the image ships that copy to both.

The approval card (approvals.py) and the remote-control hook (gate_rules.py) used to hold
byte-identical copies of the comment stripper and the word splitter. A fix to one copy
would have left the other reading the same command differently. They now import both
from shell_words.py; only their answer to unreadable text differs, on purpose.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import approvals
import gate_rules
import shell_words
from test_gate_quote import ALLOWED, HEREDOC, REFUSED

PUNCTUATION = ";&|()\n<>"
UNREADABLE_NOTE = "could not read this command's quoting or heredoc safely"
POLICY_NOTE = "touches the app's permission policy or settings"


class OneCopyTests(unittest.TestCase):
    """The two checks use the same functions, not two copies that could drift apart."""

    def test_both_checks_use_the_shared_functions(self) -> None:
        for module in (approvals, gate_rules):
            with self.subTest(module=module.__name__):
                self.assertIs(module.strip_comments, shell_words.strip_comments)
                self.assertIs(module.split_words, shell_words.split_words)
                for name in ("WORD_BREAKS", "_strip_comments", "_split_words"):
                    self.assertFalse(hasattr(module, name), name)

    def test_the_hook_joins_lines_with_the_shared_function(self) -> None:
        """The continuation join reads comments too, so it lives in the shared file, not in a third copy."""
        self.assertIs(gate_rules._join_shell_continuations, shell_words.join_shell_continuations)

    def test_both_checks_read_readable_text_the_same_way(self) -> None:
        """Where the text can be read, the two checks give the same words; they differ only on unreadable text."""
        for name, command in {**REFUSED, **ALLOWED}.items():
            try:
                expected = approvals._shell_words(command, PUNCTUATION)
            except ValueError:
                continue
            with self.subTest(name):
                self.assertEqual(gate_rules._shell_words(command, PUNCTUATION), expected)

    def test_unreadable_text_keeps_each_check_s_own_answer(self) -> None:
        """The approval check fails closed; the hook reads the text again line by line."""
        with self.assertRaises(ValueError):
            approvals._shell_words(HEREDOC, PUNCTUATION)
        self.assertIn("EOF", gate_rules._shell_words(HEREDOC, PUNCTUATION))


class ImageTests(unittest.TestCase):
    """The image installs the shared module next to the worker and next to the hook."""

    def test_the_dockerfile_copies_it_to_both_places(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        hook_copy = next(line for line in dockerfile.splitlines() if "gate/gate_hook.py" in line)
        worker_copy = next(line for line in dockerfile.splitlines() if " approvals.py " in line)
        self.assertIn(" shell_words.py ", hook_copy)
        self.assertTrue(hook_copy.endswith(" /usr/share/ha_codex/"), hook_copy)
        self.assertIn(" shell_words.py ", worker_copy)
        self.assertTrue(worker_copy.endswith(" /"), worker_copy)

    def test_the_gate_marker_lists_it(self) -> None:
        """Remote control cannot be turned on while a file the hook imports is missing."""
        marker = json.loads((ROOT / "gate" / "remote-control-gate.json").read_text(encoding="utf-8"))
        self.assertIn("/usr/share/ha_codex/shell_words.py", marker["files"])


class ApprovalNoteTests(unittest.TestCase):
    """The card says why a command is refused Allow once: unreadable text, or a protected place."""

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

    def test_a_harmless_heredoc_is_refused_for_its_quoting_not_for_a_policy_edit(self) -> None:
        for command in ("cat <<EOF\nit is\nEOF\n", HEREDOC + "cp a.yaml b.yaml", "grep -c x <<< 'it is'", "ls \\\n-la"):
            with self.subTest(command=command):
                record = self.record(command)
                self.assertFalse(record["can_allow"])
                self.assertIn(UNREADABLE_NOTE, record["note"])
                self.assertNotIn(POLICY_NOTE, record["note"])

    def test_a_readable_command_that_names_the_policy_keeps_the_policy_note(self) -> None:
        for command in ("cp x /etc/", "sh -c 'cp x /etc/'", "cat /data/options.json"):
            with self.subTest(command=command):
                record = self.record(command)
                self.assertFalse(record["can_allow"])
                self.assertIn(POLICY_NOTE, record["note"])

    def test_a_command_that_is_not_text_is_read_as_text(self) -> None:
        """Like command_names_protected_path, the check turns any command into text before it reads it."""
        self.assertFalse(approvals.command_is_unreadable(["cat /data/options.json", 5]))
        self.assertTrue(approvals.command_is_unreadable(["cat <<EOF", 5]))
        self.assertFalse(approvals.command_is_unreadable(None))


if __name__ == "__main__":
    unittest.main()
