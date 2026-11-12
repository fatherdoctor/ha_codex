"""Regression tests for the CX-FIX4.f5 closure findings."""
import pathlib
import unittest

import approvals
import gate_rules


class ClosureCases(unittest.TestCase):
    def denied(self, command, remote):
        return bool(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'command': command}},
                                               remote, gate_rules.STRICT_RULES))

    def test_supervisor_text_floor_and_unrelated_commands(self):
        refused = ('ha su info', "grep 'ha su' x", 'echo ha su', '2>/dev/null ha su info',
                   'time -p ha su info', 'doas ha su info', 'ha\nsu info',
                   'ha --api-token x su info')
        allowed = ('cat ha.log | grep supervisor', 'docker logs ha | grep supervisor',
                   'git commit -m "ha: fix su handling"')
        for remote in (False, True):
            for command in refused:
                with self.subTest(command=command, remote=remote):
                    self.assertTrue(self.denied(command, remote))
            for command in allowed:
                with self.subTest(command=command, remote=remote):
                    self.assertFalse(self.denied(command, remote))
        self.assertFalse(self.denied('ha core logs | grep supervisor', False))

    def test_wrapped_writes_and_find_exec(self):
        refused = ('nohup cp -r /tmp/codex /etc/', 'timeout 60 cp -r /tmp/codex /etc/',
                   'timeout -k 5 60 cp -r /tmp/codex /etc/', 'nice -n 5 cp x /etc',
                   'ionice -c 3 -n 1 cp x /etc', 'doas cp x /etc', 'setsid cp x /etc',
                   'stdbuf -o L cp x /etc', 'xargs -t cp -r -t /etc',
                   'echo x | xargs -I{} cp -r {} /etc/', 'cp -rt /etc /tmp/codex',
                   'mv -vt/etc /tmp/codex', 'install -Dt /etc /tmp/codex',
                   'find /tmp -name codex -exec cp -r {} /etc \\;',
                   'find /tmp -execdir cp -t /etc {} +', 'nice find / -delete',
                   'timeout 60 find / -name x -delete')
        for command in refused:
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))
        for command in ('nohup cp /etc/hosts /tmp/copy', 'timeout 60 find /tmp -name x',
                        'xargs -t cp x /tmp', 'cp -rt /tmp /config/file'):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))

    def test_tar_extract_but_not_archive_creation(self):
        for command in ('tar -C /etc -xf x.tar', 'cd /etc && tar xf x.tar',
                        'tar --extract -C / x.tar', 'tar --get --directory /etc -f x.tar'):
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))
        for command in ('cd /etc && tar cf /tmp/e.tar .',
                        'cd / && tar czf /backup/x.tgz homeassistant',
                        'tar -C /etc -cf /tmp/e.tar .'):
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))

    def test_doc_warning_follows_the_who_can_answer_paragraph(self):
        text = (pathlib.Path(__file__).resolve().parents[1] / 'DOCS.md').read_text()
        protected = text.index('**Protected policy.**')
        who = text.index('**Who can answer.**', protected)
        warning = 'This check reads only the command text. It cannot see a path built while the command runs, a symlink, or a relative path from an unknown folder. Such a command keeps **Allow once**, so read it before you allow it.'
        self.assertIn(warning, text[who:])
        self.assertNotIn('This check reads only the command text.', text[protected:who])
        self.assertNotIn('The Allow-button check', text)


if __name__ == '__main__':
    unittest.main()
