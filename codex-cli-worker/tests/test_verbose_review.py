"""Review regressions. All credentials, output and paths are synthetic fixtures."""
import io
import json
import logging
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_server import server
import verbose_log
from verbose_log import Handler, Tee, VerboseLog


class VerboseReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'worker.log'
        self.store = VerboseLog(self.path)
        self.store.configure({'enabled': True, 'lines': 500})

    def test_provider_failures_keep_loaded_state_and_do_not_rewrite_retained_log(self):
        self.store.capture('stdout', 'retained before provider failure')
        before = self.path.read_bytes()
        with patch.object(self.store, 'secrets', side_effect=OSError('synthetic provider failure')), \
                patch.object(verbose_log, 'atomic', wraps=verbose_log.atomic) as saved:
            for _ in range(200):
                self.store.capture('stdout', 'not stored')
            self.assertEqual(saved.call_count, 0)
        self.assertTrue(self.store.loaded)
        self.assertFalse(self.store.status()['available'])
        self.assertEqual(self.path.read_bytes(), before)
        self.store.capture('stdout', 'provider recovered')
        self.assertTrue(self.store.status()['available'])
        self.assertIn(b'provider recovered', self.path.read_bytes())

    def test_bulk_capture_masks_every_physical_line_before_export(self):
        # Both spellings are synthetic credentials deliberately planted in a bulk record.
        secret = 'SYNTHETIC_OWN_"\\_SECRET'
        self.store.secrets = lambda: [secret]
        self.store.capture('stdout', secret + '\n' + json.dumps(secret) + '\nprivate prose')
        raw = self.path.read_bytes()
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(json.dumps(secret)[1:-1].encode(), raw)
        self.assertEqual(raw.count(b'[redacted]'), 2)
        self.assertIn(b'private prose', raw)

    def test_export_masks_a_secret_learned_after_capture(self):
        secret = 'SYNTHETIC_LATER_SECRET'
        self.store.capture('stdout', secret)
        self.assertIn(secret.encode(), self.path.read_bytes())
        self.store.secrets = lambda: [secret]
        exported = self.store.export()
        self.assertNotIn(secret.encode(), exported)
        self.assertNotIn(secret.encode(), self.path.read_bytes())
        self.assertIn(b'[redacted]', exported)

    def test_append_refuses_a_file_changed_outside_the_store(self):
        self.store.capture('stdout', 'first record')
        self.path.write_bytes(b'synthetic external replacement\n')
        self.store.capture('stdout', 'must not append')
        self.assertEqual(self.path.read_bytes(), b'synthetic external replacement\n')
        self.assertFalse(self.store.available)
        self.assertFalse(self.store.loaded)

    def test_signin_code_is_remembered_before_its_line_is_stored(self):
        code = 'SYNTHETIC_LOGIN_CODE'
        def stored(task, source, text):
            self.store.capture(source, text)
        with (patch.object(server, 'verbose_logs', self.store),
              patch.object(server, 'parse_login_output', return_value={'code': code, 'url': ''}),
              patch.object(server, 'update_auth_state'),
              patch.object(server, 'write_task_log', side_effect=stored)):
            server.auth_reader_thread(io.StringIO('Sign-in code: ' + code + '\n'))
        raw = self.path.read_bytes()
        self.assertNotIn(code.encode(), raw)
        self.assertIn(b'Sign-in code: [redacted]', raw)

    def test_capture_writes_linear_volume_instead_of_one_file_per_line(self):
        sizes = []
        atomic = verbose_log.atomic
        write = os.write

        def saved(path, raw):
            if path == self.path:
                sizes.append(len(raw))
            return atomic(path, raw)

        def appended(fd, raw):
            sizes.append(len(raw))
            return write(fd, raw)

        with patch.object(verbose_log, 'atomic', side_effect=saved), patch.object(os, 'write', side_effect=appended):
            for number in range(1000):
                self.store.capture('burst', f'record-{number:04} ' + 'x' * 100)
        self.assertLess(sum(sizes), 1000 * 200 * 4)
        self.assertTrue(self.path.read_bytes().endswith(b'record-0999 ' + b'x' * 100 + b'\n'))

    def test_masked_bytes_are_at_rest_before_any_export(self):
        # This value stands in for the worker's own token, not arbitrary user prose.
        self.store.secrets = lambda: ['SYNTHETIC_OWN_SECRET']
        self.store.capture('stdout', 'SYNTHETIC_OWN_SECRET private prose stays')
        raw = self.path.read_bytes()
        self.assertNotIn(b'SYNTHETIC_OWN_SECRET', raw)
        self.assertIn(b'[redacted] private prose stays', raw)

    def test_byte_limit_assertions_happen_outside_observation_callbacks(self):
        self.store = VerboseLog(self.path, max_bytes=4096)
        self.store.configure({'enabled': True, 'lines': 2000})
        sizes = []
        original = verbose_log.atomic

        def saved(path, raw):
            if path == self.path:
                sizes.append(len(raw))
            return original(path, raw)

        with patch.object(verbose_log, 'atomic', side_effect=saved):
            self.store.capture('burst', '\n'.join('x' * 400 for _ in range(250)))
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 4096)
        self.assertLessEqual(self.store.bytes, 4096)
        self.assertLessEqual(self.path.stat().st_size, 4096)

    def test_restart_repairs_oversized_file_and_private_mode_before_export(self):
        self.path.write_bytes(b'synthetic old line\n' * 500)
        self.path.chmod(0o644)
        fresh = VerboseLog(self.path, max_bytes=4096)
        fresh.status()
        self.assertLessEqual(self.path.stat().st_size, 4096)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_settings_use_defaults_with_a_note(self):
        self.store.settings_path.write_text('{broken')
        fresh = VerboseLog(self.path)
        status = fresh.status()
        self.assertTrue(status['available'])
        self.assertFalse(status['enabled'])
        self.assertTrue(status.get('note'))

    def test_clear_works_with_invalid_settings_and_retained_log(self):
        self.store.capture('stdout', 'old private text')
        self.store.settings_path.write_text(json.dumps({'enabled': True, 'lines': 500, 'unexpected': True}))
        fresh = VerboseLog(self.path)
        fresh.clear()
        self.assertFalse(self.path.exists())

    def test_completed_stream_line_cannot_cross_clear(self):
        stream = Tee(io.StringIO(), self.store, 'stdout')
        entered, release = threading.Event(), threading.Event()
        capture = self.store.capture

        def held(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic capture timed out')
            return capture(*args, **kwargs)

        with patch.object(self.store, 'capture', side_effect=held):
            thread = threading.Thread(target=stream.write, args=('OLD_LINE_BEFORE_CLEAR\n',))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.store.clear()
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn(b'OLD_LINE_BEFORE_CLEAR', self.store.export())

    def access(self, path, status=200, method='GET', level=logging.INFO, logger='werkzeug'):
        return logging.LogRecord(logger, level, '', 0, 'synthetic-peer - - [synthetic time] "%s" %s %s\n',
                                 (f'{method} {path} HTTP/1.1', status, '-'), None)

    def test_successful_poll_access_lines_are_excluded(self):
        handler = Handler(self.store)
        for path in ('/status', '/status?refresh=1', '/tasks/sample', '/tasks/sample/activity?after=0',
                     '/tasks/sample/log', '/auth/status', '/remote-control', '/queue'):
            handler.emit(self.access(path))
        self.assertFalse(self.path.exists())

    def test_warning_error_nonpoll_and_other_logger_lines_are_kept(self):
        handler = Handler(self.store)
        cases = [self.access('/status', level=logging.WARNING), self.access('/status', status=500),
                 self.access('/status', method='POST'), self.access('/verbose/export'),
                 self.access('/tasks/sample/attachments/item'), self.access('/status', logger='another')]
        for record in cases:
            handler.emit(record)
        self.assertEqual(self.path.read_bytes().count(b'\n'), len(cases))

    def test_usage_reader_has_a_distinct_source(self):
        with patch.object(server, 'verbose_logs', self.store):
            server._queue_app_server_lines(io.StringIO('synthetic usage response\n'), queue.Queue())
        self.assertIn(b'usage-reader.in', self.path.read_bytes())
        self.assertNotIn(b'app-server.in', self.path.read_bytes())

    def test_credential_files_are_reused_but_rotations_and_live_values_are_seen(self):
        # Only these temporary synthetic files are credential sources for this test.
        home = self.root / 'auth-home'
        home.mkdir()
        options = self.root / 'options.json'
        token = self.root / 'worker-token'
        auth = home / 'auth.json'
        options.write_text(json.dumps({'HA_TOKEN': 'SYNTHETIC_OLD_HA'}))
        token.write_text('SYNTHETIC_WORKER')
        auth.write_text(json.dumps({'tokens': {'access_token': 'SYNTHETIC_OLD_ACCESS'}}))
        cache = verbose_log.CredentialCache()
        with (patch.object(server, 'OPTIONS_PATH', options), patch.object(server, 'WORKER_TOKEN_PATH', token),
              patch.object(server, 'CODEX_HOME', home), patch.object(server, '_verbose_credential_files', cache),
              patch.object(verbose_log, 'private_read', wraps=verbose_log.private_read) as reads):
            for _ in range(100):
                self.assertIn('SYNTHETIC_OLD_ACCESS', server.verbose_secrets())
            self.assertEqual(reads.call_count, 3)
            options.write_text(json.dumps({'HA_TOKEN': 'SYNTHETIC_NEW_HA'}))
            self.assertIn('SYNTHETIC_NEW_HA', server.verbose_secrets())
            self.assertEqual(reads.call_count, 4)
            old_time = auth.stat().st_mtime_ns
            replacement = home / 'replacement'
            replacement.write_text(json.dumps({'tokens': {'access_token': 'SYNTHETIC_NEW_ACCESS'}}))
            os.utime(replacement, ns=(old_time, old_time))
            replacement.replace(auth)
            self.assertIn('SYNTHETIC_NEW_ACCESS', server.verbose_secrets())
            self.assertEqual(reads.call_count, 5)
            with patch.dict(os.environ, {'HA_TOKEN': 'SYNTHETIC_LIVE_ENV'}):
                self.assertIn('SYNTHETIC_LIVE_ENV', server.verbose_secrets())
            self.assertEqual(reads.call_count, 5)
            auth.unlink()
            auth.symlink_to(home / 'missing-target')
            with self.assertRaises(OSError):
                server.verbose_secrets()

    def test_pairing_code_is_masked_when_a_failed_pair_request_clears_status_before_capture(self):
        from remote_control import ControlError, Supervisor
        # A synthetic pairing credential, never a real device or account code.
        code = 'SYNTHETIC-PAIR-CODE'
        options = self.root / 'options.json'
        options.write_text('{}')
        home = self.root / 'home'
        home.mkdir()
        results = []

        class Process:
            stdout = io.StringIO('Pairing code: ' + code + '\n')

            def poll(self):
                return None

            def wait(self):
                relay.wake.set()
                return 0

        class FailedPairing:
            def pair(self, cancelled):
                raise ControlError('synthetic pairing failure')

        relay = Supervisor('synthetic-codex', self.root / 'relay.json', lambda: {}, lambda: 'synthetic',
                           gate=lambda: '', popen=lambda *args, **kwargs: Process(),
                           control_factory=lambda path: FailedPairing(),
                           on_log=lambda line: self.store.capture('relay', line))
        relay.save_enabled(True)
        parse = relay._parse

        def interleaved(line):
            parsed_code = parse(line)
            if parsed_code:
                # Force the precise race: pair() clears the published code, then fails.
                ok, state = relay.pair()
                results.append((ok, state['pairing_code'], server.verbose_secrets()))
            return parsed_code

        self.store.secrets = server.verbose_secrets
        with (patch.object(server, 'OPTIONS_PATH', options),
              patch.object(server, 'WORKER_TOKEN_PATH', self.root / 'absent-worker-token'),
              patch.object(server, 'CODEX_HOME', home),
              patch.object(server, '_verbose_credential_files', verbose_log.CredentialCache()),
              patch.object(server, 'remote_control_supervisor', relay),
              patch.object(relay, '_command', return_value=['synthetic-codex']),
              patch.object(relay, '_monitor_control'),
              patch.object(relay, '_parse', side_effect=interleaved)):
            relay._loop()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertEqual(results[0][1], '')
        self.assertNotIn(code, results[0][2])
        # Assert on the stored bytes before any export can mask them a second time.
        raw = self.path.read_bytes()
        self.assertNotIn(code.encode(), raw)
        self.assertIn(b'Pairing code: [redacted]', raw)
        self.assertEqual(raw.count(b'Pairing code:'), 1)

    def test_relay_output_is_observed_once_while_local_notes_still_are_observed(self):
        from remote_control import Supervisor
        relay = Supervisor('synthetic-codex', self.root / 'relay.json', lambda: {}, lambda: 'synthetic', on_log=lambda line: self.store.capture('relay', line))
        relay._parse('synthetic relay output')
        relay.on_log('synthetic relay output')
        relay._log('synthetic local note')
        raw = self.path.read_bytes()
        self.assertEqual(raw.count(b'synthetic relay output'), 1)
        self.assertEqual(raw.count(b'synthetic local note'), 1)

    def test_real_werkzeug_access_format_is_filtered_without_a_live_server(self):
        from werkzeug.serving import WSGIRequestHandler
        request = WSGIRequestHandler.__new__(WSGIRequestHandler)
        request.command = 'GET'
        request.path = '/status'
        request.request_version = 'HTTP/1.1'
        request.address_string = lambda: 'synthetic-peer'
        request.log_date_time_string = lambda: 'synthetic time'
        logger = logging.getLogger('werkzeug')
        old = logger.handlers[:], logger.level, logger.propagate
        logger.handlers = [Handler(self.store)]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            request.log_request(200)
            self.assertFalse(self.path.exists())
            request.log_request(500)
            logger.warning('synthetic warning')
            logger.info('ordinary message about "GET /status HTTP/1.1" 200 -')
        finally:
            logger.handlers, logger.level, logger.propagate = old
        self.assertEqual(self.path.read_bytes().count(b'\n'), 3)
