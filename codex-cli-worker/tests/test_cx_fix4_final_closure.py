"""Regression coverage for the second CX-FIX4 closure."""
import unittest

import approvals
import gate_rules


class FinalClosureCases(unittest.TestCase):
    def denied(self, command, remote):
        return bool(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'command': command}},
                                               remote, gate_rules.STRICT_RULES))

    def test_n1_supervisor_after_value_flags_is_always_refused(self):
        for flag in ('--api-token', '--endpoint', '--config', '--log-level'):
            for sep in (' ', '='):
                for family in ('supervisor', 'su'):
                    command = f'ha {flag}{sep}value {family} info'
                    for remote in (False, True):
                        with self.subTest(command=command, remote=remote):
                            self.assertTrue(self.denied(command, remote))
        for command in ('ha --api-token x supervisor restart', 'ha --api-token x su info',
                        'ha --endpoint e supervisor info', 'ha --config /tmp/c.yaml supervisor options',
                        'sh -c "ha --api-token x su info"',
                        'ha --api-token "$(cat /tmp/x | head -1)" su info'):
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertTrue(self.denied(command, remote))
        self.assertFalse(self.denied('ha --api-token x core restart', False))
        self.assertTrue(self.denied('ha --api-token x core restart', True))

    def test_n2_unreadable_flag_values_are_refused_for_remote(self):
        for command in ('ha --api-token "$(cat /data/tok)" core restart',
                        'ha --api-token $(cat /tmp/t) core restart',
                        'ha --api-token `cat t` core info', "ha --api-token 'a b' core info",
                        'ha --endpoint=sample --api-token=$(cat t) core info',
                        'ha --config "some path" info', "'h'a --api-token 'a b' info",
                        "ha --config=a' b' info", 'ha --api-token=prefix`cat t` info'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command, True))
                self.assertFalse(self.denied(command, False))

    def test_n3_directory_destinations_and_working_directories(self):
        for command in ('cp /tmp/requirements.toml /etc/codex/',
                        'cp /tmp/requirements.toml /etc/codex',
                        'install -m644 /tmp/requirements.toml /etc/codex/',
                        'cd /etc/codex && cp /tmp/requirements.toml .',
                        'env --chdir=/etc/codex cat requirements.toml',
                        'env -C/etc/codex cat requirements.toml',
                        'env --chdir /etc/codex cat requirements.toml',
                        'env -C /etc/codex cat requirements.toml'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_n4_directory_actions_and_find(self):
        for command in ('rm -rf /etc/codex', 'mv /etc/codex /etc/codex.old',
                        'cd /etc && mv codex x', 'tar -C /etc/codex -xf x.tar',
                        'ln -s /etc/codex /tmp/c && cat /tmp/c/requirements.toml',
                        'find /etc -name requirements.toml -delete',
                        'find / -name requirements.toml -delete',
                        'sudo find /etc -name requirements.toml -delete',
                        'cd -- /etc && mv codex x'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_n5_accepted_remote_false_positives(self):
        for command in ("grep -rn 'ha core' docs", "git log --grep='ha host'", "grep -c 'ha os' x",
                        "echo 'ha backups list'", 'grep "ha apps" README.md', 'ha --help core'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command, True))
                self.assertFalse(self.denied(command, False))
        for remote in (False, True):
            self.assertTrue(self.denied("grep 'ha su' notes.txt", remote))
            self.assertFalse(self.denied('grep supervisor x', remote))
            self.assertFalse(self.denied('grep -i supervisor home-assistant.log', remote))
