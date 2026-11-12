"""Bundled instructions are removed without touching owner instructions or policy."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_server import server
import policy
ROOT = Path(__file__).resolve().parents[1]

class NoHouseRules(unittest.TestCase):
    def test_image_does_not_bundle_house_instructions(self):
        self.assertFalse((ROOT / 'policy/house-rules.md').exists())
        self.assertNotIn('COPY policy/house-rules.md', (ROOT / 'Dockerfile').read_text())

    def test_startup_preserves_owner_home_instructions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'home'; home.mkdir()
            instructions = home / 'AGENTS.md'; instructions.write_text('Owner instructions, unchanged.')
            with patch.multiple(server, CODEX_HOME=home, CODEX_CONFIG_PATH=home/'config.toml', DATA_ROOT=root,
                                SCHEMA_PATH=root/'schema.json', AUTH_QR_DIR=root/'qr'), \
                 patch.object(server, 'task_root', return_value=root/'tasks'), \
                 patch.object(server, 'api_token', return_value='synthetic'):
                server.ensure_runtime_files()
            self.assertEqual(instructions.read_text(), 'Owner instructions, unchanged.')

    def test_migration_removes_only_exact_legacy_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder); target = home/'AGENTS.md'; old = b'synthetic old bundled copy'
            with patch.object(policy, 'LEGACY_INSTRUCTIONS_SHA256', frozenset({hashlib.sha256(old).hexdigest()})):
                target.write_bytes(old); target.chmod(0o444)
                self.assertTrue(policy.remove_legacy_instructions(home))
                self.assertFalse(target.exists())
                self.assertFalse(policy.remove_legacy_instructions(home))
                target.write_text('Edited by owner')
                self.assertFalse(policy.remove_legacy_instructions(home))
                self.assertEqual(target.read_text(), 'Edited by owner')

    def test_migration_never_follows_a_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); target = root/'owner.md'; target.write_text('synthetic owner text')
            home = root/'home'; home.mkdir(); (home/'AGENTS.md').symlink_to(target)
            self.assertFalse(policy.remove_legacy_instructions(home))
            self.assertEqual(target.read_text(), 'synthetic owner text')

    def test_config_agents_read_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'AGENTS.md'; target.write_text('Owner workspace instructions')
            with patch.object(server, 'AGENTS_PATH', target):
                self.assertEqual(server.read_agents_file(), 'Owner workspace instructions')

    def test_task_context_states_workspace_facts_without_house_style(self):
        with patch.object(server, 'attached_image_note', return_value=''), \
             patch.object(server, 'backup_instructions', return_value=''), \
             patch.object(server, 'home_assistant_api_instructions', return_value=''), \
             patch.object(server, 'ha_docs_note', return_value=''), \
             patch.object(server, 'read_options', return_value={}):
            text = server.build_prompt('Inspect the configuration', 'synthetic')
        self.assertIn('Owner workspace instructions: /config/AGENTS.md.', text)
        self.assertNotIn('Make focused edits', text)
        self.assertNotIn('prefer a change you are confident', text)
