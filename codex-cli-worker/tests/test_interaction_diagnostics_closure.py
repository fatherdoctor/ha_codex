"""Closure regressions use temporary stores and synthetic values only."""
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import approvals
from interaction_diagnostics import InteractionRecorder, RollingDiagnostics
from test_server import server


class DiagnosticsClosureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'interaction-diagnostics.json'

    def test_thirty_unclean_restarts_leave_room_for_five_completed_tasks(self):
        for index in range(30):
            store = RollingDiagnostics(self.path)
            recorder = InteractionRecorder(store)
            recorder.begin(f'page-{index}', 'addon_page')
            recorder.begin(f'relay-{index}', 'relay_read')
            recorder.flush()

        store = RollingDiagnostics(self.path)
        restarted = store.export()['interactions']
        self.assertTrue(restarted)
        self.assertTrue(all(row['outcome'] == 'unknown (restart)' for row in restarted))
        self.assertTrue(all(row['end'] is not None for row in restarted))

        recorder = InteractionRecorder(store)
        for index in range(5):
            recorder.begin(f'finished-{index}', 'addon_page')
            recorder.finish(f'finished-{index}', 'done')
        rows = store.export()['interactions']
        self.assertEqual(sum(row['outcome'] == 'done' for row in rows), 5)
        self.assertLessEqual(len(rows), store.capacity)
        self.assertFalse(any(row['outcome'] == 'running' for row in rows))

    def test_startup_does_not_reclose_a_task_started_by_this_store(self):
        store = RollingDiagnostics(self.path)
        recorder = InteractionRecorder(store)
        recorder.begin('active', 'addon_page')
        for _ in range(3):
            self.assertEqual(store.export()['interactions'][0]['outcome'], 'running')

    def test_key_creation_uses_no_hard_link_and_keeps_private_mode(self):
        store = RollingDiagnostics(self.path)
        with patch('interaction_diagnostics.os.link', side_effect=PermissionError('links unavailable')):
            store.append({'outcome': 'done', 'entities': ['light.synthetic']})
        self.assertEqual(store.key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(store.key_path.read_bytes()), 32)
        self.assertEqual(len(store.export()['interactions'][0]['entities']), 1)

    def test_bad_key_explains_recovery_and_still_records_without_entity_ids(self):
        for kind in ('mode', 'length', 'symlink'):
            with self.subTest(kind=kind):
                store = RollingDiagnostics(self.root / f'{kind}.json')
                store.key_path.write_bytes(b'x' * (4 if kind == 'length' else 32))
                store.key_path.chmod(0o644 if kind == 'mode' else 0o600)
                if kind == 'symlink':
                    target = self.root / 'synthetic-key-target'
                    store.key_path.replace(target)
                    store.key_path.symlink_to(target)
                with self.assertLogs('interaction_diagnostics', level='WARNING') as logs:
                    recorder = InteractionRecorder(store)
                    recorder.begin('sample', 'addon_page')
                    recorder.event('sample', {'kind': 'tool', 'name': 'read'}, entities=['light.synthetic'])
                    recorder.finish('sample', 'done')
                self.assertIn('delete', ' '.join(logs.output).lower())
                self.assertIn('new key', ' '.join(logs.output).lower())
                row = store.export()['interactions'][-1]
                self.assertEqual(row['outcome'], 'done')
                self.assertEqual(row['entities'], [])
                store.key_path.unlink()
                recorder.begin('recovered', 'addon_page')
                recorder.event('recovered', {'kind': 'tool', 'name': 'read'}, entities=['light.synthetic'])
                recorder.finish('recovered', 'done')
                self.assertEqual(len(store.export()['interactions'][-1]['entities']), 1)

    def test_only_newest_corrupt_store_is_retained(self):
        store = RollingDiagnostics(self.path)
        for _ in range(5):
            self.path.write_text('not JSON')
            store.export()
            self.assertEqual(len(list(self.root.glob(self.path.name + '.corrupt-*'))), 1)

    def test_key_settings_and_set_aside_paths_are_protected(self):
        paths = ('/data/interaction-diagnostics.key', '/data/interaction-diagnostics.settings.json',
                 '/data/interaction-diagnostics.json.corrupt-123456abcdef')
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(approvals.touches_protected_path(path))
                self.assertTrue(approvals.command_names_protected_path(f'cat {path}'))
        self.assertTrue(approvals.command_names_protected_path('cat /data/interaction-diagnostics*'))

    def test_policy_causes_survive_the_export_allowlist(self):
        cases = (
            ('The effective approval policy is "never", which this app refuses.', 'approval_policy_never'),
            ('The protected policy may not set approval_policy to "never".', 'approval_policy_never'),
            ('The protected policy file could not be read: invalid JSON', 'policy_parse'),
            ('Permission policy could not be parsed', 'policy_parse'),
            ('The protected policy file is not a JSON object.', 'policy_parse'),
        )
        store = RollingDiagnostics(self.path)
        recorder = InteractionRecorder(store)
        for index, (problem, expected) in enumerate(cases):
            recorder.begin(str(index), 'addon_page')
            rule = server.launch_policy_diagnostic_rule(problem)
            self.assertEqual(rule, expected)
            recorder.event(str(index), {'kind': 'gate', 'rule': rule})
            recorder.finish(str(index), 'failed')
        self.assertEqual([row['events'][0]['rule'] for row in store.export()['interactions']],
                         [expected for _, expected in cases])

    def test_ingress_administrator_can_export(self):
        store = RollingDiagnostics(self.path)
        with (patch.object(server, 'interaction_logs', store),
              patch.object(server, 'is_ingress_request', return_value=True),
              patch.object(server, 'ingress_admin', return_value=({'id': 'synthetic-admin'}, ''))):
            response = server.app.test_client().get('/diagnostics/export')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
