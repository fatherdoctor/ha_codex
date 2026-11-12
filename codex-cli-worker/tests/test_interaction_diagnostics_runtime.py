"""Exercise actual worker collectors and exports with synthetic interactions only."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack
from test_server import server
from approvals import COMMAND_APPROVAL
from interaction_diagnostics import InteractionRecorder, RollingDiagnostics
from relay_reads import RelayReads, ReadError

class RuntimeDiagnosticsTests(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = RollingDiagnostics(Path(temp.name) / 'interactions.json')
        self.recorder = InteractionRecorder(self.store, '0.1.81.2', '0.166.0')
        p = patch.object(server, 'diagnostics', self.recorder)
        p.start()
        self.addCleanup(p.stop)

    def test_background_sources_finish_and_exceptions_are_recorded_without_prompt(self):
        for source, expected in (('page', 'addon_page'), ('schedule', 'scheduled_run'), ('lead', 'lead_channel')):
            task = {'task_source': source, 'status': 'running', 'chat_settings': {'model': 'gpt-6.1-sol'}}

            def run(*args):
                # Synthetic privacy canary representing command text.
                server.record_activity_event('sample', {
                    'type': 'item.started',
                    # Synthetic privacy canary; the field/identifier names the private data it represents.
                    'item': {'id': 'one', 'type': 'command_execution', 'command': 'PRIVATE_COMMAND_CANARY'},
                })
                # Synthetic privacy canary representing tool output, command text.
                server.record_activity_event('sample', {
                    'type': 'item.completed',
                    'item': {
                        'id': 'one',
                        'type': 'command_execution',
                        'status': 'completed',
                        'exit_code': 0,
                        # Synthetic privacy canary; the field/identifier names the private data it represents.
                        'aggregated_output': 'PRIVATE_OUTPUT_CANARY',
                    },
                })
                task['status'] = 'completed'
            with ExitStack() as stack:
                for name, value in {'tasks': {'sample': task}, 'running_processes': {}, 'approval_sessions': {}}.items():
                    stack.enter_context(patch.object(server, name, value))
                for name in ('finish_phase', 'append_activity_step', 'note_edited_files', 'finish_activity', 'remove_run_artifacts', 'start_next_queued', 'cleanup_backups'):
                    stack.enter_context(patch.object(server, name))
                stack.enter_context(patch.object(server.verification, 'end'))
                stack.enter_context(patch.object(server.ha_data, 'end'))
                stack.enter_context(patch.object(server, 'get_run_dir', return_value=Path('/synthetic')))
                stack.enter_context(patch.object(server, 'run_task', side_effect=run))
                # Synthetic privacy canary representing prompt.
                server._run_background_task('sample', 'PRIVATE_PROMPT_CANARY', None, None)
            row = self.store.export()['interactions'][-1]
            self.assertEqual(row['source'], expected)
            self.assertEqual(row['outcome'], 'done')
            self.assertEqual(row['events'][0]['name'], 'commandExecution')
            self.assertIsNotNone(row['end'])
        # Synthetic privacy canary representing path.
        self.assertNotIn('CANARY', self.store.path.read_text())
        # Synthetic privacy canary representing untrusted free text.
        self.assertNotIn('CANARY', json.dumps(self.store.export()))

    def test_real_session_records_request_and_human_decision_without_changing_it(self):
        task = {
            'status': 'running',
            'current_turn_id': 'turn',
            'turns': [{'turn_id': 'turn', 'execution_settings': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium'}}],
        }
        proc = Mock(stdin=io.StringIO())
        proc.poll.return_value = None
        self.recorder.begin('sample', 'addon_page')

        def update(_task, **fields):
            task.update(fields)
            return True
        with (
            patch.object(server, 'tasks', {'sample': task}),
            patch.object(server, 'running_processes', {'sample': proc}),
            # Synthetic privacy canary representing credential.
            patch.object(server, 'api_token', return_value='SYNTHETIC_TOKEN_CANARY'),
            patch.object(server, 'update_task', side_effect=update),
            patch.object(server, 'notify_permission_async'),
            patch.object(server, 'prompt_attachment_paths', return_value=[]),
        ):
            # Synthetic privacy canary representing path, prompt, approval identifier.
            session = server.create_approval_session(
                'sample',
                proc,
                # Synthetic privacy canary; the field/identifier names the private data it represents.
                'PRIVATE_PROMPT_CANARY',
                Path('/synthetic/final'),
                None,
                {},
                dict(server.DEFAULT_OPTIONS),
            )
            session.thread_id = 'synthetic-thread'
            session.turn_id = 'synthetic-turn'
            # Synthetic privacy canary representing command text, approval identifier, approval reason.
            session.receive({
                'id': 4,
                'method': COMMAND_APPROVAL,
                'params': {
                    'threadId': 'synthetic-thread',
                    'turnId': 'synthetic-turn',
                    'command': 'echo sample',
                    'cwd': '/config',
                    # Synthetic privacy canary; the field/identifier names the private data it represents.
                    'reason': 'PRIVATE_REASON_CANARY',
                },
            })
            approval = task['approvals'][0]['approval_id']
            # Synthetic privacy canary representing person name, approval identifier.
            session.decide(approval, 'allow', {'id': 'PRIVATE_PERSON_CANARY'})
            self.assertIn('accept', proc.stdin.getvalue())
        self.recorder.finish('sample', 'done')
        row = self.store.export()['interactions'][0]
        self.assertEqual([x['decision'] for x in row['events'] if x['kind'] == 'approval'], ['asked', 'allowed'])
        # Synthetic privacy canary representing untrusted free text.
        self.assertNotIn('CANARY', json.dumps(row))
        self.assertNotIn('echo sample', json.dumps(row))

    def test_relay_read_observer_preserves_results_and_refusals(self):
        proc = Mock()
        proc.poll.return_value = None
        # Synthetic privacy canary representing credential, entity identifier.
        engine = RelayReads(
            lambda: (proc, False),
            # Synthetic privacy canary; the field/identifier names the private data it represents.
            lambda: 'SYNTHETIC_TOKEN_CANARY',
            lambda x: x,
            # Synthetic privacy canary; the field/identifier names the private data it represents.
            fetch=lambda *_: {'entity_id': 'light.synthetic', 'state': 'PRIVATE_STATE_CANARY', 'attributes': {}},
            observer=server.record_relay_read,
        )
        engine.environment({})
        result = engine.dispatch({
            'capability': engine.capability,
            'arguments': {'operation': 'state', 'entity_id': 'light.synthetic'},
        })
        # Synthetic privacy canary representing untrusted free text.
        self.assertEqual(result['state'], 'PRIVATE_STATE_CANARY')
        with self.assertRaises(ReadError):
            # Synthetic privacy canary representing credential, entity identifier.
            engine.dispatch({
                'capability': engine.capability,
                # Synthetic privacy canary; the field/identifier names the private data it represents.
                'arguments': {'operation': 'state', 'entity_id': 'sensor.private_token_canary'},
            })
        engine.end_observation()
        rows = self.store.export()['interactions']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'relay_read')
        self.assertEqual(rows[0]['outcome'], 'done')
        self.assertEqual(rows[0]['events'][0]['domain'], 'light')
        self.assertTrue(any((e['kind'] == 'error' for e in rows[0]['events'])))
        self.assertIn('credential', [e.get('rule') for e in rows[0]['events']])
        # Synthetic privacy canary representing untrusted free text.
        self.assertNotIn('CANARY', json.dumps(rows))
        self.assertNotIn('light.synthetic', json.dumps(rows))
        # Synthetic privacy canary representing exception text.
        engine.observer = Mock(side_effect=OSError('PRIVATE_CANARY'))
        # Synthetic privacy canary representing entity identifier.
        self.assertEqual(engine.dispatch({
            'capability': engine.capability,
            'arguments': {'operation': 'state', 'entity_id': 'light.synthetic'},
        # Synthetic privacy canary; the field/identifier names the private data it represents.
        })['state'], 'PRIVATE_STATE_CANARY')

    def test_export_route_requires_auth_and_discloses_coverage_without_caching(self):
        self.recorder.begin('sample', 'addon_page')
        self.recorder.finish('sample', 'done')
        with (
            patch.object(server, 'interaction_logs', self.store),
            patch.object(server, 'api_token', return_value='fixture-only'),
            patch.object(server, 'is_ingress_request', return_value=False),
        ):
            client = server.app.test_client()
            self.assertEqual(client.get('/diagnostics/export').status_code, 401)
            response = client.get('/diagnostics/export', headers={'Authorization': 'Bearer fixture-only'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('attachment', response.headers['Content-Disposition'])
            self.assertEqual(response.json['coverage'], 'remote ChatGPT threads: not recorded')
            self.assertEqual(len(response.json['interactions']), 1)

    def test_build_packages_collector_and_scrubber(self):
        text = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        self.assertIn('COPY interaction_diagnostics.py diagnostics_scrub.py /', text)
        self.assertIn('ENV ADDON_VERSION=${BUILD_VERSION} CODEX_VERSION=${CODEX_VERSION}', text)

    def test_fixed_refusal_from_stderr_is_recorded_but_arbitrary_prose_is_dropped(self):
        self.recorder.begin('sample', 'addon_page')
        message = 'This request enables full access, which is never allowed.'
        with patch.object(server, 'write_task_log'):
            # Synthetic privacy canary representing secret, person name.
            server.reader_thread(
                'sample',
                'stderr',
                # Synthetic privacy canary; the field/identifier names the private data it represents.
                io.StringIO('PRIVATE_PERSON_CANARY\n' + message + '\nPRIVATE_SECRET_CANARY\n'),
                {},
            )
        self.recorder.finish('sample', 'failed')
        row = self.store.export()['interactions'][0]
        self.assertEqual([x['rule'] for x in row['events'] if x['kind'] == 'gate'], ['full_access'])
        # Synthetic privacy canary representing untrusted free text.
        self.assertNotIn('CANARY', json.dumps(row))

    def test_history_read_metadata_follows_authorization_and_cannot_break_the_read(self):
        from test_ha_data import data_worker, START, END
        from ha_data import HomeAssistantData, MAX_QUERIES
        worker = data_worker()
        worker.diagnostics = self.recorder
        engine = HomeAssistantData(worker, self.store.path.parent / 'exports')
        capability = engine.begin('chat')
        self.addCleanup(engine.end, 'chat')
        self.recorder.begin('chat', 'addon_page')
        payload = {
            'operation': 'history',
            'capability': capability,
            'entity_ids': ['sensor.synthetic'],
            'start_time': START,
            'end_time': END,
        }
        with (
            patch.object(engine, 'history', return_value={'series': {}}) as history,
            patch.object(engine, 'save_result', return_value={'status': 'observed'}),
        ):
            self.assertEqual(engine.dispatch(payload), {'status': 'observed'})
            history.assert_called_once()
            engine.queries['chat'] = MAX_QUERIES
            self.assertEqual(engine.dispatch(payload)['status'], 'unavailable')
            self.assertEqual(history.call_count, 1)
            engine.queries['chat'] = 0
            worker.diagnostics = Mock()
            # Synthetic privacy canary representing exception text.
            worker.diagnostics.event.side_effect = OSError('PRIVATE_CANARY')
            self.assertEqual(engine.dispatch(payload), {'status': 'observed'})
        self.recorder.finish('chat', 'done')
        reads = [e for e in self.store.export()['interactions'][0]['events'] if e['kind'] == 'ha_read']
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['operation'], 'history')
        self.assertEqual(reads[0]['domain'], 'sensor')
