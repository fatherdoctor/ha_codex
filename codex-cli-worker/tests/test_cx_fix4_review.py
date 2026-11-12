"""Regression cases from the CX-FIX4 review, with no live Home Assistant calls."""
import unittest

import approvals
import gate_rules


class ReviewCases(unittest.TestCase):
    def denied(self, command, remote=True, event='PreToolUse'):
        return bool(gate_rules.check_tool_call(event, {'tool_input': {'command': command}},
                                               remote, gate_rules.STRICT_RULES))

    def test_remote_off_preserves_ordinary_approval(self):
        for command in ('ha core restart', 'ha addons info', 'ha host reboot',
                        'ha backups list', 'ha network info'):
            with self.subTest(command=command):
                self.assertFalse(self.denied(command, False))
                self.assertTrue(self.denied(command, True))
        for command in ('ha supervisor info', 'ha su info'):
            self.assertTrue(self.denied(command, False), command)

    def test_command_position_and_flags(self):
        for command in ('ha --log-level debug core restart', 'ha --raw-json os info',
                        'ha -v host info', '"ha" core restart', 'ha "core" restart',
                        "ha 'addons' list", 'ha\\\ncore stop', 'ha  addons',
                        'ha\taddons', 'ha\naddons', '/usr/bin/ha core restart',
                        'env ha core restart', 'sudo ha core restart', 'command ha core restart',
                        'xargs ha core restart', 'VAR=1 ha core restart',
                        'bash -c "ha core restart"', "sh -c 'ha core restart'", 'HA CORE restart',
                        'echo ok | ha core restart', 'true; ha core restart',
                        'true && ha core restart', 'false || ha core restart',
                        'echo ha\nha core restart', 'ha apps list'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command))

    def test_non_commands_are_not_ha_invocations(self):
        for command in ('cat ha-notes', 'sha256sum x', 'sha core', 'chain addons', 'ha-core restart',
                        'ha_host info', 'ls /ha/core', 'ha.os', 'ha hostname',
                        'ha -h', 'ha --help', 'ha info', 'ha states list'):
            with self.subTest(command=command):
                self.assertFalse(self.denied(command))
        # The closure ruling accepts these conservative refusals with remote on.
        for command in ('echo ha core', 'grep ha core file', 'echo "the ha host is down"',
                        'git log --format=%ha host', 'x.ha core'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command))
                self.assertFalse(self.denied(command, False))

    def test_permission_requests_and_other_families(self):
        for command in ('ha apps info', 'ha core info', 'ha os info', 'ha hardware info',
                        'ha backups list', 'ha supervisor info'):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command, event='PermissionRequest'))
        for family in ('store', 'jobs', 'resolution', 'dns', 'audio', 'multicast', 'observer',
                       'cli', 'mounts', 'docker', 'security', 'authentication', 'banner'):
            with self.subTest(family=family):
                self.assertFalse(self.denied('ha ' + family + ' info'))

    def test_requirements_spellings(self):
        for command in ('cat /etc/codex/requirements.toml',
                        'cat /etc//codex/"requirements.toml"',
                        'cat /ETC/CODEX/REQUIREMENTS.TOML',
                        'cd /etc/codex && cat requirements.toml',
                        'cat /etc/co*/requirements.toml', 'cat /etc/codex/req*',
                        'cat /etc/codex/../codex/requirements.toml',
                        'ln -s /etc/codex/requirements.toml /tmp/r',
                        'cp /etc/codex/requirements.toml /tmp/copy',
                        'sed -i x /etc/codex/requirements.toml',
                        'echo x > /etc/codex/requirements.toml',
                        'tee /etc/codex/requirements.toml'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))
        for command in ('cat /etc/codex/requirements.toml.bak',
                        'cat /etc/codex/requirements.toml.example', 'cat /config/requirements.toml',
                        'cat /tmp/r'):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))
