"""Closure regressions: definition bodies stay checked; unchanged calls stay approvable."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gate_rules
import remote_control
import server


PROBES = {
    'changed_shell_command': ('shell_command:\n  sample: echo old\n', 'shell_command:\n  sample: echo changed\n'),
    'new_shell_key': ('shell_command:\n  sample: echo old\n', 'shell_command:\n  sample: echo old\n  another: echo added\n'),
    'changed_platform_command': ('sensor:\n  - platform: command_line\n    command: echo old\n',
                                 'sensor:\n  - platform: command_line\n    command: echo changed\n'),
    'changed_rest_url': ('rest_command:\n  sample:\n    url: /old\n', 'rest_command:\n  sample:\n    url: /changed\n'),
}
FLOW_TARGETS = ('  entity_id: [script.run_shell]\n', '  target: {entity_id: script.run_shell}\n')


class ClosureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.run = self.root / 'run'
        self.run.mkdir()
        self.rules = gate_rules.load_rules()
        for obj, name, value in ((server, 'CONFIG_ROOT', self.config),
                                 (gate_rules, 'CONFIG_ROOT', self.config),
                                 (gate_rules, 'OPTIONS_PATH', self.root / 'absent.json')):
            mock = patch.object(obj, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def check_change(self, probe, check):
        before, after = PROBES[probe]
        target = self.config / 'sample.yaml'
        target.write_text(before)
        manifest = {'sample.yaml': {'sha256': server.file_hash(target)}}
        backup = self.run / server.BACKUP_DIR / 'sample.yaml'
        backup.parent.mkdir()
        backup.write_text(before)
        watch = remote_control.ConfigWatch(self.config, self.root / 'baseline.json')
        watch.take_baseline()
        target.write_text(after)
        if check == 'watch':
            self.assertTrue(watch.new_findings())
            self.assertEqual(target.read_text(), after)
        else:
            result = server.refuse_planted_edits(self.run, {'changed': ['sample.yaml']}, manifest)
            self.assertTrue(result)
            self.assertEqual(result[0]['undone'], 'restored')
            self.assertEqual(target.read_text(), before)

    def test_c1_changed_shell_command_exchange(self):
        self.check_change('changed_shell_command', 'exchange')

    def test_c1_changed_shell_command_watch(self):
        self.check_change('changed_shell_command', 'watch')

    def test_c1_new_shell_key_exchange(self):
        self.check_change('new_shell_key', 'exchange')

    def test_c1_new_shell_key_watch(self):
        self.check_change('new_shell_key', 'watch')

    def test_c1_changed_platform_command_exchange(self):
        self.check_change('changed_platform_command', 'exchange')

    def test_c1_changed_platform_command_watch(self):
        self.check_change('changed_platform_command', 'watch')

    def test_c1_changed_rest_url_exchange(self):
        self.check_change('changed_rest_url', 'exchange')

    def test_c1_changed_rest_url_watch(self):
        self.check_change('changed_rest_url', 'watch')

    def test_c1_other_definition_and_template_bodies_are_checked(self):
        for key in ('command_line', 'rest', 'scrape', 'python_script', 'pyscript'):
            with self.subTest(key=key):
                before = key + ':\n  sample: old\n'
                self.assertTrue(gate_rules.new_refused(before, before.replace('old', 'changed'), self.rules))
        before = '- alias: old\n  action: "{{ selected }}"\n'
        self.assertTrue(gate_rules.new_refused(before, before.replace('old', 'changed'), self.rules))

    def prepare_script(self):
        (self.config / 'scripts.yaml').write_text('run_shell:\n  sequence:\n    - action: shell_command.sample\n')

    def test_c2_flow_targets_hook(self):
        self.prepare_script()
        for target in FLOW_TARGETS:
            with self.subTest(target=target):
                text = '- action: script.turn_on\n' + target
                patch_text = '*** Begin Patch\n*** Add File: sample.yaml\n' + ''.join('+' + l + '\n' for l in text.splitlines()) + '*** End Patch\n'
                self.assertTrue(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'patch': patch_text}}, False, self.rules))

    def test_c2_flow_targets_exchange(self):
        self.prepare_script()
        for line in FLOW_TARGETS:
            with self.subTest(target=line):
                target = self.config / 'sample.yaml'
                target.write_text('- action: script.turn_on\n' + line)
                result = server.refuse_planted_edits(self.run, {'added': ['sample.yaml']}, {})
                self.assertTrue(result)
                self.assertEqual(result[0]['undone'], 'removed')
                self.assertFalse(target.exists())

    def test_c2_flow_targets_watch(self):
        self.prepare_script()
        target = self.config / 'sample.yaml'
        target.write_text('- action: light.turn_on\n')
        watch = remote_control.ConfigWatch(self.config, self.root / 'baseline.json')
        watch.take_baseline()
        for line in FLOW_TARGETS:
            with self.subTest(target=line):
                target.write_text('- action: script.turn_on\n' + line)
                self.assertTrue(watch.new_findings())

    def test_c2_flow_list_with_other_entities_and_both_modes(self):
        for enabled in (False, True):
            for line in ('entity_id: [light.sample, script.run_shell]', *FLOW_TARGETS):
                with self.subTest(enabled=enabled, line=line):
                    self.assertTrue(gate_rules.active_findings(line, self.rules, {'run_shell'}, enabled))
        self.assertEqual(gate_rules.active_findings('description: script.run_shell', self.rules, {'run_shell'}), [])
        self.assertEqual(gate_rules.active_findings('# entity_id: [script.run_shell]', self.rules, {'run_shell'}), [])


if __name__ == '__main__':
    unittest.main()
