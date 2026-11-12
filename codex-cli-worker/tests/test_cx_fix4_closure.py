"""Regression coverage for the CX-FIX4 closure findings."""
import unittest

import approvals
import gate_rules


class ClosureCases(unittest.TestCase):
    def denied(self, command, remote):
        return bool(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'command': command}},
                                               remote, gate_rules.STRICT_RULES))

    def test_c1_always_on_supervisor_in_shell_forms(self):
        for command in ('`ha su info`', '{ ha supervisor info; }', 'if ha su info; then :; fi',
                        'exec ha su info', 'time ha supervisor info', 'nice ha su info',
                        'timeout 5 ha su info', 'watch ha su info', 'sudo -u root ha su info',
                        'env -u X ha supervisor info', "bash -lc 'ha su info'", 'eval ha su info',
                        'find . -exec ha su info ;', 'python3 -c "import os;os.system(\'ha su info\')"'):
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertTrue(self.denied(command, remote))

    def test_c2_remote_families_anywhere_and_flags(self):
        for command in ('`ha core info`', '{ ha core info; }', 'if ha core info; then :; fi',
                        'exec ha core info', 'time ha core info', 'nice ha core info',
                        'timeout 5 ha core info', 'watch ha core info', 'sudo -u root ha core info',
                        'env -u X ha core info', "bash -lc 'ha core info'", 'eval ha core info',
                        'find . -exec ha core info ;', 'python3 -c "import os;os.system(\'ha core info\')"',
                        'ha --api-token x core restart', 'ha --endpoint x core restart',
                        'ha --config c core restart', 'sh -xc "ha core info"',
                        "bash -lc 'ha host info'", "bash -c -- 'ha core info'",
                        "zsh -c 'ha core info'", "busybox sh -c 'ha core info'", 'echo ha core'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command, True))
                self.assertFalse(self.denied(command, False))

    def test_c3_directory_and_requirements_forms(self):
        for command in ('(cd /etc/codex && cat requirements.toml)',
                        '{ cd /etc/codex; cat requirements.toml; }',
                        'pushd /etc/codex; cat requirements.toml',
                        'env -C /etc/codex cat requirements.toml',
                        'cat /etc/codex/{requirements,x}.toml',
                        'pushd /etc/codex; cat req*', 'env -C /etc/codex cat *.toml'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_c4_globs_do_not_cross_directories(self):
        for command in ('ls /*', 'ls /e*'):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))
        # Directory targets and matching directory globs are now protected by N4.
        for command in ('cat /etc/codex/*', 'cat /etc/co*/req*', 'cat /etc/*',
                        'ls /etc/codex', 'cat /etc/codex/requirements.toml.bak'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))
