"""Bounded graph discovery, capacity behavior and per-scan retention regressions."""
import collections
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import test_cx_yaml as fixtures
import gate_rules as rules
import remote_control as rc


class GraphReviewTests(unittest.TestCase):
    setUp = fixtures.YamlTests.setUp
    write = fixtures.YamlTests.write
    hook = fixtures.YamlTests.hook

    def ordinary(self, remote=True):
        command = '*** Begin Patch\n*** Add File: ordinary.yaml\n+# ordinary\n*** End Patch\n'
        return rules.check_tool_call('PreToolUse', {'cwd': str(self.root),
            'tool_input': {'command': command}}, remote, self.policy)

    def test_sixty_include_levels_do_not_repeat_the_large_graph_walk(self):
        self.write('configuration.yaml', 'script: !include c0.inc\nzz: [' + ','.join(['1'] * 140000) + ']\n')
        for n in range(60):
            self.write(f'c{n}.inc', f'k{n}: !include c{n+1}.inc\n' if n < 59 else 'last: {sequence: []}\n')
        original = rules._compose_yaml
        for remote in (False, True):
            walked = []
            class CountedValues(list):
                def __iter__(self):
                    walked.append(len(self))
                    return super().__iter__()
            def counted(text):
                node = original(text)
                if isinstance(node, rules.yaml.MappingNode):
                    for key, value in node.value:
                        if isinstance(value, rules.yaml.SequenceNode) and len(value.value) == 140000:
                            value.value = CountedValues(value.value)
                return node
            with self.subTest(remote=remote), patch.object(rules, '_compose_yaml', side_effect=counted):
                self.ordinary(remote)
                self.assertLess(sum(walked), 1000000, 'include passes repeatedly walk the large list')

    def test_labeled_scripts_resolve_inline_yaml_and_non_yaml_includes(self):
        for key, target in [('script manual', 'manual.yaml'), ('script 2', 'manual.inc'), ('script inline', None)]:
            with self.subTest(key=key):
                body = 'sample: {sequence: [{action: shell_command.example}]}'
                self.write('configuration.yaml', key + ': ' + ('!include ' + target if target else '{' + body + '}') + '\n')
                if target:
                    self.write(target, body + '\n')
                self.assertIn('script.sample', self.hook('call.yaml', '# old\n', '- {action: script.sample}\n'))

    def test_total_capacity_has_remote_on_refusal_and_remote_off_line_fallback(self):
        self.write('scripts.yaml', 'sample: {sequence: []}\n' + '# padding\n' * 60)
        with patch.object(rules, 'HOOK_COMPOSE_MAX_BYTES', 80):
            reason = self.ordinary(True)
            self.assertIn('scripts.yaml', reason)
            self.assertIn('more YAML than the check reads in one call', reason)
            self.assertEqual(self.ordinary(False), '')
            command = '*** Begin Patch\n*** Add File: planted.yaml\n+shell_command: {sample: echo sample}\n*** End Patch\n'
            reason = rules.check_tool_call('PreToolUse', {'cwd': str(self.root),
                'tool_input': {'command': command}}, False, self.policy)
            self.assertIn('shell_command', reason)

    def test_unrelated_documents_do_not_consume_the_hook_parse_total(self):
        self.write('unrelated.yaml', 'ordinary: [' + ','.join(['a'] * 1000) + ']')
        with patch.object(rules, 'HOOK_MAX_NODES', 30):
            self.assertEqual(self.ordinary(True), '')

    def test_inventory_over_read_cap_is_not_silently_skipped(self):
        self.write('configuration.yaml', 'script: {sample: {sequence: [{action: shell_command.example}]}}\n' + '# padding\n' * 850000)
        self.assertIn('configuration.yaml', self.ordinary(True))
        self.assertIn('more YAML than the check reads in one call', self.ordinary(True))
        self.assertEqual(self.ordinary(False), '')

    def test_changed_watch_scan_composes_each_distinct_large_document_once(self):
        self.write('large.yaml', 'ordinary: [' + ','.join(['a'] * 18000) + ']')
        watch = rc.ConfigWatch(self.root, Path(self.tmp.name) / 'baseline.json')
        watch.take_baseline(); watch.new_findings(); watch.new_findings()
        self.write('planted.yaml', 'shell_command: {sample: echo sample}\n')
        original = rules._compose_yaml
        calls = collections.Counter()
        def counted(text):
            calls[hashlib.sha256(text.encode()).digest()] += 1
            return original(text)
        with patch.object(rules, '_compose_yaml', side_effect=counted):
            self.assertTrue(watch.new_findings())
        self.assertTrue(calls)
        self.assertEqual(max(calls.values()), 1)

    def test_new_file_is_not_composed_twice_for_a_final_newline(self):
        original = rules._compose_yaml
        calls = []
        def counted(text):
            if 'light.turn_on' in text:
                calls.append(text)
            return original(text)
        command = '*** Begin Patch\n*** Add File: new.yaml\n+action: light.turn_on\n*** End Patch\n'
        with patch.object(rules, '_compose_yaml', side_effect=counted):
            reason = rules.check_tool_call('PreToolUse', {'cwd': str(self.root),
                'tool_input': {'command': command}}, True, self.policy)
        self.assertEqual(reason, '')
        self.assertEqual(len(calls), 1)

    def test_included_target_owns_its_capacity_message(self):
        self.write('configuration.yaml', 'script: !include scripts.yaml\n')
        self.write('scripts.yaml', 'sample: {sequence: []}\n' + '# padding\n' * 30)
        with patch.object(rules, 'HOOK_COMPOSE_MAX_BYTES', 100):
            self.assertIn('scripts.yaml: ', self.ordinary(True))

    def test_patch_adding_a_nested_script_include_is_refused(self):
        self.write('configuration.yaml', 'script: !include scripts.inc\n')
        before = 'sample: {sequence: []}\n'
        self.write('scripts.inc', before)
        self.write('body.inc', 'sequence: [{action: shell_command.example}]\n')
        reason = self.hook('scripts.inc', before, before + 'second: !include body.inc\n')
        self.assertIn('scripts.inc', reason)
        self.assertIn('nested script include', reason)

    def test_cache_rejects_one_graph_above_the_cost_limit(self):
        node = rules.yaml_node('sample: [one, two, three]\n')
        cache = rules.YamlCache(max_cost=100)
        cache.put(b'key', 25, (node, ''))
        self.assertEqual(cache.results, {})
        self.assertEqual(cache.cost, 0)

    def test_multiple_documents_explain_the_single_document_requirement(self):
        self.write('scripts.yaml', 'sample: {sequence: []}\n---\nsecond: {}\n')
        self.assertIn('single-document', self.ordinary(True))


if __name__ == '__main__':
    unittest.main()
