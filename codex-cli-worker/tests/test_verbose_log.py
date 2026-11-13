"""Bounded verbose storage and actual worker surfaces, using synthetic data only."""
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_server import server
from approvals import AppServerSession, touches_protected_path, command_names_protected_path
from verbose_log import VerboseLog, Tee, Handler, MAX_BYTES, TRUNCATED
import verbose_log


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'interaction-diagnostics.verbose.log'
        # Planted values stand in for the add-on's own credentials, never owner data.
        self.secrets = ['SYNTHETIC_SUPERVISOR', 'SYNTHETIC_HA_TOKEN', 'SYNTHETIC_RELAY',
                        'SYNTHETIC_WORKER', 'SYNTHETIC_ACCESS', 'SYNTHETIC_REFRESH', 'SYNTHETIC_ID']
        self.store = VerboseLog(self.path, lambda: self.secrets)

    def enable(self, lines=500):
        self.store.configure({'enabled': True, 'lines': lines})

    def test_off_by_default_and_only_explicit_owner_toggle_turns_it_off(self):
        self.store.capture('stdout', 'not retained')
        self.assertFalse(self.path.exists())
        self.assertFalse(self.store.status()['enabled'])
        self.enable()
        self.store.capture('stdout', 'retained')
        self.assertIn(b'retained', self.store.export())
        restored = VerboseLog(self.path, lambda: self.secrets)
        self.assertTrue(restored.status()['enabled'])
        restored.configure({'enabled': False, 'lines': 500})
        restored.capture('stdout', 'not retained again')
        self.assertNotIn(b'not retained again', restored.export())
        self.assertFalse(VerboseLog(self.path).status()['enabled'])

    def test_masks_each_own_secret_and_preserves_arbitrary_private_prose(self):
        self.enable()
        prose = 'UNFILTERED_PROMPT PRIVATE_CONTACT_CANARY /arbitrary/path user_password=not_an_addon_secret'
        self.store.capture('rpc', prose + ' ' + ' '.join(self.secrets))
        at_rest = self.path.read_bytes()
        for secret in self.secrets:
            self.assertNotIn(secret.encode(), at_rest)
        result = self.store.export().decode()
        self.assertIn(prose, result)
        for secret in self.secrets:
            self.assertNotIn(secret, result)
            self.assertNotIn(secret.encode(), self.path.read_bytes())

    def test_only_exact_values_are_masked_even_short_or_json_escaped(self):
        self.secrets = ['short', 'quote"slash\\secret', 'SYNTHETIC_🙂_SECRET', '  NORMALIZED_SECRET\n']
        self.enable()
        self.store.capture('rpc', json.dumps({'token': self.secrets[1], 'value': 'short', 'user_token': 'leave_this_alone', 'unicode': self.secrets[2], 'spaced': 'NORMALIZED_SECRET'}))
        result = self.store.export().decode()
        self.assertNotIn('short', result)
        self.assertNotIn('slash', result)
        self.assertIn('leave_this_alone', result)
        self.assertNotIn('SYNTHETIC_', result)
        self.assertNotIn('\\ud83d', result)
        self.assertNotIn('NORMALIZED_SECRET', result)

    def test_line_fifo_long_burst_and_restart(self):
        self.enable(lines=100)
        self.store.capture('burst', '\n'.join(f'record-{i:04}' for i in range(1200)))
        result = self.store.export().decode().splitlines()
        self.assertEqual(len(result), 100)
        self.assertTrue(result[0].endswith('record-1100'))
        self.assertTrue(result[-1].endswith('record-1199'))
        restored = VerboseLog(self.path, lambda: self.secrets)
        restored.capture('after restart', 'new record')
        result = restored.export().decode().splitlines()
        self.assertLessEqual(len(result), 100)
        self.assertGreaterEqual(len(result), 50)
        self.assertTrue(result[-1].endswith('new record'))

    def test_inflight_capture_cannot_cross_clear_or_disable_cycle(self):
        self.enable()
        for operation in ('clear', 'disable_cycle'):
            with self.subTest(operation=operation):
                entered, release = threading.Event(), threading.Event()
                def credentials():
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('Synthetic provider timed out')
                    return self.secrets
                self.store.secrets = credentials
                thread = threading.Thread(target=self.store.capture, args=('stdout', 'OLD_PRIVATE_DATA'))
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    if operation == 'clear':
                        self.store.clear()
                    else:
                        self.store.configure({'enabled': False, 'lines': 500})
                        self.enable()
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.store.secrets = lambda: self.secrets
                self.assertNotIn(b'OLD_PRIVATE_DATA', self.store.export())
                self.assertTrue(self.store.status()['enabled'])
                self.store.clear()

    def test_capture_after_clear_or_disable_cycle_is_retained(self):
        self.enable()
        for operation in ('clear', 'disable_cycle'):
            with self.subTest(operation=operation):
                self.store.capture('stdout', 'old record')
                if operation == 'clear':
                    self.store.clear()
                else:
                    self.store.configure({'enabled': False, 'lines': 500})
                    self.enable()
                self.store.capture('stdout', 'new record')
                self.assertIn(b'new record', self.store.export())
                self.store.clear()

    def test_restart_preserves_unicode_separators_inside_physical_records(self):
        self.enable(lines=100)
        for separator in ('\u2028', '\u2029', '\x85', '\v', '\f'):
            with self.subTest(separator=repr(separator)):
                self.store.clear()
                text = separator.join(f'segment-{i:03}' for i in range(150))
                self.store.capture('stdout', text)
                before = self.store.export()
                self.assertEqual(before.count(b'\n'), 1)
                restored = VerboseLog(self.path, lambda: self.secrets)
                self.assertEqual(restored.status()['stored_lines'], 1)
                self.assertEqual(restored.export(), before)
                self.assertIn(b'segment-000', before)
                self.assertIn(b'segment-149', before)
                self.store = restored

    def test_byte_cap_never_exceeded_by_destination_or_atomic_write(self):
        self.store = VerboseLog(self.path, lambda: (), max_bytes=4096)
        self.enable(lines=2000)
        writes = []
        original = verbose_log.atomic
        def checked(path, raw):
            writes.append(len(raw))
            original(path, raw)
        with patch.object(verbose_log, 'atomic', side_effect=checked):
            for i in range(250):
                self.store.capture('burst', f'{i:03} ' + 'x' * 400)
                self.assertLessEqual(self.path.stat().st_size, 4096)
        restored = VerboseLog(self.path, lambda: (), max_bytes=4096)
        restored.capture('restart', 'last record')
        self.assertLessEqual(self.path.stat().st_size, 4096)
        self.assertTrue(restored.export().endswith(b'last record\n'))
        self.assertTrue(writes)
        self.assertLessEqual(max(writes), 4096)

    def test_lower_line_limit_is_not_published_before_the_file_is_trimmed(self):
        self.enable()
        self.store.capture('burst', '\n'.join(str(i) for i in range(300)))
        original = verbose_log.atomic
        def check_order(path, raw):
            if path == self.store.settings_path:
                self.assertLessEqual(len(self.path.read_bytes().splitlines()), 100)
            original(path, raw)
        with patch.object(verbose_log, 'atomic', side_effect=check_order):
            self.store.configure({'enabled': True, 'lines': 100})

    def test_overlong_unicode_line_has_marker_and_valid_utf8(self):
        self.store = VerboseLog(self.path, lambda: (), max_bytes=4096)
        self.enable()
        self.store.capture('stdout', '🙂' * 10000)
        result = self.store.export()
        self.assertLessEqual(len(result), 4096)
        self.assertTrue(result.decode().endswith(TRUNCATED))

    def test_truncation_does_not_cut_a_secret_into_an_exported_prefix(self):
        self.store = VerboseLog(self.path, lambda: ['START_OF_PRIVATE_SECRET_END'], max_bytes=512)
        self.enable()
        stream = Tee(io.StringIO(), self.store, 'stdout')
        stream.write('x' * 420 + 'START_OF_PRIVATE_SECRET_END' + 'z' * (2 * 1024 * 1024) + '\n')
        result = self.store.export().decode()
        self.assertNotIn('START_OF_PRIVATE', result)
        self.assertIn('[redacted]', result)
        self.assertLessEqual(len(result.encode()), 512)

    def test_partial_stream_writes_do_not_leak_split_secrets(self):
        self.enable()
        stream = Tee(io.StringIO(), self.store, 'stdout')
        stream.write('SYNTHETIC_')
        stream.flush()
        self.assertFalse(self.path.exists())
        stream.write('SUPERVISOR\n')
        self.assertNotIn(b'SYNTHETIC_', self.store.export())
        stream.write('last partial line')
        stream.finish()
        self.assertTrue(self.store.export().endswith(b'last partial line\n'))

    def test_disabled_or_cleared_partial_lines_never_reappear(self):
        stream = Tee(io.StringIO(), self.store, 'stdout')
        stream.write('OFF_PRIVATE_CANARY')
        self.enable()
        stream.write('enabled line\n')
        self.assertNotIn(b'OFF_PRIVATE_CANARY', self.store.export())
        stream.write('CLEAR_PRIVATE_CANARY')
        self.store.clear()
        stream.write('after clear\n')
        self.assertNotIn(b'CLEAR_PRIVATE_CANARY', self.store.export())

    def test_crash_during_replacement_keeps_previous_readable_file(self):
        self.enable(lines=100)
        self.store.capture('stdout', '\n'.join(['earlier line'] * 99 + ['before interruption']))
        previous = self.path.read_bytes()
        with patch.object(verbose_log.os, 'replace', side_effect=OSError('synthetic interruption')):
            self.store.capture('stdout', 'interrupted write')
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertTrue(self.store.status()['enabled'])
        restored = VerboseLog(self.path, lambda: ())
        self.assertIn(b'before interruption', restored.export())
        self.assertNotIn(b'interrupted write', restored.export())
        self.assertEqual(list(self.root.glob('.verbose-*.tmp')), [])

    def test_storage_mode_limits_validation_clear_and_symlink_refusal(self):
        self.enable()
        self.store.capture('stdout', 'record')
        for path in (self.path, self.store.settings_path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for value in ({'enabled': True, 'lines': 99}, {'enabled': True, 'lines': 2001},
                      {'enabled': 1, 'lines': 500}, {'enabled': False, 'lines': True}):
            with self.assertRaises(ValueError):
                self.store.configure(value)
        self.store.clear()
        self.assertFalse(self.path.exists())
        self.assertTrue(self.store.status()['enabled'])
        target = self.root / 'target'
        target.write_text('untouched')
        self.path.symlink_to(target)
        fresh = VerboseLog(self.path)
        self.assertFalse(fresh.status()['available'])
        self.assertEqual(target.read_text(), 'untouched')

    def test_provider_failure_does_not_disable_toggle_or_publish_unmasked_text(self):
        self.enable()
        self.store.secrets = Mock(side_effect=OSError('synthetic credential failure'))
        self.store.capture('stdout', 'PRIVATE_VALUE')
        self.assertTrue(self.store.status()['enabled'])
        self.assertFalse(self.store.status()['available'])
        self.assertFalse(self.path.exists())
        with self.assertRaises(OSError):
            self.store.export()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = VerboseLog(self.root / 'interaction-diagnostics.verbose.log', lambda: ['SYNTHETIC_SECRET'])
        self.store.configure({'enabled': True, 'lines': 500})
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(server, 'verbose_logs', self.store))

    def test_worker_streams_logging_raw_task_log_and_rpc_request_and_response(self):
        with patch.object(server, 'get_task_dir', return_value=self.root / 'task'):
            server.write_task_log('synthetic', 'stdout', 'UNFILTERED_USER_TOKEN=not_an_addon_secret')
        session = AppServerSession(io.StringIO(), lock=threading.RLock(), thread_params={}, turn_params={},
            on_event=lambda _: None, on_approvals=lambda _: None, on_final=lambda _: None,
            is_active=lambda: True, redact=lambda text: text, on_message=server.capture_verbose)
        session.start()
        session.receive({'id': 'worker-1', 'result': {}})
        session._write({'method': 'synthetic', 'params': {'prompt': 'PRIVATE_PROMPT', 'token': 'SYNTHETIC_SECRET'}})
        session.receive({'method': 'unknown/notice', 'params': {'raw_output': 'PRIVATE_OUTPUT'}})
        stream = Tee(io.StringIO(), self.store, 'stderr')
        stream.write('STDERR_MESSAGE\n')
        handler = Handler(self.store)
        handler.emit(logging.LogRecord('synthetic', logging.ERROR, '', 0, 'LOGGING_MESSAGE', (), None))
        result = self.store.export().decode()
        for value in ('UNFILTERED_USER_TOKEN', 'PRIVATE_PROMPT', 'PRIVATE_OUTPUT', 'STDERR_MESSAGE', 'LOGGING_MESSAGE', 'app-server.in', 'app-server.out'):
            self.assertIn(value, result)
        self.assertNotIn('SYNTHETIC_SECRET', result)

    def test_installed_stdout_stderr_and_logging_handlers_capture_actual_outputs(self):
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        with patch.object(server.sys, 'stdout', io.StringIO()), patch.object(server.sys, 'stderr', io.StringIO()), patch.object(server.atexit, 'register'):
            try:
                server.install_verbose_logging()
                print('ACTUAL_STDOUT SYNTHETIC_SECRET')
                print('ACTUAL_STDERR', file=server.sys.stderr)
                root_logger.warning('ACTUAL_LOG_RECORD')
            finally:
                root_logger.handlers[:] = original_handlers
        exported = self.store.export().decode()
        for value in ('ACTUAL_STDOUT', 'ACTUAL_STDERR', 'ACTUAL_LOG_RECORD'):
            self.assertIn(value, exported)
        self.assertNotIn('SYNTHETIC_SECRET', exported)

    def test_observer_failure_cannot_change_rpc_or_permission_state(self):
        writer = io.StringIO()
        session = AppServerSession(writer, lock=threading.RLock(), thread_params={}, turn_params={},
            on_event=lambda _: None, on_approvals=lambda _: None, on_final=lambda _: None,
            is_active=lambda: True, redact=lambda text: text, on_message=Mock(side_effect=OSError('synthetic')))
        session.start()
        self.assertEqual(json.loads(writer.getvalue())['method'], 'initialize')
        session.receive({'id': 'worker-1', 'result': {}})
        self.assertFalse(session.finished.is_set())

    def test_provider_reads_each_own_secret_from_synthetic_runtime_state(self):
        auth = self.root / 'codex-home'
        auth.mkdir()
        (auth / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'SYNTHETIC_OPENAI', 'tokens': {
            'access_token': 'SYNTHETIC_ACCESS', 'refresh_token': 'SYNTHETIC_REFRESH', 'id_token': 'SYNTHETIC_ID'}}))
        token_file = self.root / 'worker_api_token'
        token_file.write_text('SYNTHETIC_WORKER')
        options_file = self.root / 'options.json'
        options_file.write_text(json.dumps({'HA_TOKEN': '  SYNTHETIC_HA\n'}))
        with (patch.object(server, 'CODEX_HOME', auth), patch.object(server, 'WORKER_TOKEN_PATH', token_file),
              patch.object(server, 'OPTIONS_PATH', options_file),
              patch.dict(os.environ, {'SUPERVISOR_TOKEN': 'SYNTHETIC_SUPERVISOR'}),
              patch.object(server.relay_reads, 'capability', 'SYNTHETIC_RELAY'),
              patch.object(server.verification, 'capabilities', {'sample':'SYNTHETIC_VERIFY'}),
              patch.object(server.ha_data, 'capabilities', {'sample':'SYNTHETIC_DATA'}),
              patch.dict(server.auth_state, {'user_code':'SYNTHETIC_CODE'}),
              patch.dict(server.remote_control_supervisor.status, {'pairing_code':'SYNTHETIC_PAIR'})):
            values = server.verbose_secrets()
        for value in ('SYNTHETIC_OPENAI','SYNTHETIC_ACCESS','SYNTHETIC_REFRESH','SYNTHETIC_ID','SYNTHETIC_WORKER','SYNTHETIC_HA','SYNTHETIC_SUPERVISOR','SYNTHETIC_RELAY','SYNTHETIC_VERIFY','SYNTHETIC_DATA','SYNTHETIC_CODE','SYNTHETIC_PAIR'):
            self.assertIn(value, values)

    def admin_client(self):
        self.stack.enter_context(patch.object(server, 'api_token', return_value='synthetic-worker'))
        identity = self.stack.enter_context(patch.object(server, 'ingress_admin', return_value=({'user_id': 'synthetic-admin'}, '')))
        ingress = self.stack.enter_context(patch.object(server, 'is_ingress_request', return_value=True))
        return server.app.test_client(), identity, ingress

    def test_verbose_routes_require_an_ingress_administrator(self):
        client, identity, ingress = self.admin_client()
        identity.return_value = (None, 'Not an administrator.')
        for method, path in (('get', '/verbose/settings'), ('post', '/verbose/settings'),
                             ('post', '/verbose/export'), ('post', '/verbose/clear')):
            self.assertEqual(getattr(client, method)(path, json={}).status_code, 403)
        identity.return_value = ({'user_id': 'synthetic-admin'}, '')
        ingress.return_value = False
        response = client.post('/verbose/export', json={'confirmed': True},
                               headers={'Authorization': 'Bearer synthetic-worker'})
        self.assertIn(response.status_code, (401, 403))

    def test_verbose_export_requires_strict_confirmation(self):
        client, _, _ = self.admin_client()
        for value in ({}, {'confirmed': False}, {'confirmed': 1}):
            self.assertEqual(client.post('/verbose/export', json=value).status_code, 400)

    def test_verbose_export_is_a_private_text_attachment(self):
        # This represents arbitrary user content, which verbose output deliberately retains.
        self.store.capture('stdout', 'UNFILTERED_PRIVATE_CANARY')
        client, _, _ = self.admin_client()
        exported = client.post('/verbose/export', json={'confirmed': True})
        self.assertEqual(exported.status_code, 200)
        self.assertIn(b'UNFILTERED_PRIVATE_CANARY', exported.data)
        self.assertEqual(exported.headers['Cache-Control'], 'no-store')
        self.assertEqual(exported.mimetype, 'text/plain')

    def test_verbose_content_is_absent_from_anonymized_export(self):
        self.store.capture('stdout', 'UNFILTERED_PRIVATE_CANARY')
        client, _, _ = self.admin_client()
        self.assertNotIn(b'UNFILTERED_PRIVATE_CANARY', client.get('/diagnostics/export').data)

    def test_clear_route_removes_text_without_disabling_capture(self):
        self.store.capture('stdout', 'old private text')
        client, _, _ = self.admin_client()
        self.assertEqual(client.post('/verbose/clear').status_code, 400)
        self.assertEqual(client.post('/verbose/clear', json={}).status_code, 200)
        self.assertFalse(self.store.path.exists())
        self.assertTrue(client.get('/verbose/settings').json['enabled'])

    def test_verbose_files_are_protected_from_agent_mutation(self):
        for path in ('/data/codex-home/house-rules/verbose/worker.log','/data/codex-home/house-rules/verbose/worker.settings.json','/data/codex-home/house-rules/verbose/.verbose-synthetic.tmp'):
            self.assertTrue(touches_protected_path(path))
            self.assertTrue(command_names_protected_path('cat '+path))
            self.assertTrue(server.gate_rules._policy_restriction('cat '+path))
