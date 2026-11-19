"""A read-only turn never saves externally changed dashboard files through the worker."""
import json
import unittest
from unittest.mock import patch
import test_verification as fixtures
from test_server import server


class DashboardTurnPermissions(unittest.TestCase):
    def setUp(self):
        fixtures.VerificationTests.setUp(self)
        for name in ('core', 'ws_read'):
            guard = patch.object(self.engine, name, side_effect=AssertionError('Unexpected network in synthetic fixture'))
            guard.start(); self.addCleanup(guard.stop)
        readback = patch.object(self.engine, 'dashboard_readback', return_value={'status': 'passed'})
        readback.start(); self.addCleanup(readback.stop)

    def external_edit(self, mode='read-only'):
        self.options.update(codex_sandbox='workspace-write', auto_save_lovelace=True, config_check=False)
        server.tasks['chat']['permission_mode'] = mode
        path = self.config / '.storage/lovelace'
        path.parent.mkdir()
        path.write_text(json.dumps({'data': {'config': {'views': []}}}))
        run = server.get_run_dir('chat'); run.mkdir(parents=True)
        before = server.record_baseline('chat')
        self.assertIn('.storage/lovelace', before)
        # Simulate another actor changing the file while this chat's turn runs.
        path.write_text(json.dumps({'data': {'config': {'views': [{'title': 'External change'}]}}}))
        changes = server.diff_manifests(before, server.build_manifest())
        self.assertIn('.storage/lovelace', changes['changed'])
        return run, before, changes

    def test_read_only_capability_scoped_verification_does_not_save_external_change(self):
        self.external_edit()
        with patch.object(server, 'save_lovelace_dashboard', return_value=(True, 'synthetic accepted')) as save, \
             patch.object(self.engine, 'dashboard_readback', return_value={'status': 'passed'}), \
             patch.object(self.engine, 'core', side_effect=ValueError('Synthetic browser unavailable')) as core:
            result = self.engine.dispatch({'capability': self.capability, 'operation': 'dashboard',
                                           'path': '/lovelace/0', 'save_pending': True})
        save.assert_not_called()
        core.assert_not_called()
        self.assertIn('read-only', result['message'])

    def test_read_only_end_of_turn_assessment_does_not_save_external_change(self):
        run, before, changes = self.external_edit()
        with patch.object(server, 'save_lovelace_dashboard', return_value=(True, 'synthetic accepted')) as save:
            result = server.assess_changes('chat', run, changes, before)
        save.assert_not_called()
        self.assertEqual(result['lovelace_results'], [])

    def test_default_turn_keeps_both_authorized_dashboard_save_paths(self):
        run, before, changes = self.external_edit('default')
        with patch.object(server, 'save_lovelace_dashboard', return_value=(True, 'synthetic accepted')) as save, \
             patch.object(self.engine, 'dashboard_readback', return_value={'status': 'passed'}):
            self.engine.save_pending_dashboard('chat', '/lovelace/0')
            result = server.assess_changes('chat', run, changes, before)
        self.assertEqual(save.call_count, 2)
        self.assertTrue(result['lovelace_results'][0]['success'])

    def test_global_read_only_still_narrows_a_default_chat_on_both_paths(self):
        run, before, changes = self.external_edit('default')
        self.options['codex_sandbox'] = 'read-only'
        with patch.object(server, 'save_lovelace_dashboard', return_value=(True, 'synthetic accepted')) as save:
            with self.assertRaisesRegex(ValueError, 'read-only'):
                self.engine.save_pending_dashboard('chat', '/lovelace/0')
            self.assertEqual(server.assess_changes('chat', run, changes, before)['lovelace_results'], [])
        save.assert_not_called()

    def test_background_read_only_mode_also_prevents_both_dashboard_saves(self):
        run, before, changes = self.external_edit('default')
        server.tasks['chat'].update(task_source='schedule', background_mode='read-only')
        with patch.object(server, 'save_lovelace_dashboard', return_value=(True, 'synthetic accepted')) as save:
            with self.assertRaisesRegex(ValueError, 'read-only'):
                self.engine.save_pending_dashboard('chat', '/lovelace/0')
            self.assertEqual(server.assess_changes('chat', run, changes, before)['lovelace_results'], [])
        save.assert_not_called()

    def test_verification_rechecks_mode_after_validation_before_the_write(self):
        self.external_edit('default')
        def validate(_changes):
            self.options['codex_sandbox'] = 'read-only'
            return []
        with patch.object(server, 'validate_changed_files', side_effect=validate), \
             patch.object(server, 'save_lovelace_dashboard', return_value=(True, 'synthetic accepted')) as save, \
             patch.object(self.engine, 'dashboard_readback', return_value={'status': 'passed'}):
            with self.assertRaisesRegex(ValueError, 'read-only'):
                self.engine.save_pending_dashboard('chat', '/lovelace/0')
        save.assert_not_called()

    def test_assessment_rechecks_mode_after_discovery_before_the_write(self):
        run, before, changes = self.external_edit('default')
        original = server.find_lovelace_dashboard_refs
        def discover(value):
            refs = original(value)
            self.options['codex_sandbox'] = 'read-only'
            return refs
        with patch.object(server, 'find_lovelace_dashboard_refs', side_effect=discover), \
             patch.object(server, 'save_lovelace_dashboard', return_value=(True, 'synthetic accepted')) as save:
            self.assertEqual(server.assess_changes('chat', run, changes, before)['lovelace_results'], [])
        save.assert_not_called()
