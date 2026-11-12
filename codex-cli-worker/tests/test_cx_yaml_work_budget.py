"""Work limits remain stable under load and unknown dependencies cannot approve edits."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_cx_yaml as fixtures
import test_cx_yaml_review as large
import gate_rules as rules
import remote_control as rc


class ScaledClock:
    def monotonic(self):
        return time.monotonic() * 4

    def __getattr__(self, name):
        return getattr(time, name)


class WorkBudgetTests(unittest.TestCase):
    setUp = fixtures.YamlTests.setUp
    write = fixtures.YamlTests.write
    hook = fixtures.YamlTests.hook

    def test_scaled_clock_and_zero_composes_on_second_idle_scan(self):
        self.write('automations.yaml', large.AUTOMATIONS)
        self.write('scripts.yaml', large.SCRIPTS)
        self.write('configuration.yaml',
                   'automation: !include automations.yaml\nscript: !include scripts.yaml\n'
                   'packages: !include_dir_named packages\n')
        self.write('packages/commands.yaml', 'shell_command: !include ../commands.txt\n')
        self.write('commands.txt', 'sample: echo before\n')
        watch = rc.ConfigWatch(self.root, Path(self.tmp.name) / 'baseline')

        with patch.object(rules, 'time', ScaledClock()):
            watch.take_baseline()
            for _ in range(3):
                self.assertEqual(watch.new_findings(remote_on=True), [])
            with patch.object(rules, '_compose_yaml', wraps=rules._compose_yaml) as compose:
                self.assertEqual(watch.new_findings(remote_on=True), [])
                self.assertEqual(compose.call_count, 0)
            for text in ('shell_command:\n  sample: echo sample\n',
                         'shell_command: {sample: echo sample}\n'):
                self.write('planted.yaml', text)
                findings = watch.new_findings(remote_on=True)
                self.assertTrue(any(item['file'] == 'planted.yaml' for item in findings))
                self.assertFalse((self.root / 'planted.yaml').exists())

    def test_flow_comma_list_finds_the_refused_script(self):
        text = '- action: script.turn_on\n  data: {entity_id: "script.ordinary, SCRIPT.sample"}\n'
        findings = rules.find_refused(text, self.policy, {'sample'})
        self.assertTrue(any('script.sample' in item.reason for item in findings))

    def test_over_limit_script_map_cannot_authorize_a_call(self):
        self.write('scripts.yaml', 'sample: {sequence: [{action: shell_command.sample}]}\n')
        original = rules._compose_yaml

        def over_limit(text):
            if text.startswith('sample:'):
                raise rules.YamlBudgetError('test node limit')
            return original(text)

        with patch.object(rules, '_compose_yaml', side_effect=over_limit):
            reason = self.hook('caller.yaml', '[]\n', '- {action: script.sample}\n')
        self.assertIn('scripts.yaml', reason)
        self.assertIn('refused', reason)

    def test_uncomposed_include_root_is_not_silently_skipped(self):
        self.write('configuration.yaml', 'shell_command: !include commands.txt\n')
        self.write('commands.txt', 'sample: echo sample\n')
        original = rules._compose_yaml

        def over_limit(text):
            if '!include' in text:
                raise rules.YamlBudgetError('test node limit')
            return original(text)

        with patch.object(rules, '_compose_yaml', side_effect=over_limit):
            reason = self.hook('ordinary.yaml', '# before\n', '# after\n')
        self.assertIn('configuration.yaml', reason)
        self.assertIn('refused', reason)

    def test_script_bodies_use_nodes_without_serializing_and_reparsing(self):
        text = 'sample: {sequence: [{action: shell_command.sample}]}\n'
        with patch.object(rules.yaml, 'serialize', side_effect=AssertionError('body serialized')):
            self.assertIn('sample', rules.refused_scripts({'scripts.yaml': text}, self.policy))

    def test_baseline_uses_included_bytes_already_read(self):
        self.write('configuration.yaml', 'shell_command: !include commands.txt\n')
        self.write('commands.txt', 'sample: echo sample\n')
        original = rules.read_config_bytes
        reads = []

        def read(root, rel, *args):
            if rel == 'commands.txt':
                reads.append(rel)
                if len(reads) > 1:
                    raise FileNotFoundError('removed between reads')
            return original(root, rel, *args)

        with patch.object(rules, 'read_config_bytes', side_effect=read):
            rc.ConfigWatch(self.root, Path(self.tmp.name) / 'baseline').take_baseline()
        self.assertEqual(reads, ['commands.txt'])

    def test_size_refusal_explains_the_limit(self):
        self.write('large.yaml', '#' * (rules.HOOK_MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'larger than 512 KiB'):
            rules.read_config_bytes(self.root, 'large.yaml', rules.HOOK_MAX_BYTES)

    def test_five_times_reference_documents_fit_the_work_limit(self):
        self.assertIsNotNone(rules.yaml_node(large.AUTOMATIONS * 5))
        self.assertIsNotNone(rules.yaml_node(large.SCRIPTS * 5))


if __name__ == '__main__':
    unittest.main()
