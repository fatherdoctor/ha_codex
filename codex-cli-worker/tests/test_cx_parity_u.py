"""Review regressions: documented remote approvals and the generic backup rule."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class _RemoteNote(HTMLParser):
    def __init__(self):
        super().__init__()
        self.recording = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'p' and dict(attrs).get('id') == 'remote-control-note':
            self.recording = True

    def handle_data(self, value):
        if self.recording:
            self.parts.append(value)

    def handle_endtag(self, tag):
        if tag == 'p':
            self.recording = False

    def text(self):
        return ' '.join(' '.join(self.parts).lower().split())


class ParityReviewDocsTests(unittest.TestCase):
    def assert_normal_approval_contract(self, text):
        self.assertIn('approval', text, 'Normal remote actions must be described as native approvals.')
        self.assertNotIn('commands that need more than the sandbox are refused', text)
        self.assertNotIn('sandbox escalation while it is on', text)
        self.assertIn('full-access', text, 'The retained full-access restriction must remain explicit.')
        self.assertIn('credential', text)
        self.assertIn('/config/agents.md', text)

    def test_f1_page_note_describes_normal_remote_approval_and_retained_refusals(self):
        parser = _RemoteNote()
        parser.feed((ROOT / 'web/index.html').read_text())
        self.assert_normal_approval_contract(parser.text())

    def test_f1_command_checks_bullet_describes_normal_remote_approval_and_retained_refusals(self):
        docs = (ROOT / 'DOCS.md').read_text()
        bullet = next(line for line in docs.splitlines() if line.startswith('- **Command checks.**'))
        self.assert_normal_approval_contract(' '.join(bullet.lower().split()))

    def test_no_house_practice_is_bundled(self):
        self.assertFalse((ROOT / 'policy/house-rules.md').exists())
        self.assertIn('/config/AGENTS.md', (ROOT / 'server.py').read_text())


if __name__ == '__main__':
    unittest.main()
