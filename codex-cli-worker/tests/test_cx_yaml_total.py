"""One hook budget includes every composition, including rejected inputs."""
import unittest
from unittest.mock import patch

import test_cx_yaml as fixtures
import gate_rules as rules


class TotalBudgetTests(unittest.TestCase):
    setUp = fixtures.YamlTests.setUp
    write = fixtures.YamlTests.write
    hook = fixtures.YamlTests.hook

    def run_hook(self, before='# before\n', after='# after\n'):
        scans = []
        original = rules.ParseScan

        def capture(*args, **kwargs):
            scan = original(*args, **kwargs)
            scans.append(scan)
            return scan

        with patch.object(rules, 'ParseScan', side_effect=capture):
            reason = self.hook('ordinary.yaml', before, after)
        self.assertEqual(len(scans), 1)
        return reason, scans[0]

    def assert_bounded(self, scan):
        self.assertLessEqual(scan.nodes, rules.HOOK_MAX_NODES)
        self.assertLessEqual(scan.bytes, rules.HOOK_COMPOSE_MAX_BYTES)

    def test_four_previously_allowed_lists_refuse_planted_file_within_total(self):
        for n in range(4):
            self.write(f'lists/list_{n}.yaml', f'zz_list_{n}: [' + ','.join([str(n)] * 124000) + ']\n')
        reason, scan = self.run_hook(after='shell_command: {sample: echo sample}\n')
        self.assertIn('refused', reason)
        self.assert_bounded(scan)

    def test_million_item_inventory_refuses_early_by_count(self):
        self.write('big.yaml', '[' + ','.join(['a'] * 1000100) + ']')
        reason, scan = self.run_hook()
        self.assertIn('big.yaml', reason)
        self.assertIn('refused', reason)
        self.assert_bounded(scan)
        self.assertLess(scan.nodes, 1000100)

    def test_byte_total_is_shared_across_distinct_documents(self):
        self.write('one.yaml', '# first\n' + 'x: ' + 'a' * 300000)
        self.write('two.yaml', '# second\n' + 'x: ' + 'b' * 300000)
        with patch.object(rules, 'HOOK_COMPOSE_MAX_BYTES', 400000):
            reason, scan = self.run_hook()
            self.assertIn('refused', reason)
            self.assert_bounded(scan)

    def test_cache_bounds_retained_graph_cost_and_replacement(self):
        cache = rules.YamlCache()
        node = rules.yaml_node('[' + ','.join(['x'] * 12000) + ']')
        for n in range(8):
            cache.put(bytes([n]), 24001, (node, ''))
        self.assertLessEqual(cache.cost, rules.YAML_CACHE_MAX_COST)
        self.assertLess(len(cache.results), 8)
        before = cache.cost
        cache.put(bytes([7]), 24001, (node, ''))
        self.assertEqual(cache.cost, before)

    def test_transitive_script_resolution_spends_the_same_node_total(self):
        scripts = ''.join(
            f's{n}: {{sequence: [{{action: script.s{n + 1}}}]}}\n'
            for n in range(100))
        scripts += 's100: {sequence: [{action: shell_command.sample}]}\n'
        self.write('scripts.yaml', scripts)
        with patch.object(rules, 'HOOK_MAX_NODES', 2000):
            reason, scan = self.run_hook()
            self.assertIn('scripts.yaml', reason)
            self.assertIn('total hook work limit', reason)
            self.assert_bounded(scan)

    def test_hook_without_fast_loader_refuses_before_parsing(self):
        with patch.object(rules, 'YAML_LOADER', rules.yaml.SafeLoader):
            with patch.object(rules.yaml, 'parse', side_effect=AssertionError('slow parse')):
                reason, scan = self.run_hook(after='name: sample\n')
        self.assertIn('fast safe YAML parser is unavailable', reason)
        self.assertEqual(scan.nodes, 0)

    def test_unreadable_file_message_names_manual_repair(self):
        self.write('broken.yaml', 'a: [1, 2\nb: script.sample\n')
        reason, _ = self.run_hook()
        self.assertIn('broken.yaml', reason)
        self.assertIn('could not be read', reason)
        self.assertIn('person', reason)
        self.assertNotIn('script definitions', reason)


if __name__ == '__main__':
    unittest.main()
