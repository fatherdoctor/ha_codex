"""Command text the shell splitter cannot read is still checked, never skipped.

A heredoc whose body holds an apostrophe leaves an unfinished quote for Python's
``shlex``, although the shell runs the whole command. The hook reads such text again
line by line, so the lines it can read keep their quoting, and its always-refused
Supervisor rule also has a text floor that sees ``ha``, its global flags and a family
name spelled with quotes or backslashes. The approval check fails closed instead: a
command with a heredoc, a here-string or a backslash-newline, or one it cannot read with
its quoting intact, is never offered Allow once. A ``#``
starts a comment only at the start of a word, as it does for the shell.
"""

from __future__ import annotations

import io
import os
import sys
import subprocess
import tempfile
import threading
import time
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
        """Fail closed: a heredoc, readable or not, or an unfinished quote, means no Allow once."""
        for command in ("ls -la", "cp a.yaml b.yaml", "tar -xf backup.tar"):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))
                self.assertTrue(approvals.command_names_protected_path("cat <<EOF\nit is\nEOF\n" + command))
                self.assertTrue(approvals.command_names_protected_path(HEREDOC + command))


# A shell comment holding one apostrophe, after the command. Before this rule the splitter
# paired it with the apostrophe in HEREDOC's body and read the command as heredoc text.
QUOTED_COMMENT = "\n# don't"

# Cases from an independent review of this check, plus here-string and backslash-newline
# forms: every write into /etc and every command with << is refused Allow once; readable
# commands without << keep it.
REFUSED = {
    "wrapper": "sh -c 'cp x /etc/'",
    "wrapper after heredoc": HEREDOC + "sh -c 'cp x /etc/'",
    "multiline wrapper": "sh -c 'cp x /etc/\n'",
    "multiline wrapper after heredoc": HEREDOC + "sh -c 'cp x /etc/\n'",
    "double-quoted multiline wrapper after heredoc": HEREDOC + 'sh -c "cp x /etc/\n"',
    "plain write after heredoc": HEREDOC + "cp x /etc/",
    "write joined by backslash-newline": "cp x \\\n/etc/",
    "multiline wrapper, heredoc, quoted comment": HEREDOC + "sh -c 'cp x /etc/\n'" + QUOTED_COMMENT,
    "wrapper, heredoc, quoted comment": HEREDOC + "sh -c 'cp x /etc/'" + QUOTED_COMMENT,
    "double-quoted wrapper, heredoc, quoted comment": HEREDOC + 'sh -c "cp x /etc/\n"' + QUOTED_COMMENT,
    "write between two heredocs": HEREDOC + "sh -c 'cp x /etc/\n'\n" + HEREDOC,
    "ordinary copy after an unreadable heredoc": HEREDOC + "cp a.yaml b.yaml",
    "ordinary copy after a readable heredoc": "cat <<EOF\nit is\nEOF\ncp a.yaml b.yaml",
    "here-string": "grep -c x <<< 'it is'",
    "ordinary command joined by backslash-newline": "ls \\\n-la",
}
ALLOWED = {
    "quoted multiline message, then a copy": 'echo "line one\nline two"\ncp a.yaml b.yaml',
    "multiline wrapper script": "sh -c 'echo one\necho two'",
    "quoted apostrophe": 'echo "it\'s"',
    "real comment with an apostrophe": "cp a.yaml b.yaml # don't",
}


class HeredocStructureTests(unittest.TestCase):
    """The approval check refuses heredocs by structure instead of trying to read their bodies."""

    def test_refused_commands_through_both_predicates(self) -> None:
        for name, command in REFUSED.items():
            with self.subTest(name):
                self.assertTrue(approvals._protected_ancestor_operations(command))
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_readable_commands_without_heredoc_are_not_protected(self) -> None:
        for name, command in ALLOWED.items():
            with self.subTest(name):
                self.assertFalse(approvals._protected_ancestor_operations(command))
                self.assertFalse(approvals.command_names_protected_path(command))

    def test_heredoc_is_refused_even_when_the_splitter_succeeds(self) -> None:
        """The quoted-comment cases split without error, so only the structure rule refuses them."""
        for name in ("multiline wrapper, heredoc, quoted comment", "write between two heredocs"):
            with self.subTest(name):
                approvals._shell_words(REFUSED[name], ";&|()\n<>")  # does not raise
                self.assertTrue(approvals._protected_ancestor_operations(REFUSED[name]))


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

    def test_matrix_records(self) -> None:
        for name, command in REFUSED.items():
            with self.subTest(name):
                record = self.record(command)
                self.assertFalse(record["can_allow"])
                self.assertIn("permission policy", record["note"])
        for name, command in ALLOWED.items():
            with self.subTest(name):
                record = self.record(command)
                self.assertTrue(record["can_allow"])
                self.assertIn("Known credentials are hidden", record["note"])


class SupervisorSpellingRegressionTests(unittest.TestCase):
    """Audit cases use the gate boundary and a print-only shell function."""

    def test_quote_and_wrapper_controls(self) -> None:
        """Retain the original malformed-quote protection and ordinary wrapper spellings."""
        controls = SUPERVISOR_COMMANDS + (
            'h"a" s"u" info', r'h\a s\u info',
            'env -u SAMPLE ha supervisor info', 'command -- ha su info',
            'timeout 1 ha supervisor info', 'busybox sh -c \'ha "su" info\'',
            '/bin/sh -c \'ha "supervisor" info\'', 'ha --log-level=debug "su" info',
        )
        for command in controls:
            for prefix in ('', HEREDOC):
                for remote in (False, True):
                    with self.subTest(command=command, heredoc=bool(prefix), remote=remote):
                        self.assertIn('Supervisor', refusal(prefix + command, remote))

    def test_super_alias_refused_in_all_contexts(self) -> None:
        """Use every alias shape from the audit with both relay modes and heredoc contexts."""
        for command in ('ha super info', 'ha "super" info', 'ha --log-level debug super info',
                        'env -u SAMPLE ha super info', 'sh -c \'ha "super" info\''):
            for prefix in ('', HEREDOC):
                for remote in (False, True):
                    with self.subTest(command=command, heredoc=bool(prefix), remote=remote):
                        self.assertIn('Supervisor', refusal(prefix + command, remote))

    def test_line_continuations_refused_in_all_contexts(self) -> None:
        """Literal continuations inside executable and family words must not conceal them."""
        for command in ('h\\\na su info', 'ha s\\\nu info', 'ha super\\\nvisor info',
                        'env -u SAMPLE h\\\na s\\\nu info', 'sh -c \'h\\\na s\\\nu info\''):
            for prefix in ('', HEREDOC):
                for remote in (False, True):
                    with self.subTest(command=command, heredoc=bool(prefix), remote=remote):
                        self.assertIn('Supervisor', refusal(prefix + command, remote))

    def test_other_layer_and_remote_controls(self) -> None:
        """The separate approval check and relay-family distinction still hold."""
        self.assertTrue(approvals.command_names_protected_path('h\\\na s\\\nu info'))
        self.assertTrue(approvals.command_names_protected_path(HEREDOC + 'ha super info'))
        self.assertEqual(refusal('ha core info', False), '')
        self.assertIn('Supervisor', refusal('ha core info', True))

    def test_shell_joins_literal_words_without_spaces(self) -> None:
        """A local function proves shell argument boundaries without an HA executable."""
        for command, expected in (('h\\\na s\\\nu info', '<su><info>'),
                                  ('ha super\\\nvisor info', '<supervisor><info>')):
            result = subprocess.run(['/bin/sh', '-c', 'ha() { printf "<%s>" "$@"; };\n' + command],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, expected)


