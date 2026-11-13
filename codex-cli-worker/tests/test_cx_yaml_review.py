"""Large configuration and case-folding regressions from the parsed-check review."""
from pathlib import Path
import unittest
import time
from unittest.mock import patch
import test_cx_yaml as fixtures
import gate_rules as rules
import remote_control as rc
AUTOMATIONS = '- alias: Sample\n  trigger: []\n  action: [{action: light.turn_on, target: {entity_id: light.sample}}]\n' * 6300
SCRIPTS = ''.join((f'sample_{i}:\n  alias: Sample\n  sequence: [{{action: light.turn_on}}]\n' for i in range(3500)))

class ReviewTests(unittest.TestCase):
    setUp = fixtures.YamlTests.setUp
    write = fixtures.YamlTests.write
    hook = fixtures.YamlTests.hook
    exchange = fixtures.YamlTests.exchange

    def large_config(self):
        self.write('automations.yaml', AUTOMATIONS)
        self.write('scripts.yaml', SCRIPTS)
        self.write('configuration.yaml', 'automation: !include automations.yaml\nscript: !include scripts.yaml\n')
        self.write('ordinary.yaml', '# before\ninput_boolean: {}\n')

    def test_large_baseline_and_planted_block_and_flow_are_removed(self):
        self.large_config()
        watch = rc.ConfigWatch(self.root, Path(self.tmp.name) / 'large-baseline')
        watch.take_baseline()
        self.assertTrue(watch.has_baseline())
        for text in ('shell_command:\n  sample: echo sample\n', 'shell_command: {sample: echo sample}\n'):
            self.write('planted.yaml', text)
            found = watch.new_findings(remote_on=True)
            self.assertTrue(any((x['file'] == 'planted.yaml' and x.get('undone') == 'removed' for x in found)))
            self.assertFalse((self.root / 'planted.yaml').exists())

    def test_large_hook_allows_comment_and_new_file_but_names_flow_refusal(self):
        self.large_config()
        self.assertEqual(self.hook('ordinary.yaml', '# before\ninput_boolean: {}\n', '# after\ninput_boolean: {}\n'), '')
        body = '*** Begin Patch\n*** Add File: new.yaml\n+input_boolean: {}\n*** End Patch\n'
        self.assertEqual(rules.check_tool_call('PreToolUse', {'cwd': str(self.root), 'tool_input': {'command': body}}, True, self.policy), '')
        reason = self.hook('planted.yaml', 'input_boolean: {}\n', 'shell_command: {sample: echo sample}\n')
        self.assertIn('shell_command', reason)
        self.assertIn('planted.yaml', reason)
        self.assertNotIn('cannot be checked safely', reason)

    def test_capital_spellings_through_hook_exchange_and_watch(self):
        self.write('scripts.yaml', 'sample:\n  sequence: [{action: shell_command.sample}]\n')
        for i, text in enumerate(('- action: SHELL_COMMAND.x\n', '- {action: Shell_Command.X}\n', '- action: script.turn_on\n  target: {entity_id: " Script.sample "}\n')):
            with self.subTest(text=text):
                name = f'casefold_{i}.yaml'
                before = 'input_boolean: {}\n'
                self.assertTrue(rules.find_refused(text, self.policy, {'sample'}))
                self.assertTrue(self.hook(name, before, text))
                result, current = self.exchange(name, before, text)
                self.assertTrue(result)
                self.assertEqual(current, before)
                p = self.write(name, before)
                watch = rc.ConfigWatch(self.root, Path(self.tmp.name) / f'casefold-base{i}')
                watch.take_baseline()
                p.write_text(text)
                self.assertTrue(any((x['file'] == name for x in watch.new_findings(remote_on=True))))
                self.assertEqual(p.read_text(), before)

    def test_parser_budget_never_escapes_and_line_pass_still_reports(self):
        text = 'shell_command:\n  sample: echo sample\n'
        slow = 'x: ' + '[' * 80 + '0' + ']' * 80 + '\n' + text
        texts = {'deep.yaml': slow, 'scripts.yaml': slow}
        self.assertTrue(rules.find_refused(slow, self.policy))
        rules.refused_scripts(texts, self.policy)
        rules.included_definitions(texts, self.policy)
        self.write('deep.yaml', slow)
        rules.extend_include_texts(texts, self.root, self.policy)
        watch = rc.ConfigWatch(self.root, Path(self.tmp.name) / 'deep-baseline')
        watch.take_baseline()
        self.assertIn('deep.yaml', watch.scan())

    def test_each_inventory_document_is_composed_once_per_hook(self):
        for i in range(12):
            self.write(f'ordinary_{i}.yaml', f'input_boolean: {{sample_{i}: {{}}}}\n')
        self.write('configuration.yaml', ''.join((f'light_{i}: !include ordinary_{i}.yaml\n' for i in range(12))))
        original = rules._compose_yaml
        seen = []

        def tracking(text):
            seen.append(text)
            return original(text)
        with patch.object(rules, '_compose_yaml', side_effect=tracking):
            self.assertEqual(self.hook('edit.yaml', '# before\n', '# after\n'), '')
        for i in range(12):
            self.assertEqual(seen.count(f'input_boolean: {{sample_{i}: {{}}}}\n'), 1)

    def test_unfollowable_include_refusal_names_source_file(self):
        self.write('configuration.yaml', 'shell_command: !include secrets.yaml\n')
        reason = self.hook('ordinary.yaml', 'input_boolean: {}\n', 'input_boolean: {sample: {}}\n')
        self.assertIn('configuration.yaml', reason)

    def test_media_inventory_does_not_make_yaml_absence_incomplete(self):
        for i in range(rc.WATCH_MAX_FILES + 1):
            self.write(f'media/item{i}.txt', 'sample')
        watch = rc.ConfigWatch(self.root, Path(self.tmp.name) / 'media-baseline')
        watch.take_baseline()
        self.write('planted.yaml', 'shell_command: {sample: echo sample}\n')
        result = watch.new_findings(remote_on=True)
        self.assertTrue(any((x.get('undone') == 'removed' for x in result)))

    def test_hook_total_refuses_oversized_script_source_but_watch_resolves_it(self):
        self.write('scripts.yaml', 'sample:\n  sequence:\n    - action: shell_command.sample\n' + '# padding\n' * 220000)
        self.assertIn('sample', rules.config_refused_scripts(self.policy))
        for after in ('# after\n', '- action: script.sample\n'):
            reason = self.hook('ordinary.yaml', '# before\n', after)
            self.assertIn('scripts.yaml', reason)
            self.assertIn('total hook work limit', reason)

    def test_touched_over_cap_refusal_names_its_file(self):
        text = 'key: value\n' + '# padding\n' * 220000
        self.write('oversized.yaml', text)
        body = '*** Begin Patch\n*** Update File: oversized.yaml\n@@\n-key: value\n+key: after\n*** End Patch\n'
        reason = rules.check_tool_call('PreToolUse', {'cwd': str(self.root), 'tool_input': {'command': body}}, True, self.policy)
        self.assertIn('oversized.yaml', reason)

    def test_scanned_include_bytes_are_hashed_without_a_second_read(self):
        self.write('configuration.yaml', 'shell_command: !include commands.yaml\n')
        self.write('commands.yaml', 'sample: echo sample\n')
        original = rules.read_config_bytes
        reads = []

        def read(root, rel, *args):
            reads.append(rel)
            if rel == 'commands.yaml' and reads.count(rel) > 1:
                raise AssertionError('second read')
            return original(root, rel, *args)
        with patch.object(rules, 'read_config_bytes', side_effect=read):
            rc.ConfigWatch(self.root, Path(self.tmp.name) / 'hash-baseline').scan()
        self.assertEqual(reads.count('commands.yaml'), 1)

    def test_include_body_refusal_names_the_changed_file(self):
        self.write('configuration.yaml', 'shell_command: !include commands.yaml\n')
        before = 'sample: echo before\n'
        after = 'sample: echo after\n'
        reason = self.hook('commands.yaml', before, after)
        self.assertIn('commands.yaml', reason)
        self.assertIn('included shell_command', reason)
