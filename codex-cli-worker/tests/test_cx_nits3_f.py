"""Regression checks for Python-definition wording and its documented scope."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gate_rules
import remote_control


class PythonDefinitionWording(unittest.TestCase):
    def test_docstring_describes_called_python_scripts(self):
        doc = re.sub(r'\s+', ' ', gate_rules.find_refused.__doc__)
        self.assertIn('A python_script definition enables scripts that run when called.', doc)

    def test_existing_baseline_reason_still_matches_unchanged_definition(self):
        reason = 'a python_script definition, which runs on its own once loaded'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'config'
            root.mkdir()
            path = root / 'sample.yaml'
            path.write_text('python_script:\n', encoding='utf-8')
            findings = gate_rules.find_refused(path.read_text(), gate_rules.STRICT_RULES)
            self.assertEqual([item.reason for item in findings], [reason])
            watch = remote_control.ConfigWatch(root, Path(temp) / 'baseline.json', rules=lambda: gate_rules.STRICT_RULES)
            watch.take_baseline()
            self.assertEqual(watch.new_findings(remote_on=True), [])
            self.assertEqual(path.read_text(), 'python_script:\n')
            path.write_text('python_script:\npython_script:\n', encoding='utf-8')
            self.assertEqual(len(watch.new_findings()), 1)

    def test_documented_path_check_applies_only_when_remote_is_on(self):
        for path in ('python_scripts/example.py', 'pyscript/example.py'):
            payload = {'tool_input': {'command': 'cat ' + path}}
            with self.subTest(path=path):
                self.assertTrue(gate_rules.check_tool_call('PreToolUse', payload, True, gate_rules.STRICT_RULES))
                self.assertEqual(gate_rules.check_tool_call('PreToolUse', payload, False, gate_rules.STRICT_RULES), '')

    def test_docs_distinguish_yaml_scan_and_path_check(self):
        text = (ROOT / 'DOCS.md').read_text(encoding='utf-8')
        section = re.sub(r'\s+', ' ', text.split('**Python definitions.**', 1)[1].split('\n  - ', 1)[0]).lower()
        self.assertIn('yaml scan does not read `.py` files', section)
        self.assertIn('remote control', section)
        self.assertIn('`python_scripts/`', section)
        self.assertIn('`pyscript/`', section)
        self.assertIn('refused', section)


if __name__ == '__main__':
    unittest.main()