class ShellContinuationTests(unittest.TestCase):
    """Check lexical joins against the shell without invoking any service."""

    def test_normalized_arguments_match_a_print_only_shell(self) -> None:
        """Unquoted/double-quoted joins disappear; single-quoted pairs and escapes stay literal."""
        join = '\\' + '\n'
        commands = (
            f'emit h{join}a s{join}u',
            f'emit ha{join}core stop',
            f'emit "h{join}a" "s{join}u"',
            f"emit 'h{join}a' 's{join}u'",
            'emit "h' + '\\' * 2 + '\n' + 'a"',
            f"emit sample # don't\nemit h{join}a",
            f'emit sample # comment{join}still comment\nemit done',
            f'emit literal\\#hash h{join}a',
        )
        prelude = 'emit() { printf "<%s>" "$@"; };\n'
        for command in commands:
            with self.subTest(command=command):
                normalized = gate_rules._join_shell_continuations(command)
                self.assertIsNotNone(normalized)
                outputs = [subprocess.run(['/bin/sh', '-c', prelude + script],
                           capture_output=True, text=True, check=True).stdout
                           for script in (command, normalized)]
                self.assertEqual(outputs[0], outputs[1])

    def test_both_parsers_and_the_independent_floor_join_literal_words(self) -> None:
        """Neither a parser nor the floor may replace a word join with a space."""
        join = '\\' + '\n'
        self.assertTrue(gate_rules._ha_supervisor_command(f'h{join}a s{join}u info'))
        self.assertTrue(gate_rules._ha_command(f'h{join}a co{join}re info', True))
        self.assertFalse(gate_rules._ha_command(f'h{join}a co{join}re info', False))
        with patch.object(gate_rules, '_ha_supervisor_command', return_value=False), \
                patch.object(gate_rules, '_ha_command', return_value=False):
            for family in (f's{join}u', f'su{join}per', f'super{join}visor'):
                for remote in (False, True):
                    with self.subTest(family=family, remote=remote):
                        self.assertIn('Supervisor', refusal(f'h{join}a {family} info', remote))

    def test_single_quoted_pairs_keep_their_text_but_the_floor_refuses_a_supervisor_spelling(self) -> None:
        """The join keeps single-quoted pairs; the floor's second reading refuses a Supervisor spelling in them."""
        join = '\\' + '\n'
        for content in (f'h{join}a s{join}u info', f'$(h{join}a s{join}u)',
                        f'`h{join}a s{join}u`'):
            command = "emit '" + content + "'"
            with self.subTest(command=command):
                self.assertEqual(gate_rules._join_shell_continuations(command), command)
                for remote in (False, True):
                    self.assertIn('Supervisor', refusal(command, remote))
        for command in (f"emit 'sample{join}text'", f"emit 'h{join}a co{join}re info'"):
            with self.subTest(command=command):
                self.assertEqual(refusal(command, False), '')

    def test_readable_harmless_joins_keep_no_opinion(self) -> None:
        """Readable continuations must not become a blanket command ban."""
        join = '\\' + '\n'
        for command in (f'printf sample{join}text', f'ls {join}-la'):
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertEqual(refusal(command, remote), '')
        self.assertEqual(refusal(f'h{join}a co{join}re info', False), '')

    def test_ambiguous_continuations_refuse_and_request_one_line(self) -> None:
        """Heredocs, substitutions and unreadable quotes require a simpler command."""
        join = '\\' + '\n'
        for command in (HEREDOC + f'h{join}a su info', f'echo $(h{join}a su info)',
                        f'echo `h{join}a su info`', f"echo $'h{join}a'",
                        f"sh -c 'h{join}a su info", f'cat <(h{join}a su info)'):
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertIsNone(gate_rules._join_shell_continuations(command))
                    self.assertIn('one line', refusal(command, remote))
        self.assertEqual(refusal('echo $(printf sample)', False), '')


class CommentContinuationTests(unittest.TestCase):
    """A shell comment ends at its newline even when preceded by a backslash."""

    def test_comment_does_not_hide_a_joined_supervisor_command(self) -> None:
        command = '# marker\\\nh\\\na su info'
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.assertIn('Supervisor', refusal(command, remote))
        self.assertEqual(gate_rules._join_shell_continuations(command), '# marker\\\nha su info')
        self.assertTrue(gate_rules._ha_supervisor_command(command))
        self.assertTrue(approvals.command_names_protected_path(command))

    def test_comment_boundaries_hold_for_parsers_and_the_text_floor(self) -> None:
        join = '\\' + '\n'
        for comment in ('# marker', "# don't", '# $(ignored)', '# <<ignored'):
            for family in ('su', 'super', 'supervisor'):
                command = f'{comment}{join}h{join}a {family} info'
                with self.subTest(comment=comment, family=family):
                    self.assertTrue(gate_rules._ha_supervisor_command(command))
                    core = f'{comment}{join}h{join}a co{join}re info'
                    self.assertTrue(gate_rules._ha_command(core, True))
                    self.assertFalse(gate_rules._ha_command(core, False))
                    for remote in (False, True):
                        with patch.object(gate_rules, '_ha_supervisor_command', return_value=False), \
                                patch.object(gate_rules, '_ha_command', return_value=False):
                            self.assertIn('Supervisor', refusal(command, remote))

    def test_print_only_shell_and_normalizer_agree_after_a_comment(self) -> None:
        join = '\\' + '\n'
        prelude = 'ha() { printf "<%s>" "$@"; }; emit() { printf "<%s>" "$@"; };\n'
        for shell in ('/bin/sh', '/bin/bash'):
            for command, expected in ((f'# marker{join}h{join}a su info', '<su><info>'),
                                      (f'# marker{join}emit sample{join}text', '<sampletext>'),
                                      (f'emit first # marker{join}emit second', '<first><second>')):
                with self.subTest(shell=shell, command=command):
                    normalized = gate_rules._join_shell_continuations(command)
                    self.assertIsNotNone(normalized)
                    for script in (command, normalized):
                        run = subprocess.run([shell, '-c', prelude + script],
                                             capture_output=True, text=True, check=True)
                        self.assertEqual(run.stdout, expected)

    def test_quoted_hash_and_harmless_comments_remain_literal(self) -> None:
        join = '\\' + '\n'
        for command in (f"emit '# marker{join}literal'", f'emit "# marker{join}literal"',
                        f'# marker{join}emit sample{join}text'):
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertEqual(refusal(command, remote), '')


