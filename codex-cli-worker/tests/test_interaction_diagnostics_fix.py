"""Review regressions: synthetic stores and identities, never runtime data."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics_scrub as schema
from interaction_diagnostics import InteractionRecorder, RollingDiagnostics
from test_server import server

class DiagnosticsReviewTests(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = RollingDiagnostics(self.root / 'interactions.json')
        self.recorder = InteractionRecorder(self.store, '0.1.81.2', '0.166.0')

    def test_f1_disk_reference_is_keyed_and_stable_after_restart(self):
        # Synthetic privacy canary representing entity identifier.
        entity = 'lock.private_entity_canary'
        self.store.append({'entities': [entity]})
        reference = json.loads(self.store.path.read_text())[0]['entities'][0]['reference']
        self.assertNotEqual(reference, hashlib.sha256(entity.encode()).hexdigest())
        other = RollingDiagnostics(self.store.path)
        other.append({'entities': [entity]})
        references = [row['entities'][0]['reference'] for row in json.loads(self.store.path.read_text())]
        self.assertEqual(references, [reference, reference])
        key_path = self.store.path.with_suffix('.key')
        self.assertEqual(key_path.stat().st_mode & 511, 384)
        self.assertNotIn(key_path.read_bytes().hex(), json.dumps(other.export()))

    def test_f2_non_admin_ingress_is_refused_but_worker_token_still_exports(self):
        with (
            patch.object(server, 'interaction_logs', self.store),
            patch.object(server, 'is_ingress_request', return_value=True),
            patch.object(server, 'ingress_admin', return_value=(None, 'Administrator required')),
        ):
            response = server.app.test_client().get('/diagnostics/export')
        self.assertEqual(response.status_code, 403)
        with (
            patch.object(server, 'interaction_logs', self.store),
            patch.object(server, 'api_token', return_value='synthetic-token'),
            patch.object(server, 'is_ingress_request', return_value=False),
        ):
            client = server.app.test_client()
            self.assertEqual(client.get('/diagnostics/export').status_code, 401)
            self.assertEqual(
                client.get('/diagnostics/export', headers={'Authorization': 'Bearer synthetic-token'}).status_code,
                200,
            )

    def test_f3_deep_or_wrong_shaped_store_is_set_aside_and_replaced(self):
        for content in ('[' * 100000, '{"rows": "wrong shape"}', 'not JSON'):
            with self.subTest(content_type=content[:1]):
                self.store.path.write_text(content)
                self.recorder.begin('sample', 'addon_page')
                self.recorder.finish('sample', 'done')
                self.assertEqual(self.store.export()['interactions'][-1]['outcome'], 'done')
        self.assertEqual(len(list(self.root.glob('*.corrupt-*'))), 1)

    def test_f3_failed_recorder_cannot_prevent_execution_or_queue_release(self):
        recorder = Mock()
        for name in ('begin', 'activity', 'event', 'finish'):
            getattr(recorder, name).side_effect = RecursionError('synthetic storage failure')
        with ExitStack() as stack:
            stack.enter_context(patch.object(server, 'diagnostics', recorder))
            stack.enter_context(patch.object(server, 'tasks', {'sample': {'status': 'completed'}}))
            stack.enter_context(patch.object(server, 'approval_sessions', {}))
            stack.enter_context(patch.object(server, 'running_processes', {}))
            run = stack.enter_context(patch.object(server, 'run_task'))
            release = stack.enter_context(patch.object(server, 'start_next_queued'))
            for name in ('finish_activity', 'remove_run_artifacts', 'cleanup_backups'):
                stack.enter_context(patch.object(server, name))
            stack.enter_context(patch.object(server, 'get_run_dir', return_value=self.root))
            stack.enter_context(patch.object(server.verification, 'end'))
            stack.enter_context(patch.object(server.ha_data, 'end'))
            server._run_background_task('sample', 'synthetic prompt', None, None)
            run.assert_called_once()
            release.assert_called_once_with(release='sample')

    def test_f4_relay_reads_group_by_capability_and_keep_running_tasks(self):
        with patch.object(server, 'diagnostics', self.recorder):
            self.recorder.begin('long-task', 'addon_page')
            for index in range(30):
                server.record_relay_read(
                    {'operation': 'state', 'domain': 'light', 'session': 'synthetic-session'},
                    {'ok': True},
                    None,
                    self.recorder.clock(),
                )
            self.recorder.flush()
        rows = self.store.export()['interactions']
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum((row['source'] == 'addon_page' for row in rows)), 1)
        relay = next((row for row in rows if row['source'] == 'relay_read'))
        self.assertEqual(relay['read_counts'], [{'operation': 'state', 'domain': 'light', 'count': 30}])

    def test_f4_active_task_survives_more_completed_tasks_than_capacity(self):
        self.recorder.begin('long', 'addon_page')
        for index in range(110):
            self.recorder.begin(str(index), 'lead_channel')
            self.recorder.finish(str(index), 'done')
        self.recorder.flush()
        rows = self.store.export()['interactions']
        self.assertTrue(any((row['source'] == 'addon_page' and row['outcome'] == 'running' for row in rows)))

    def test_f7_non_dict_rows_are_dropped_with_a_count(self):
        self.store.path.write_text(json.dumps([1, 'synthetic junk', None, {'outcome': 'done'}]))
        export = self.store.export()
        self.assertEqual(len(export['interactions']), 1)
        self.assertEqual(export['discarded_rows'], 3)

    def test_f8_event_writes_are_coalesced_and_export_flushes(self):
        with patch.object(self.store, 'append', wraps=self.store.append) as append:
            self.recorder.begin('sample', 'addon_page')
            for _ in range(12):
                self.recorder.event('sample', {'kind': 'tool', 'name': 'read', 'result': 'done'})
            self.assertLess(append.call_count, 6)
            self.recorder.flush()
            self.assertEqual(len(self.store.export()['interactions'][0]['events']), 12)

    def test_f9_versions_follow_syntax_and_the_actual_cli_pin(self):
        self.assertEqual(schema.scrub({'addon_version': '1.2.3'})['addon_version'], '1.2.3')
        dockerfile = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        import re
        pin = re.search('^ARG CODEX_VERSION=([0-9.]+)$', dockerfile, re.MULTILINE).group(1)
        self.assertEqual(schema.scrub({'cli_version': pin})['cli_version'], pin)

    def test_retention_default_and_validated_settings_range(self):
        self.assertEqual(self.store.capacity, 25)
        self.store.configure(10)
        self.assertEqual(self.store.capacity, 10)
        self.store.configure(100)
        self.assertEqual(self.store.max_bytes, 128 * 1024 * 10)
        for value in (9, 101, True, '25'):
            with (
                self.subTest(value=value),
                self.assertRaises(ValueError),
            ):
                self.store.configure(value)

    def test_f5_launch_policy_causes_are_not_all_called_full_access(self):
        self.assertEqual(
            server.launch_policy_diagnostic_rule('The effective approval policy is "never", which this app refuses.'),
            'approval_policy_never',
        )
        self.assertEqual(server.launch_policy_diagnostic_rule('Permission policy could not be parsed'), 'policy_parse')
        self.assertEqual(
            server.launch_policy_diagnostic_rule('The sandbox mode "danger-full-access" is not allowed in this app.'),
            'full_access',
        )

    def test_settings_require_an_admin_validate_bounds_and_survive_restart(self):
        with (
            patch.object(server, 'interaction_logs', self.store),
            patch.object(server, 'is_ingress_request', return_value=True),
            patch.object(server, 'ingress_admin', return_value=(None, 'Administrator required')),
        ):
            client = server.app.test_client()
            self.assertEqual(client.post('/diagnostics/settings', json={'capacity': 50}).status_code, 403)
        with (
            patch.object(server, 'interaction_logs', self.store),
            patch.object(server, 'is_ingress_request', return_value=True),
            patch.object(server, 'ingress_admin', return_value=({'id': 'synthetic-admin'}, '')),
        ):
            client = server.app.test_client()
            self.assertEqual(client.post('/diagnostics/settings', json={'capacity': 50}).status_code, 200)
            self.assertEqual(client.get('/diagnostics/settings').json['capacity'], 50)
            self.assertEqual(client.post('/diagnostics/settings', json={'capacity': 101}).status_code, 400)
        self.assertEqual(RollingDiagnostics(self.store.path).capacity, 50)

    def test_legacy_bare_hashes_are_removed_from_disk_during_migration(self):
        # Synthetic privacy canary representing entity identifier.
        reference = hashlib.sha256(b'light.private_entity_canary').hexdigest()
        self.store.path.write_text(json.dumps([{'outcome': 'done', 'entities': [{'domain': 'light', 'reference': reference}]}]))
        self.assertEqual(self.store.export()['interactions'][0]['entities'], [])
        self.assertNotIn(reference, self.store.path.read_text())
