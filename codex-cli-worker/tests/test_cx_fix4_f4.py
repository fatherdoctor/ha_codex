"""Regression cases for the CX-FIX4.f4 closure."""
import unittest

import approvals
import gate_rules


class FinalRoundCases(unittest.TestCase):
    def denied(self, command, remote):
        return bool(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'command': command}},
                                               remote, gate_rules.STRICT_RULES))

    def test_g1_find_and_requirement_arguments(self):
        for command in ('find -L /etc -name requirements.toml -delete',
                        'cd /etc && find . -name requirements.toml -delete',
                        'cd / && find etc -name requirements.toml -delete',
                        'find /e* -name requirements.toml -delete',
                        'find /etc -regex ".*requirements.toml" -delete',
                        'find / -name configuration.yaml', 'find /etc -name something',
                        'cd /etc && find . -name something',
                        'cat /tmp/requirements.toml', 'find /config -name req*.toml',
                        'find /config -path "*/requirements.toml"',
                        "find /config -regex '.*requirements[.]toml'", 'find /config -name r?quirements.toml'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_g2_write_destinations(self):
        for command in ('cp -r /tmp/codex /etc/', 'cp -r codex /etc', 'mv /tmp/codex /etc',
                        'tar -C/etc -xf x.tar codex', 'cd /etc && tar xf x.tar',
                        'unzip x.zip -d /etc', 'cd /etc && unzip x.zip',
                        'cp x /', 'mv x /etc', 'install x /etc/', 'rsync x /etc',
                        'ln -s x /etc', 'cp -t /etc x', 'cd / && cp x etc',
                        'tar -C / -xf x.tar', 'unzip x.zip -d /',
                        'sudo -u root cp x /etc', "sh -c 'cp x /etc'",
                        "cd /etc && bash -c 'cp x .'", 'env -C /etc cp x .',
                        'cd /etc && cp /tmp/a /tmp/b', 'cd / && mv /tmp/a /tmp/b',
                        'cp x /e*', 'unzip x.zip -d /et?', 'cp x //', 'find // -name other'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))
        for command in ('cat /etc/hosts', 'ls /etc', 'cp /etc/hosts /tmp/copy',
                        'find /config -name configuration.yaml'):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))

    def test_g4_unrelated_simple_commands(self):
        for command in ('ha core logs | grep -i supervisor', 'ha core logs 2>&1 | grep -i su',
                        'cat ha.log | grep supervisor', 'docker logs ha | grep supervisor',
                        'git commit -m "ha: fix su handling"', 'ls /usr/share/ha-assets/su/',
                        'echo ha su', 'ha info; echo supervisor', 'ha\nsu info'):
            with self.subTest(command=command):
                self.assertFalse(self.denied(command, False))
                if not command.startswith('ha core logs'):
                    self.assertFalse(self.denied(command, True))

    def test_g4_n1_forms_always_refused(self):
        for command in ('ha --api-token x supervisor restart', 'ha --api-token x su info',
                        'ha --endpoint e supervisor info', 'ha --config /tmp/c.yaml supervisor options',
                        'sh -c "ha --api-token x su info"', 'h\\a --api-token x s\\u info',
                        "ha --api-token x 's'u info", 'ha --api-token x "supervisor" info',
                        'ha --api-token $(cat /tmp/t) su info', 'ha --api-token x\\ y su info',
                        'HA --api-token x SU info', 'ha\t--api-token\tx\tsu info',
                        'ha --api-token x\\\n su info', "bash -lc 'ha --api-token x su info'",
                        'ha --api-token=x su info', 'ha --api-token ${T:-x} su info',
                        'ha -- su info', 'xargs ha --api-token x su info',
                        '/usr/bin/ha --api-token x su info', '`ha --api-token x su info`',
                        'python3 -c "import os;os.system(\'ha --api-token x su info\')"',
                        "eval 'ha --api-token x su info'", 'xargs -r ha su info',
                        'nice -n 5 ha su info', 'ha --api-token "$(printf \')\')" su info',
                        './ha su info', 'bin/ha --config x supervisor info',
                        'h"a" --api-token x s"u"pervisor info'):
            for remote in (False, True):
                with self.subTest(command=command, remote=remote):
                    self.assertTrue(self.denied(command, remote))

    def test_g4_command_wrappers_and_boundaries(self):
        wrappers = ('{}', 'sudo {}', 'env {}', 'command {}', 'exec {}', 'time {}',
                    'nice {}', 'timeout 5 {}', 'watch {}', 'eval {}',
                    'xargs {}', 'true; {}', 'true && {}', 'true | {}', '({})', '`{}`')
        for wrapper in wrappers:
            for family in ('su', 'supervisor', 'core', 'host'):
                command = wrapper.format('ha ' + family + ' info')
                for remote in (False, True):
                    with self.subTest(command=command, remote=remote):
                        self.assertEqual(self.denied(command, remote), remote or family in ('su', 'supervisor'))
