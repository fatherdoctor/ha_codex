"""Regression checks for Python-definition wording and its documented scope."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PythonDefinitionWording(unittest.TestCase):
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
