"""Peer-review dependency and identical-graph regressions; synthetic files only."""
import unittest
from unittest.mock import patch
import test_cx_yaml as fixtures
import gate_rules as rules


class PeerFixTests(unittest.TestCase):
    setUp = fixtures.YamlTests.setUp
    write = fixtures.YamlTests.write
    hook = fixtures.YamlTests.hook

    def call_sample(self):
        return self.hook('ordinary.yaml', '# before\n', '- {action: script.sample}\n')

    def test_non_yaml_script_include_is_resolved_and_refused_by_script(self):
        self.write('configuration.yaml', 'script: !include actions.inc\n')
        self.write('actions.inc', 'sample: {sequence: [{action: shell_command.example}]}\n')
        self.assertIn('script.sample', self.call_sample())

    def test_missing_script_include_is_refused_by_target(self):
        for target in ('missing.yaml', 'missing.inc'):
            with self.subTest(target=target):
                self.write('configuration.yaml', 'script: !include ' + target + '\n')
                reason = self.call_sample()
                self.assertIn(target, reason)
                self.assertIn('missing', reason)

    def test_unreadable_or_uncheckable_script_include_refuses(self):
        self.write('configuration.yaml', 'script: !include actions.inc\n')
        self.write('actions.inc', 'sample: {sequence: []}\n')
        original = rules.read_config_bytes
        def unavailable(root, rel, maximum):
            if rel == 'actions.inc':
                raise OSError('synthetic denied read')
            return original(root, rel, maximum)
        with patch.object(rules, 'read_config_bytes', side_effect=unavailable):
            self.assertIn('actions.inc', self.call_sample())
        for text in ('sample: [', '[not, a, script, map]'):
            self.write('actions.inc', text)
            self.assertIn('actions.inc', self.call_sample())

    def test_unresolved_nested_script_includes_require_manual_review(self):
        self.write('configuration.yaml', 'script: !include scripts.inc\n')
        for body in ('!include body.inc', '{sequence: !include body.inc}',
                     '{sequence: [!include body.inc]}'):
            with self.subTest(body=body):
                self.write('scripts.inc', 'sample: ' + body + '\n')
                self.write('body.inc', 'sequence: [{action: shell_command.example}]\n')
                reason = self.call_sample()
                self.assertIn('scripts.inc', reason)
                self.assertIn('script include', reason)

    def test_benign_non_yaml_script_and_its_ordinary_edit_remain_available(self):
        self.write('configuration.yaml', 'script: !include actions.inc\n')
        before = 'sample: {sequence: [{action: light.turn_on}]}\n'
        self.write('actions.inc', before)
        self.assertEqual(self.call_sample(), '')
        self.assertEqual(self.hook('actions.inc', before, before + '# comment\n'), '')
        self.assertIn('shell_command', self.hook('actions.inc', before,
            'sample: {sequence: [{action: shell_command.example}]}\n'))

    def test_identical_graphs_are_not_rewalked_for_each_inventory_file(self):
        text = '[' + ','.join(['{}'] * 1500) + ']'
        for n in range(100):
            self.write(f'list_{n}.yaml', text)
        original = rules.yaml_mappings
        visits = []
        def counted(*args, **kwargs):
            for node in original(*args, **kwargs):
                visits.append(1)
                yield node
        with patch.object(rules, 'yaml_mappings', side_effect=counted):
            self.assertEqual(self.hook('ordinary.yaml', '# before\n', '# after\n'), '')
        self.assertLess(len(visits), 5000)

    def test_large_leaf_include_retains_dependency_without_spending_edge_walk_budget(self):
        texts = {'configuration.yaml': 'shell_command: !include commands.inc\n',
                 'commands.inc': 'sample: [' + ','.join(['ordinary'] * 1000) + ']'}
        with patch.object(rules, 'INCLUDE_MAX_NODES', 20):
            found = rules.included_definitions(texts, self.policy, strict=True)
        self.assertEqual(found['commands.inc'], {'shell_command'})

    def test_identical_include_graph_keeps_each_files_relative_dependency(self):
        text = 'script: !include actions.inc\n'
        self.write('one/config.yaml', text)
        self.write('two/config.yaml', text)
        self.write('one/actions.inc', 'first: {sequence: [{action: light.turn_on}]}\n')
        self.write('two/actions.inc', 'sample: {sequence: [{action: shell_command.example}]}\n')
        self.assertIn('script.sample', self.call_sample())
