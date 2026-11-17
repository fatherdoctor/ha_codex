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
    )
    REMOTE_FORMS = (
        'ha 2>/dev/null host info',
        'ha 2>&1 core info',
        "dash -c 'ha </dev/null host info'",
    )
    QUOTED_VALUE_FORMS = (
        "dash -c 'ha --api-token \"a b\" su info'",
        "setsid sh -c 'ha --endpoint a\\ b su info'",
        "dash -c 'ha --api-token \"a b\" s\\\nu info'",
    )
    # The redirection takes the family word as its target, so the shell runs "ha info".
    NOT_SUPERVISOR = ('ha > su info', 'ha 2> su info', 'ha core info 2>/dev/null', 'echo ha 2>/dev/null')

    # Dash has no here-string and reads "&>" as "&" then ">", so these run under bash only.
    BASH_ONLY = ('<<<', ' &>/dev/null su')

    def _runs(self, command: str, family: str) -> None:
        """Run the form with a print-only ha stub that writes its arguments to a file, not stdout."""
        with tempfile.TemporaryDirectory() as folder:
            stub = Path(folder) / 'ha'
            stub.write_text('#!/bin/sh\nprintf "<%s>" "$@" > "$HA_STUB_OUT"\n', encoding='utf-8')
            stub.chmod(0o755)
            out = Path(folder) / 'out'
            env = {**os.environ, 'PATH': folder + os.pathsep + os.environ.get('PATH', ''), 'HA_STUB_OUT': str(out)}
            shells = ('bash',) if any(mark in command for mark in self.BASH_ONLY) else ('bash', 'dash')
            for shell in shells:
                with self.subTest(shell=shell, command=command):
                    out.write_text('', encoding='utf-8')
                    subprocess.run([shell, '-c', command], capture_output=True, text=True,
                                   env=env, timeout=10, check=False, stdin=subprocess.DEVNULL)
                    self.assertIn(f'<{family}><info>', out.read_text(encoding='utf-8'))

    def test_print_only_shell_runs_each_form(self) -> None:
        for command in self.SUPERVISOR_FORMS + self.QUOTED_VALUE_FORMS:
            family = 'supervisor' if ' supervisor ' in command else 'super' if ' super ' in command else 'su'
            self._runs(command, family)
        for command in self.REMOTE_FORMS:
            self._runs(command, 'core' if ' core ' in command else 'host')

    def test_hook_refuses_supervisor_forms_in_both_modes(self) -> None:
        for command in self.SUPERVISOR_FORMS + self.QUOTED_VALUE_FORMS:
            for remote_on in (False, True):
                with self.subTest(command=command, remote_on=remote_on):
                    self.assertIn('Supervisor', refusal(command, remote_on))

    def test_hook_refuses_remote_forms_with_remote_control_on(self) -> None:
        for command in self.REMOTE_FORMS:
            with self.subTest(command=command):
                self.assertIn('Supervisor', refusal(command, True))
                self.assertEqual(refusal(command, False), '')

    def test_parsers_alone_skip_the_redirection(self) -> None:
        for command in self.SUPERVISOR_FORMS:
            if command.startswith('dash'):
                continue  # the parsers do not open dash; the text floor covers it
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_supervisor_command(command))
        for command in self.REMOTE_FORMS[:2]:
            with self.subTest(command=command):
                self.assertTrue(gate_rules._ha_command(command, True))

    def test_text_floor_alone_refuses_each_form(self) -> None:
        with patch.object(gate_rules, '_ha_supervisor_command', return_value=False), \
                patch.object(gate_rules, '_ha_command', return_value=False):
            for command in self.SUPERVISOR_FORMS + self.QUOTED_VALUE_FORMS:
                for remote_on in (False, True):
                    with self.subTest(command=command, remote_on=remote_on):
                        self.assertIn('Supervisor', refusal(command, remote_on))
            for command in self.REMOTE_FORMS:
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
        tokens = gate_rules._shell_words('true &>/dev/null ha su info', ';&|()\n')
        segments = gate_rules._command_segments(tokens, ';&|()\n')
        self.assertIn(['>/dev/null', 'ha', 'su', 'info'], segments)
        self.assertIn(['true', '&>/dev/null', 'ha', 'su', 'info'], segments)

    def test_a_quoted_operator_does_not_hide_the_next_command(self) -> None:
        """A quoted ``">"`` before a pipe is read both ways, so the command after the pipe is still checked."""
        tokens = gate_rules._shell_words('echo ">" | ha su info', ';&|()\n')
        self.assertIn(['ha', 'su', 'info'], gate_rules._command_segments(tokens, ';&|()\n'))
        self.assertTrue(gate_rules._ha_supervisor_command('echo ">" | ha su info'))

    def test_text_floor_time_stays_flat_with_repeated_flags(self) -> None:
        """No two alternatives of the floor's prefix match the same text, so a long flag list is fast."""
        for unit in ('--log-level=a ', '--log-level a ', '-x ', '2>/dev/null ', '2> x '):
            with self.subTest(unit=unit):
                started = time.monotonic()
                gate_rules.HA_ALWAYS_TEXT_RE.search('ha ' + unit * 22 + 'x')
                self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