class ContinuationFloorTests(unittest.TestCase):
    """Continued forms that the join or the parser reads differently from the shell.

    Each form runs a print-only ``ha`` stub with a Supervisor family under bash and dash, so the
    shell control proves the command would reach the Supervisor. The hook must refuse it with
    remote control off and on, and the text floor must refuse it alone.
    """

    JOIN = '\\' + '\n'
    FORMS = (
        ('true \r# ; h' + JOIN + 'a su info', 'su'),
        ('true \r# ; ha s' + JOIN + 'u info', 'su'),
        ('true \r# ; ha sup' + JOIN + 'er info', 'super'),
        ('echo ${x- #}; h' + JOIN + 'a su info', 'su'),
        ('echo ${x:- #}; ha s' + JOIN + 'u info', 'su'),
        ("dash -c 'h" + JOIN + "a su info'", 'su'),
        ("setsid sh -c 'h" + JOIN + "a su info'", 'su'),
        ("bash --norc -c 'h" + JOIN + "a su info'", 'su'),
    )

    def test_print_only_shell_runs_each_form(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            stub = Path(folder) / 'ha'
            stub.write_text('#!/bin/sh\nprintf "<%s>" "$@"\n', encoding='utf-8')
            stub.chmod(0o755)
            env = {**os.environ, 'PATH': folder + os.pathsep + os.environ.get('PATH', '')}
            for shell in ('bash', 'dash'):
                for command, family in self.FORMS:
                    with self.subTest(shell=shell, command=command):
                        run = subprocess.run([shell, '-c', command], capture_output=True, text=True,
                                             env=env, timeout=10, check=False)
                        self.assertIn(f'<{family}><info>', run.stdout)

    def test_hook_refuses_each_form(self) -> None:
        for command, _family in self.FORMS:
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertIn('Supervisor', refusal(command, remote))

    def test_text_floor_alone_refuses_each_form(self) -> None:
        with patch.object(gate_rules, '_ha_supervisor_command', return_value=False), \
                patch.object(gate_rules, '_ha_command', return_value=False):
            for command, _family in self.FORMS:
                for remote in (False, True):
                    with self.subTest(command=command, remote=remote):
                        self.assertIn('Supervisor', refusal(command, remote))

    def test_protected_command_pattern_names_every_alias(self) -> None:
        """The first command check names all three Supervisor aliases, independent of the floor."""
        for family in sorted(gate_rules.HA_ALWAYS_FAMILIES):
            with self.subTest(family=family):
                self.assertTrue(any(pattern.search(f'ha {family} info')
                                    for pattern, _why in gate_rules.PROTECTED_COMMAND_RES))



class RedirectionTests(unittest.TestCase):
    """A redirection between ``ha`` and the family does not hide the family.

    The shell removes a redirection before ``ha`` runs, wherever it stands, so each form below
    runs a print-only ``ha`` stub with the family under bash and dash. Both parsers skip the
    redirection, the text floor accepts it, and a global-flag value with a quoted or escaped
    blank is refused in both remote-control modes.
    """

    SUPERVISOR_FORMS = (
        'ha 2>/dev/null su info',
        'ha 2> /dev/null su info',
        'ha </dev/null su info',
        'ha >/dev/null super info',
        'ha 2>&1 su info',
        'ha >&2 su info',
        'ha 2>&- su info',
        'ha --raw-json 2>/dev/null supervisor info',
        'ha --log-level debug 2>&1 su info',
        'ha --log-level 2>/dev/null debug su info',
        'ha -- 2>/dev/null su info',
        'ha >| /dev/null su info',
        'ha <<<x su info',
        'ha 3</dev/null su info',
        'ha &>/dev/null su info',
        'true &>/dev/null; ha 2>&1 su info',
        'env ha 2>&1 su info',
        "sh -c 'ha 2>&1 su info'",
        "dash -c 'ha 2>/dev/null su info'",
        # The shell ends a word at "<" and ">": an operator joined to ha, a flag or another redirection.
        'ha>/dev/null su info',
        'ha</dev/null su info',
        'ha>&2 su info',
        'ha>>/dev/null su info',
        'env ha>/dev/null su info',
        '/usr/bin/env ha>/dev/null supervisor info',
        'ha 2>/dev/null< /dev/null su info',
        # The target is a file in the test's own folder: a runner that is not root cannot write in /dev.
        'ha >out2> /dev/null su info',
        'ha --api-token>/dev/null x su info',
        'ha --log-level</dev/null debug su info',
        # A number right after an operator is its target, even after a blank (bash).
        'ha 2>&1>/dev/null su info',
        'ha 2>& 1>/dev/null su info',
        'ha >& 1>x su info',
        # Before ha, the redirection is skipped by the parsers too.
        '2>/dev/null ha su info',
    )
    REMOTE_FORMS = (
        'ha 2>/dev/null host info',
        'ha 2>&1 core info',
        '>/dev/null 2>&1 ha host info',
        'ha>/dev/null host info',
        'ha </dev/null> /dev/null host info',
        'ha --config>/dev/null x host info',
        "dash -c 'ha </dev/null host info'",
    )
    # A redirection target or a word that the text floor cannot read: a quote, a backslash, a
    # substitution, a process substitution, or a word that expands to nothing or to two words.
    UNREADABLE_FORMS = (
        "dash -c 'ha >\"a b\" su info'",
        "setsid sh -c 'ha 2>a\\ b su info'",
        'echo ">" & ha 2>&1 >"a b" su info',
        "dash -c 'ha >$(echo out) su info'",
        "dash -c 'ha >`echo out` su info'",
        'ha 2>${x:- a} su info',
        'ha --config ${x:- a} su info',
        'ha $EMPTY su info',
        'ha `true` su info',
        'ha < <(true) su info',
        'ha > >(cat) su info',
        'ha 2> >(cat >/dev/null) su info',
        # The family word itself escaped after an unreadable target.
        "dash -c 'ha >\"a b\" s\\u info'",
        # Python ends a line at a carriage return, a form feed or U+2028; the shell does not.
        'ha ${x:+\r} su info',
        'ha ${x:+\f} su info',
        'ha ${x:+\v} su info',
        'ha ${x:+\x1c} su info',
        'ha ${x:+\x85} su info',
        'ha ${x:+\u2028} su info',
        # A newline inside an expansion or a quoted word: the shell reads on to its end.
        'ha ${x:+a\nb} su info',
        'ha $(:\n) su info',
        'ha `:\n` su info',
        'ha >"a\nb" su info',
        'ha >"a\rb c" su info',
        "ha >'a\fb c' su info",
        # Behind a quoted operator and 2>&1: the parsers read these themselves.
        'echo ">" & ha 2>&1 >"a\nb" su info',
        'echo ">" & ha 2>&1 >"a\rb c" su info',
        "echo '>' | ha 2>&1 >\"a\nb\" su info",
        'echo "2>" & ha 2>&1 >"a\rb c" su info',
    )
    UNREADABLE_REMOTE_FORMS = ("dash -c 'ha >\"a b\" host info'",)
    QUOTED_VALUE_FORMS = (
        'ha --config "a\nb" su info',
        'ha --config "a\rb" su info',
        'echo ">" & ha 2>&1 --config "a\nb" su info',
        "dash -c 'ha --api-token \"a b\" su info'",
        "setsid sh -c 'ha --endpoint a\\ b su info'",
        "dash -c 'ha --api-token \"a b\" s\\\nu info'",
    )
    # A quoted or escaped operator before 2>&1 is a plain word, so it cannot join the "&" after it
    # and hide the next command from the parsers.
    QUOTED_OPERATOR_FORMS = tuple(
        prefix + middle
        for prefix in ('echo ">" & ', "echo '>' | ", 'echo "2>" & ', 'echo \\> & ')
        for middle in ('ha 2>&1 >"a\nb" su info', 'ha 2>&1 >"a\rb c" su info', 'ha 2>&1 -x"y z" su info',
                       'ha 2>&1 -x"> y z" su info', 'ha 2>&1 --raw-json="> y z" su info'))
    # Bash brace expansion and nullglob can remove a word or build the family word. Dash expands
    # neither, so these run under bash only.
    BRACE_FORMS = (
        'ha {,} su info',
        'ha --config {,} x su info',
        'ha 2>/dev/null {,} su info',
        'ha {,}su info',
        'ha {su,} info',
        'ha s{u,} info',
        'ha {s,}u info',
        'ha {su,info}',
        'ha 2>&1 {su,} info',
        'ha {x,s}{u,y} info',
        'ha {s..s}u info',
        "bash -c 'ha {,} su info'",
        'shopt -s nullglob; ha /nonexistent-folder/* su info',
        'shopt -s nullglob; ha nothing-here? su info',
    )
    BRACE_REMOTE_FORMS = ('ha {,} host info', 'ha {,} core restart', 'ha {co,}re info', 'ha h{o,}st info')
    # G1: a quoted or escaped blank or operator does not end a brace word in bash, so these are
    # one word each and run ha su. Bash only.
    BRACE_QUOTED_FORMS = (
        'ha {s,"x y"}u info', 'ha {s,x\\ y}u info', 'ha {s,"a\tb"}u info', 'ha {s,"a\nb"}u info',
        "ha {s,'x;y'}u info", 'ha {s,a\\;b}u info', 'ha {s,"x&y"}u info', 'ha {s,"x|y"}u info',
        'ha {s,"x>y"}u info', 'ha {s,"x(y"}u info', 'ha s{u,"a b"} info', 'ha {s,"a b"}{u,"c d"} info',
        "ha {s,}$x''u info", 'ha --config x {s,"a b"}u info',
    )
    # G3: the shell builds the family word itself. The family position holds a $, a backtick or a
    # glob, so the word is refused in both modes, whatever it could become.
    COMPUTED_FAMILY_FORMS = (
        'ha s${x}u info', 'ha s$()u info', 'ha s$@u info', 'ha s$1u info', 'ha s`:`u info',
        'ha $(printf s)u info', 'ha ${x:-s}${x:-u} info', 'touch su; ha s? info', 'touch su; ha s[u] info',
        'ha 2>/dev/null s${x}u info', 'ha --config a s$()u info', 'env ha s`:`u info',
    )
    COMPUTED_FAMILY_BASH_FORMS = ("ha $'\\163u' info", "ha $'s\\x75' info", "ha s$''u info", 'ha s$""u info',
                                  'ha {,} s${x}u info', 'ha {,} {,} 2>/dev/null s$()u info')
    COMPUTED_REMOTE_FORMS = ('ha ${x:+a\nb} host info', 'ha h${x}ost reboot')
    # The redirection takes the family word as its target, so the shell runs "ha info".
    NOT_SUPERVISOR = ('ha > su info', 'ha 2> su info', 'ha core info 2>/dev/null', 'echo ha 2>/dev/null')

    # Dash has no here-string or process substitution and reads "&>" as "&" then ">", so these
    # run under bash only.
    BASH_ONLY = ('<<<', ' &>/dev/null su', '<(', '>(', '&1>', '& 1>')
    # Harmless commands with a quoted, computed or joined redirection: no family word follows.
    HARMLESS = ('ha info 2>"$LOG"', 'grep "x" notes.txt > "/tmp/a b"', 'echo ha > /tmp/x', 'ls>/tmp/x',
                'echo "${x:->}"', 'cmd 2>&1 | tee "log file"', 'echo ">" & ls 2>&1 -l', 'echo {a,b} > /tmp/x',
                'ls *.yaml\r\nha --help', 'cat a.txt\x0csupervisor.txt')
    # Remote control off: these name a brace, a glob or a carriage return, but bash cannot make a
    # Supervisor family of them, so the remote-off answer stays.
    HARMLESS_REMOTE_OFF = ("ha addons logs {a,b}", "ha core info | jq '.data[]'", "ha core logs\r",
                           "cat ha.json | jq '{name:.su}'", "ha core info --raw-json | jq '{v: .data.version}'",
                           "ha core info; { echo a; }", "ha core info; echo ${HOME}/{a,b}",
                           "ha addons logs x | awk '{print $1}'", "git commit -m 'ha: {a,b}'",
                           "ha backups list 2>&1 | grep -c '*'", "ha core info | jq '{a: .x, b: .su}'",
                           "bash -c 'jq \"{name: .su}\" ha.json'",
                           "grep -rn ha --include='*.yaml' .", "echo '{s,x}u' ha")
    # H1: a brace word inside quotes that a second shell reads as a command and expands.
    SECOND_SHELL_BRACE_FORMS = (
        "bash -c 'ha {s,x}u info'", "bash -c 'ha s{u,} info'", 'bash -c "ha {s,x}u info"',
        "bash -c 'ha {s,\"x y\"}u info'", "bash -c 'ha {,} {s,x}u info'", "eval 'ha {s,x}u info'",
        'eval "ha s{u,} info"', "eval ha '{s,x}u' info", 'eval ha "s{u,}" info', "trap 'ha {s,x}u info' EXIT",
        "x='ha {s,x}u info'; eval \"$x\"", "echo 'ha {s,x}u info' | bash", "bash -c 'echo a; ha s{u,} info'",
        "env bash -c 'ha s{u,} info'", "bash -lc 'ha s{u,} info'", """bash -c 'bash -c "ha s{u,} info"'""",
        "bash -c 'ha \"s\"{u,} info'", "eval 'ha {s,\"x y\"}u info'")
    # H2: a built family word behind a word that the parser does not step over.
    BUILT_FAMILY_BEHIND_FORMS = (
        'while ha s${x}u info; do break; done', 'until ha s${x}u info; do break; done',
        'if false; then :; else ha s${x}u info; fi', 'if false; then :; elif ha s${x}u info; then :; fi',
        "while ha $'su' info; do break; done", 'touch su; while ha s[u] info; do break; done',
        'setsid ha s${x}u info', 'setsid -w ha s${x}u info', 'coproc ha s${x}u info',
        'exec -a x ha s${x}u info', 'time -p ha s${x}u info', 'builtin command ha s${x}u info',
        'function f { ha s${x}u info; }; f', 'while ha {,} s${x}u info; do break; done',
        'while ha {s,$(:)}u info; do break; done', 'while ha {s,${x:-a b}}u info; do break; done',
        'while ha {s,$((1 + 1))}u info; do break; done', 'while ha s{u,${x:-(}} info; do break; done')

    def _runs(self, command: str, family: str, shells: tuple[str, ...] = ()) -> None:
        """Run the form with a print-only ha stub that writes its arguments to a file, not stdout."""
        with tempfile.TemporaryDirectory() as folder:
            stub = Path(folder) / 'ha'
            stub.write_text('#!/bin/sh\nprintf "<%s>" "$@" > "$HA_STUB_OUT"\n', encoding='utf-8')
            stub.chmod(0o755)
            out = Path(folder) / 'out'
            env = {**os.environ, 'PATH': folder + os.pathsep + os.environ.get('PATH', ''), 'HA_STUB_OUT': str(out)}
            env.pop('x', None)
            env.pop('EMPTY', None)
            if not shells:
                shells = ('bash',) if any(mark in command for mark in self.BASH_ONLY) else ('bash', 'dash')
            for shell in shells:
                with self.subTest(shell=shell, command=command):
                    out.write_text('', encoding='utf-8')
                    subprocess.run([shell, '-c', command], capture_output=True, text=True, cwd=folder,
                                   env=env, timeout=10, check=False, stdin=subprocess.DEVNULL)
                    self.assertIn(f'<{family}>', out.read_text(encoding='utf-8'))

    def test_print_only_shell_runs_each_form(self) -> None:
        for command in self.SUPERVISOR_FORMS + self.QUOTED_VALUE_FORMS + self.UNREADABLE_FORMS:
            family = 'supervisor' if ' supervisor ' in command else 'super' if ' super ' in command else 'su'
            self._runs(command, family)
        for command in self.QUOTED_OPERATOR_FORMS:
            self._runs(command, 'su')
        for command in self.REMOTE_FORMS + self.UNREADABLE_REMOTE_FORMS:
            self._runs(command, 'core' if ' core ' in command else 'host')

    def test_print_only_bash_runs_each_brace_form(self) -> None:
        for command in self.BRACE_FORMS:
            self._runs(command, 'su', ('bash',))
        for command in self.BRACE_REMOTE_FORMS:
            self._runs(command, 'core' if 'core' in command or '{co,}' in command else 'host', ('bash',))

    def test_hook_refuses_brace_and_quoted_operator_forms(self) -> None:
        for command in self.BRACE_FORMS + self.QUOTED_OPERATOR_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn('Supervisor', refusal(command, remote_on))
        for command in self.BRACE_REMOTE_FORMS:
            with self.subTest(command=command):
                self.assertIn('Supervisor', refusal(command, True))
                self.assertEqual(refusal(command, False), '')

    def test_parsers_alone_read_past_a_quoted_operator(self) -> None:
        """C3: the parsers no longer depend on the floor for a command after a quoted operator."""
        for command in self.QUOTED_OPERATOR_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_supervisor_command(command))
        self.assertTrue(gate_rules._ha_command('echo ">" & ha 2>&1 >"a b" host info', True))

    def test_unreadable_word_rule_alone_refuses_brace_forms(self) -> None:
        for command in self.BRACE_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertTrue(gate_rules._ha_unknown_flag(command, remote_on))
        for command in self.BRACE_REMOTE_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_unknown_flag(command, True))
                self.assertFalse(gate_rules._ha_unknown_flag(command, False))

    def test_a_brace_sequence_counts_as_a_family_word(self) -> None:
        """Bash expands {a..z}{a..z} to every pair of letters, su among them; the rule does not list them."""
        self.assertTrue(gate_rules._brace_word_names_family('{a..z}{a..z}', False))
        self.assertFalse(gate_rules._brace_word_names_family('{a,b}', False))

    def test_harmless_braces_and_globs_keep_the_remote_off_answer(self) -> None:
        for command in self.HARMLESS_REMOTE_OFF:
            with self.subTest(command=command):
                self.assertEqual(refusal(command, False), '')

    def test_print_only_shell_runs_each_g1_and_g3_form(self) -> None:
        for command in self.BRACE_QUOTED_FORMS + self.COMPUTED_FAMILY_BASH_FORMS:
            self._runs(command, 'su', ('bash',))
        for command in self.COMPUTED_FAMILY_FORMS:
            self._runs(command, 'su', ('bash', 'dash'))
        self._runs('ha {ho,"a b"}st reboot', 'host', ('bash',))
        for command in self.COMPUTED_REMOTE_FORMS:
            self._runs(command, 'host', ('bash', 'dash'))

    def test_hook_refuses_a_quoted_brace_word_and_a_computed_family_word(self) -> None:
        for command in self.BRACE_QUOTED_FORMS + self.COMPUTED_FAMILY_FORMS + self.COMPUTED_FAMILY_BASH_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn('Supervisor', refusal(command, remote_on))
        self.assertIn('Supervisor', refusal('ha {ho,"a b"}st reboot', True))
        # A computed family word could be su, so it is refused with remote control off too.
        for command in self.COMPUTED_REMOTE_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn('Supervisor', refusal(command, remote_on))

    def test_each_rule_alone_refuses_its_forms(self) -> None:
        for command in self.BRACE_QUOTED_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_unknown_flag(command, False))
        for command in self.COMPUTED_FAMILY_FORMS + self.COMPUTED_FAMILY_BASH_FORMS + self.COMPUTED_REMOTE_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_supervisor_command(command))

    def test_brace_words_follow_plain_quotes(self) -> None:
        """A blank inside quotes stays in the word; quoted text is read again as a command of its own (H1)."""
        self.assertEqual(gate_rules._brace_words(' {s,"x y"}u info', 0), ['{s,"x y"}u'])
        self.assertEqual(gate_rules._brace_words(" {s,a\\ b}u;x", 0), ['{s,a\\ b}u'])
        # A second shell would cut the jq filter at its blank: "{a:" has no comma, so it is no expansion.
        self.assertEqual(gate_rules._brace_words(" core info | jq '{a: .b}'", 0), ['{a:'])
        self.assertFalse(gate_rules._brace_word_names_family('{a:', False))
        self.assertEqual(gate_rules._brace_words(" bash -c 'ha s{u,} info'", 0), ['s{u,}'])
        self.assertEqual(gate_rules._brace_words(' eval ha "s{u,}" info', 0), ['s{u,}'])
        self.assertEqual(gate_rules._brace_words(""" bash -c 'bash -c "ha {s,x}u"'""", 0), ['{s,x}u'])
        # In double quotes a backslash still escapes the quote, so the text goes on past it.
        self.assertEqual(gate_rules._brace_words(' "a\\"b {s,x}u"', 0), ['{s,x}u'])
        # A quote still open at the end: the rest is one word, and its quote refuses it.
        self.assertEqual(gate_rules._brace_words(' x "{s,u}', 0), ['"{s,u}'])
        # Quoted text that closes after ha is read, even when it opens before ha.
        self.assertEqual(gate_rules._brace_words("git commit -m 'ha: {a,b}'", 18), ['{a,b}'])
        self.assertEqual(gate_rules._brace_words("echo '{s,x}u' ha", 14), [])
        self.assertEqual(gate_rules._brace_words('{s,u} x ha {a,b}', 10), ['{a,b}'])
        # With ${, $(, $', $", a backtick, a comment or a heredoc the text is cut at every blank, quotes or not.
        self.assertEqual(gate_rules._brace_words(' ${x} {s,"a b"}u', 0), ['${x}', '{s,"a'])
        self.assertEqual(gate_rules._brace_words(" # it's {s,u}", 0), ['{s,u}'])
        self.assertEqual(gate_rules._brace_words(' <<x\n{a,b}\nx', 0), ['{a,b}'])

    def test_brace_word_rule_fails_closed(self) -> None:
        """G6: a quote, a backslash or a backtick in the word, or too many choices to list, count as a family word."""
        for word in ('{s,"x"}u', "{a,'b'}", '{a,b\\ c}', '{a,`b`}'):
            with self.subTest(word=word):
                self.assertTrue(gate_rules._brace_word_names_family(word, False))
        # A $ word whose braces do not close: the cut reading broke it inside ${...} or $(...) (H2).
        for word in ('{s,$', '{s,${x:-a', 's{u,${x:-'):
            with self.subTest(word=word):
                self.assertTrue(gate_rules._brace_word_names_family(word, False))
        self.assertFalse(gate_rules._brace_word_names_family('${HOME}/{a,b}', False))
        many = '{a,b}' * 7  # 128 choices, none a family word
        self.assertIsNone(gate_rules._brace_choices(many))
        self.assertTrue(gate_rules._brace_word_names_family(many, False))
        self.assertFalse(gate_rules._brace_word_names_family('{a,b}' * 6, False))

    def test_print_only_bash_runs_each_h1_and_h2_form(self) -> None:
        for command in self.SECOND_SHELL_BRACE_FORMS + self.BUILT_FAMILY_BEHIND_FORMS:
            self._runs(command, 'su', ('bash',))
        self._runs("bash -c 'ha {ho,x}st reboot'", 'host', ('bash',))
        self._runs('while ha ho${x}st reboot; do break; done', 'host', ('bash',))

    def test_hook_refuses_a_brace_word_for_a_second_shell_and_a_built_family_word_behind_any_word(self) -> None:
        for command in self.SECOND_SHELL_BRACE_FORMS + self.BUILT_FAMILY_BEHIND_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn('Supervisor', refusal(command, remote_on))
        self.assertIn('Supervisor', refusal("bash -c 'ha {ho,x}st reboot'", True))
        self.assertIn('Supervisor', refusal('while ha ho${x}st reboot; do break; done', False))

    def test_built_family_word_rule_alone_refuses_each_h2_form(self) -> None:
        """H2: the text rule needs no list of the words that can stand before ha."""
        for command in self.BUILT_FAMILY_BEHIND_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_built_family_word(command))
        # A substitution is read as a command of its own, also nested past the depth the rule reads.
        for command in ('x=$(while ha s${y}u info; do :; done)', 'echo `setsid ha s${y}u info`',
                        'echo ' + '$(' * 8 + 'ha core info' + ')' * 8):
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_built_family_word(command))
        # A blank inside a substitution does not move the family position.
        for command in ('ha core info', 'ha --log-level debug core info', 'ha core logs > $LOG',
                        'ha {,} core info', 'echo ha', 'ha --api-token `cat t` core info',
                        'ha --api-token $(cat /tmp/t) core restart'):
            with self.subTest(command=command):
                self.assertFalse(gate_rules._ha_built_family_word(command))

    def test_a_brace_word_ends_at_a_newline(self) -> None:
        """H3: a newline ends a word outside quotes, as for the shell."""
        self.assertEqual(gate_rules._brace_words('ha a\ns{u,}', 0), ['s{u,}'])

    def test_quoted_text_is_read_again_only_so_deep(self) -> None:
        """Past _BRACE_QUOTE_DEPTH the quoted text is one word with its quotes, and a brace word with a quote is refused."""
        deep = gate_rules._BRACE_QUOTE_DEPTH
        self.assertEqual(gate_rules._brace_words("'{a,b}'", -1, deep), ["'{a,b}'"])
        self.assertTrue(gate_rules._brace_word_names_family("'{a,b}'", False))
        self.assertTrue(gate_rules._brace_word_names_family("'{a}'", False))

    def test_nested_braces_are_refused_past_the_limit_and_stay_fast(self) -> None:
        """H5: a word with more than _BRACE_MAX_BRACES braces and a comma is refused without expansion."""
        limit = gate_rules._BRACE_MAX_BRACES
        self.assertTrue(gate_rules._brace_word_names_family('{a:' * limit + '{b,c}' + '}' * limit, False))
        self.assertFalse(gate_rules._brace_word_names_family('{a:' * (limit - 1) + '{b,c}' + '}' * (limit - 1), False))
        for text in ('echo ha; echo ' + '{a:' * 3200 + '1' + '}' * 3200,
                     'ha ' + '{a:' * 3200 + '{b,c}' + '}' * 3200):
            with self.subTest(text=text[:20]):
                started = time.monotonic()
                gate_rules._ha_unknown_flag(text, False)
                self.assertLess(time.monotonic() - started, 0.5)

    def test_brace_words_count_from_the_first_ha(self) -> None:
        """G6: a brace word before the first ha is not read; one between two ha lines is."""
        self.assertFalse(gate_rules._ha_unknown_flag('echo {s,}u; ha core info', False))
        self.assertTrue(gate_rules._ha_unknown_flag('ha core info; echo {s,}u; ha core logs', False))

    def test_stated_over_refusals_of_harmless_braces(self) -> None:
        """After the word ha, a brace sequence, a quoted brace word that could expand to a family word (H1), and a
        $, a backtick or a glob right after the word ha (H2) are refused. DOCS says so."""
        for command in ('echo ha; echo {1..3}', 'grep ha notes.txt; for i in {1..3}; do echo $i; done',
                        "echo 'ha {s,x}u info'", 'ha "{s,x}u" info', 'echo ha ${HOME}', 'grep ha *.yaml'):
            with self.subTest(command=command):
                self.assertIn('Supervisor', refusal(command, False))

    # J1: in a text with a #, $(, a backtick or a heredoc, the reading cuts a brace word at a quoted blank.
    CUT_BRACE_FORMS = (
        'ha {-x" y",s}u info # c', "ha {-x' y',s}u info # c", 'ha {-x\\ y,s}u info # c', 'ha {-x";",s}u info # c',
        'ha {-x">",s}u info # c', 'ha {--config" x",s}u info # c', 'ha {-x" y",su} info # c',
        'echo $(:); ha {-x" y",s}u info', 'echo `:`; ha {-x" y",s}u info', 'ha {-x" y",s}u info <<E\nE',
        'ha {-x"\ty",s}u info # c', 'bash -c \'ha {-x" y",s}u info\' # c')
    # J2: a second shell reads the text after the first shell removed its quotes and backslashes.
    SECOND_SHELL_JOINED_FORMS = (
        'eval ha s\\{u,\\} info', 'eval ha s\\{u,} info', 'eval ha s\\{u,x} info', 'eval ha \\{s,x\\}u info',
        'eval ha s\\{u..u} info', "eval eval ha 's\\{u,}' info", "eval ha s'{'u,'}' info", "eval 'ha s{u,''} info'",
        "eval \"ha s{u,\"'} info'", "eval 'ha s{u.''.u} info'")
    SECOND_SHELL_JOINED_BOTH_SHELLS = (
        "bash -c 'eval ha s\\{u,\\} info'", "bash -c 'ha s{u,'\"} info\"", "bash -c 'ha s{u,'\\''x'\\''} info'",
        "bash -c 'ha s{u,'\"x} info\"", "bash -c 'ha s{''u,} info'")
    # J3: a quoted or escaped blank before a built family word is one word for the shell.
    QUOTED_BLANK_BUILT_FORMS = (
        'while ha --config "a b" s${x}u info; do break; done', "while ha --config 'a b' s${x}u info; do break; done",
        'while ha --config a\\ b s${x}u info; do break; done', 'while ha --api-token "a b" s${x}u info; do break; done',
        'while ha --endpoint "a b c" s${x}u info; do break; done',
        'while ha --config "${y:-a b}" s${x}u info; do break; done', 'while ha -x"a b" s${x}u info; do break; done',
        'while ha 2>"a b" s${x}u info; do break; done', 'while ha > "a b" s${x}u info; do break; done',
        'setsid ha --config "a b" s${x}u info', 'if false; then :; else ha 2>"a b" s${x}u info; fi',
        'touch su; while ha --config "a b" s[u] info; do break; done')
    # J4: with bash's extglob option, @(su), +(su) and !(x) are globs.
    EXTGLOB_FORMS = ('touch su\nshopt -s extglob\nha @(su) info', 'touch su\nshopt -s extglob\nha +(su) info',
                     'touch su\nshopt -s extglob\nha @(su|x) info', 'touch su\nshopt -s extglob\nha @(s)u info',
                     'touch su\nshopt -s extglob\nwhile ha @(su) info; do break; done')
    # J5: a script in the $'...' form that spells the family or a brace with an escape.
    ANSI_C_FORMS = ("bash -c $'ha \\x73u info'", "bash -c $'ha s\\x7bu,} info'", "bash -c $'ha s\\173u,\\175 info'",
                    "eval $'ha s\\x7bu,\\x7d info'", "bash -c $'ha s{u\\x2c} info'")
    # J6: ha right after a dash is a flag group, not the program.
    DASH_HA_HARMLESS = ('ls -ha *', 'ls -ha $HOME', 'du -ha * | sort -h', 'du -ha "$dir"', 'tar -czf x.tgz -ha *',
                        'grep -ha $x f')

    def test_print_only_shell_runs_each_sixth_round_form(self) -> None:
        for command in self.CUT_BRACE_FORMS + self.SECOND_SHELL_JOINED_FORMS + self.EXTGLOB_FORMS + self.ANSI_C_FORMS:
            self._runs(command, 'su', ('bash',))
        for command in self.SECOND_SHELL_JOINED_BOTH_SHELLS + self.QUOTED_BLANK_BUILT_FORMS:
            self._runs(command, 'su', ('bash', 'dash'))

    def test_hook_refuses_each_sixth_round_form(self) -> None:
        for command in (self.CUT_BRACE_FORMS + self.SECOND_SHELL_JOINED_FORMS + self.SECOND_SHELL_JOINED_BOTH_SHELLS
                        + self.QUOTED_BLANK_BUILT_FORMS + self.EXTGLOB_FORMS + self.ANSI_C_FORMS):
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn('Supervisor', refusal(command, remote_on))

    def test_each_sixth_round_rule_alone_refuses_its_forms(self) -> None:
        for command in self.CUT_BRACE_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_unknown_flag(command, False))
        for command in self.SECOND_SHELL_JOINED_FORMS + self.SECOND_SHELL_JOINED_BOTH_SHELLS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_floor_brace_word(command, False))
        for command in self.QUOTED_BLANK_BUILT_FORMS + self.EXTGLOB_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_built_family_word(gate_rules._join_shell_continuations(command)))

    def test_ha_after_a_dash_is_not_the_program(self) -> None:
        for command in self.DASH_HA_HARMLESS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertEqual(refusal(command, remote_on), '')
        self.assertIn('Supervisor', refusal('ls; ha s${x}u info', False))

    def test_built_family_word_readings(self) -> None:
        """J7: each reading in _ha_built_family_word has a form that only it reads right."""
        # Redirections are set apart first: ha>/dev/null is ha, >, /dev/null.
        self.assertTrue(gate_rules._built_word_after_ha(gate_rules._space_redirections('while ha>/dev/null s${x}u info')))
        self.assertFalse(gate_rules._built_word_after_ha('while ha>/dev/null s${x}u info'))
        # Quotes and backslashes are removed: "h"a is the word ha.
        self.assertTrue(gate_rules._ha_built_family_word('while "h"a s${x}u info; do break; done'))
        # A quoted or escaped blank stays inside its word, and a continuation is removed.
        self.assertEqual(gate_rules._keep_quoted_blanks('ha --config "a b" s${x}u'), 'ha --config a_b s${x}u')
        self.assertEqual(gate_rules._keep_quoted_blanks("ha -x'a;b' a\\ b"), 'ha -xa_b a_b')
        self.assertEqual(gate_rules._keep_quoted_blanks('ha \\\nsu'), 'ha su')
        self.assertEqual(gate_rules._keep_quoted_blanks("a 'x\\ y'"), 'a x\\_y')
        # The second reading removes every backslash-newline pair, also inside single quotes.
        self.assertTrue(gate_rules._ha_built_family_word("while ha --config 'a\\\nb' s${x}u info; do break; done"))

    def test_stated_over_refusals_of_the_sixth_round(self) -> None:
        """DOCS states these: a quoted brace word in a text with ${ or a comment, and a $'...' escape beside ha."""
        for command in ('ha core info | jq "{v: ${x}}"', "ha core info | jq '{a: .b}' # x",
                        "printf $'a\\tb\\n'; ha core info"):
            with self.subTest(command=command):
                self.assertIn('Supervisor', refusal(command, False))
        self.assertEqual(refusal("printf $'a\\tb\\n'", False), '')

    def test_an_empty_quoted_flag_value_keeps_the_family_position(self) -> None:
        """K3: alone, "" or '' is a word to the shell, so here it is the flag's value."""
        for word in ('s${x}u', 's`:`u', 's$(:)u'):
            for command in (f'while ha --log-level "" {word} info; do break; done',
                            f'setsid ha --log-level "" {word} info',
                            f'if false; then :; else ha --log-level "" {word} info; fi',
                            f"while ha --config '' {word} info; do break; done",
                            f'while ha --config ""\t{word} info; do break; done'):
                with self.subTest(command=command):
                    self.assertIn('Supervisor', refusal(command, False))
        self.assertEqual(gate_rules._keep_quoted_blanks('ha --log-level "" s'), 'ha --log-level _ s')
        # Joined to a word, an empty pair adds nothing: ha'' is the word ha.
        self.assertEqual(gate_rules._keep_quoted_blanks("ha'' --config 'a b' s"), 'ha --config a_b s')
        self.assertIn('Supervisor', refusal("while ha'' --config \"a b\" s${x}u info; do break; done", False))

    def test_each_line_of_the_sixth_round_has_a_form_that_needs_it(self) -> None:
        """K5: the continuation and redirection steps each change an answer."""
        # The brace word for a second shell, joined over a continuation: eval reads s{u,}.
        self.assertIn('Supervisor', refusal('eval ha s\\{u\\\n,\\} info', False))
        # A redirection joined to ha, read with the quoted blanks kept.
        self.assertIn('Supervisor', refusal('while ha>"a b" s${x}u info; do break; done', False))
        # The same with the kept reading off (an apostrophe in a comment): the other reading needs it.
        self.assertIn('Supervisor', refusal("echo x # it's\nwhile ha>/dev/null s${x}u info; do break; done", False))
        # With the kept reading off, only the reading without continuations joins s and ${x}u.
        self.assertTrue(gate_rules._ha_built_family_word("echo x # it's\nwhile ha s\\\n${x}u info; do break; done"))

    def test_many_dash_ha_words_stay_fast(self) -> None:
        """K6: letters ha after a dash do not start a floor match, so many -ha words stay linear."""
        command = 'echo ' + ' '.join(['-ha'] * 10_000)
        started = time.monotonic()
        self.assertEqual(refusal(command, True), '')
        self.assertLess(time.monotonic() - started, 3)

    def test_brace_word_time_stays_flat_on_one_long_word(self) -> None:
        """G2: the brace words are read in one pass, so one long word after ha stays fast."""
        for text in ('ha ' + 'a' * 200_000, 'ha $x ' + 'a' * 200_000, 'ha ' + '{a' * 100_000,
                     'ha ' + "'{a' " * 40_000, 'ha $x ' + '{a,b}' * 40_000):
            with self.subTest(text=text[:12]):
                started = time.monotonic()
                gate_rules._ha_unknown_flag(text, False)
                self.assertLess(time.monotonic() - started, 0.5)

    def test_full_path_wrapper_before_ha_in_the_remote_parser(self) -> None:
        """C5 (M9): _ha_command knows a wrapper by its full path."""
        self.assertTrue(gate_rules._ha_command('/usr/bin/env ha host info', True))
        self.assertTrue(gate_rules._ha_command('/usr/bin/sudo -n ha core restart', True))
        self.assertFalse(gate_rules._ha_command('/usr/bin/env ha host info', False))

    def test_flag_value_rule_reads_a_quoted_family_word(self) -> None:
        """C5 (M16): with remote control off, a quoted or escaped family word after a quoted value counts."""
        for command in ('ha --config "a b" "su" info', 'ha --config "a b" s\\u info', 'ha --config "a b" "s"u info',
                        "ha --config 'a b' 'supervisor' info"):
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_unknown_flag(command, False))
        self.assertFalse(gate_rules._ha_unknown_flag('ha --config "a b" "core" info', False))

    def test_hook_refuses_supervisor_forms_in_both_modes(self) -> None:
        for command in self.SUPERVISOR_FORMS + self.QUOTED_VALUE_FORMS + self.UNREADABLE_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn('Supervisor', refusal(command, remote_on))

    def test_hook_refuses_remote_forms_with_remote_control_on(self) -> None:
        for command in self.REMOTE_FORMS + self.UNREADABLE_REMOTE_FORMS:
            with self.subTest(command=command):
                self.assertIn('Supervisor', refusal(command, True))
                self.assertEqual(refusal(command, False), '')

    def test_parsers_alone_skip_the_redirection(self) -> None:
        for command in self.SUPERVISOR_FORMS:
            if command.startswith('dash'):
                continue  # the parsers do not open dash; the text floor covers it
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_supervisor_command(command))
        for command in self.REMOTE_FORMS:
            if command.startswith('dash'):
                continue
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_command(command, True))

    def test_operators_are_set_apart_outside_quotes(self) -> None:
        """The parsers read ha>x as the shell does; a quoted or escaped operator and ${...} stay one word."""
        self.assertEqual(gate_rules._space_redirections('ha>/dev/null su').split(), ['ha', '>', '/dev/null', 'su'])
        self.assertEqual(gate_rules._space_redirections('ha 2>&1 su').split(), ['ha', '2>&', '1', 'su'])
        self.assertEqual(gate_rules._space_redirections('ha2>x').split(), ['ha2', '>', 'x'])
        self.assertEqual(gate_rules._space_redirections('ha <<-x su').split(), ['ha', '<<-', 'x', 'su'])
        self.assertEqual(gate_rules._space_redirections('ha 2>& 1>x').split(), ['ha', '2>&', '1', '>', 'x'])
        for text in ('echo ">"', "echo '<'", 'echo \\>', 'echo ${x:->}'):
            with self.subTest(text=text):
                self.assertEqual(gate_rules._space_redirections(text), text)

    def test_unreadable_word_rule_alone_refuses_each_form(self) -> None:
        """The rule for unreadable targets and $ words holds without the parsers or the floor."""
        for command in self.UNREADABLE_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertTrue(gate_rules._ha_unknown_flag(command, remote_on))
        for command in self.UNREADABLE_REMOTE_FORMS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_unknown_flag(command, True))
                self.assertFalse(gate_rules._ha_unknown_flag(command, False))

    def test_harmless_redirections_keep_no_opinion(self) -> None:
        for command in self.HARMLESS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertEqual(refusal(command, remote_on), '')

    def test_text_floor_alone_refuses_each_form(self) -> None:
        with patch.object(gate_rules, '_ha_supervisor_command', return_value=False), \
                patch.object(gate_rules, '_ha_command', return_value=False):
            for command in self.SUPERVISOR_FORMS + self.QUOTED_VALUE_FORMS + self.UNREADABLE_FORMS:
                for remote_on in (False, True):
                    with self.subTest(command=command, remote_on=remote_on):
                        self.assertIn('Supervisor', refusal(command, remote_on))
            for command in self.REMOTE_FORMS + self.UNREADABLE_REMOTE_FORMS:
                with self.subTest(command=command):
                    self.assertIn('Supervisor', refusal(command, True))

    def test_unreadable_flag_value_without_a_supervisor_word_keeps_remote_off_answer(self) -> None:
        """With remote control off, a quoted value before a remote family is not refused."""
        self.assertEqual(refusal("dash -c 'ha --api-token \"a b\" core info'", False), '')
        self.assertIn('Supervisor', refusal("dash -c 'ha --api-token \"a b\" core info'", True))

    def test_redirection_into_the_family_word_is_not_a_supervisor_command(self) -> None:
        for command in self.NOT_SUPERVISOR:
            with self.subTest(command=command):
                self.assertEqual(refusal(command, False), '')

    def test_both_shell_readings_of_ampersand_redirection(self) -> None:
        """Bash reads ``&>`` as one redirection; dash runs the part before ``&`` in the background."""
        segments = gate_rules._command_segments('true &>/dev/null ha su info', ';&|()\n')
        self.assertIn(['>', '/dev/null', 'ha', 'su', 'info'], segments)
        self.assertIn(['true', '&>', '/dev/null', 'ha', 'su', 'info'], segments)

    def test_a_quoted_operator_does_not_hide_the_next_command(self) -> None:
        """A quoted ``">"`` before a pipe is read both ways, so the command after the pipe is still checked."""
        self.assertIn(['ha', 'su', 'info'], gate_rules._command_segments('echo ">" | ha su info', ';&|()\n'))
        # A quoted ">" before "&" is a plain word in both readings: it never joins the "&".
        self.assertIn(['ha', '2>&', '1', 'su', 'info'],
                      gate_rules._command_segments('echo ">" & ha 2>&1 su info', ';&|()\n'))
        self.assertNotIn(['echo', '>&', 'ha', '2>&', '1', 'su', 'info'],
                         gate_rules._command_segments('echo ">" & ha 2>&1 su info', ';&|()\n'))
        self.assertTrue(gate_rules._ha_supervisor_command('echo ">" | ha su info'))

    def test_text_floor_time_stays_flat_with_repeated_flags(self) -> None:
        """No two alternatives of the floor's prefix match the same text, so a long flag list is fast."""
        for unit in ('--log-level=a ', '--log-level a ', '-x ', '2>/dev/null ', '2> x ', '<<-x ', '<<- x ', '<<-x<<-y '):
            with self.subTest(unit=unit):
                started = time.monotonic()
                gate_rules.HA_ALWAYS_TEXT_RE.search('ha ' + unit * 22 + 'x')
                self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
