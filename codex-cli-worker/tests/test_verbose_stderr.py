"""Normal logging must still reach stderr; the server and credentials are synthetic."""
import contextlib
import http.client
import io
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_server import server
from verbose_log import VerboseLog


def logging_probe(mode):
    """Run in a fresh process so default handlers are installed as in the worker."""
    from flask import Flask
    from werkzeug.serving import make_server, WSGIRequestHandler

    app = Flask('synthetic_logging_probe')
    app.config['PROPAGATE_EXCEPTIONS'] = False

    @app.get('/status')
    def status():
        return 'synthetic status'

    @app.get('/record')
    def record():
        logging.getLogger('synthetic.library').warning('SYNTHETIC_LIBRARY_WARNING')
        return 'synthetic record'

    @app.get('/broken')
    def broken():
        raise RuntimeError('SYNTHETIC_ROUTE_TRACEBACK')

    class RequestHandler(WSGIRequestHandler):
        def address_string(self):
            return 'synthetic-peer'

    original = io.StringIO()
    with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
        store = VerboseLog(Path(temporary) / 'worker.log')
        store.configure({'enabled': mode == 'on', 'lines': 500})
        stack.enter_context(patch.object(server, 'verbose_logs', store))
        stack.enter_context(contextlib.redirect_stderr(original))
        stack.enter_context(patch.object(server.atexit, 'register'))
        if mode != 'baseline':
            server.install_verbose_logging()
        # Only this disposable local fixture is contacted, never a Home Assistant.
        httpd = make_server('localhost', 0, app, request_handler=RequestHandler)
        worker = threading.Thread(target=httpd.serve_forever)
        worker.start()
        try:
            client = http.client.HTTPConnection('localhost', httpd.server_port, timeout=5)
            try:
                for path in ('/status', '/record', '/broken', '/missing'):
                    client.request('GET', path)
                    client.getresponse().read()
            finally:
                client.close()
        finally:
            httpd.shutdown()
            worker.join(5)
            httpd.server_close()
        if worker.is_alive():
            raise AssertionError('Synthetic server failed to stop')
        # Use a dedicated logger, so this exercises Python's last-resort handler.
        logging.getLogger('synthetic.last_resort').warning('SYNTHETIC_LAST_RESORT')
        raw = store.path.read_text() if store.path.exists() else ''
    return {'stderr': original.getvalue(), 'verbose': raw}


class StderrPreservationTests(unittest.TestCase):
    def probe(self, mode):
        result = subprocess.run([sys.executable, __file__, '--probe', mode],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_server_keeps_normal_stderr_with_verbose_off_and_on(self):
        for mode in ('baseline', 'off', 'on'):
            with self.subTest(mode=mode):
                result = self.probe(mode)
                for text in ('GET /status HTTP/', 'GET /record HTTP/', 'GET /missing HTTP/',
                             'Traceback (most recent call last)', 'SYNTHETIC_ROUTE_TRACEBACK',
                             'SYNTHETIC_LIBRARY_WARNING', 'SYNTHETIC_LAST_RESORT'):
                    self.assertIn(text, result['stderr'])
                if mode != 'on':
                    self.assertEqual(result['verbose'], '')

    def test_preconfigured_stderr_handler_keeps_its_format_and_is_observed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = VerboseLog(Path(temporary) / 'worker.log')
            store.configure({'enabled': True, 'lines': 500})
            original = io.StringIO()
            handler = logging.StreamHandler(original)
            handler.setFormatter(logging.Formatter('CUSTOM %(message)s'))
            logger = logging.getLogger('synthetic.preconfigured')
            old = logger.handlers[:], logger.propagate
            logger.handlers, logger.propagate = [handler], False
            try:
                with (patch.object(server, 'verbose_logs', store),
                      contextlib.redirect_stderr(original),
                      patch.object(server.sys, 'stdout', io.StringIO()),
                      patch.object(server.atexit, 'register')):
                    server.install_verbose_logging()
                    logger.warning('SYNTHETIC_PRECONFIGURED')
            finally:
                logger.handlers, logger.propagate = old
                handler.close()
            self.assertEqual(original.getvalue(), 'CUSTOM SYNTHETIC_PRECONFIGURED\n')
            self.assertEqual(store.path.read_bytes().count(b'CUSTOM SYNTHETIC_PRECONFIGURED'), 1)

    def test_real_server_filters_only_successful_polls_without_duplicate_records(self):
        result = self.probe('on')
        self.assertNotIn('GET /status HTTP/', result['verbose'])
        for text in ('GET /record HTTP/', 'GET /missing HTTP/', 'Traceback (most recent call last)',
                     'RuntimeError: SYNTHETIC_ROUTE_TRACEBACK', 'SYNTHETIC_LIBRARY_WARNING',
                     'SYNTHETIC_LAST_RESORT'):
            self.assertEqual(result['verbose'].count(text), 1, text)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(logging_probe(sys.argv[2])))
    else:
        unittest.main()
