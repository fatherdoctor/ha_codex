"""Relay execution and update persistence, using synthetic state only."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_control as rc
from test_server import server


class RelayExecutionTests(unittest.TestCase):
    def test_relay_retains_only_the_same_configured_api_pair_as_chat(self):
        env = rc.remote_env({'HA_TOKEN': 'synthetic', 'HA_URL': 'synthetic-origin',
                             'SUPERVISOR_TOKEN': 'synthetic-supervisor', 'HASSIO_TOKEN': 'synthetic-supervisor',
                             'HA_DATA_CAPABILITY': 'synthetic', 'HA_DATA_SOCKET': 'synthetic',
                             'HA_VERIFICATION_CAPABILITY': 'synthetic', 'CODEX_HOME': '/data/codex-home'})
        self.assertEqual(env, {'HA_TOKEN': 'synthetic', 'HA_URL': 'synthetic-origin',
                               'CODEX_HOME': '/data/codex-home'})

    def test_no_configured_token_has_honest_instructions(self):
        note = server.remote_control_instructions({})
        self.assertIn('No Core API token is configured', note)
        self.assertIn('Never bypass a denial', note)
        self.assertNotIn('Authorization: Bearer', note)

    def test_relay_launch_has_the_config_workspace(self):
        from test_remote_control import FakeProc, wait_for
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            def launch(cmd, **kw):
                calls.append(kw)
                return FakeProc()
            sup = rc.Supervisor('codex', Path(tmp) / 'state', dict, lambda: '', gate=lambda: '',
                                popen=launch, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
            try:
                sup.start()
                self.assertTrue(wait_for(lambda: bool(calls)))
                self.assertEqual(calls[0]['cwd'], '/config')
            finally:
                sup.stop()

    def test_startup_preserves_real_enrollment_key_and_database(self):
        # Pinned rust-v0.166.0 state/src/runtime/remote_control.rs:39-48,84.
        # Actual columns: state/migrations/0024_remote_control_enrollments.sql
        # plus 0037_remote_control_enrollments_enabled.sql.
        # Durable lookup is URL + account + initialize clientInfo.name; a
        # server/display name and random socket are not part of that key.
        from test_cx_pair import Transport
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'
            home = data / 'codex-home'
            home.mkdir(parents=True)
            db = home / 'state_5.sqlite'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE remote_control_enrollments ('
                             'websocket_url TEXT NOT NULL, account_id TEXT NOT NULL, '
                             'app_server_client_name TEXT NOT NULL, server_id TEXT NOT NULL, '
                             'environment_id TEXT NOT NULL, server_name TEXT NOT NULL, '
                             'updated_at INTEGER NOT NULL, remote_control_enabled INTEGER, '
                             'PRIMARY KEY (websocket_url, account_id, app_server_client_name))')
                conn.execute('INSERT INTO remote_control_enrollments VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                             ('wss://relay.invalid', 'synthetic-account', 'ha_codex', 'synthetic-id',
                              'synthetic-environment', 'First room', 1, 1))
            before = db.read_bytes()
            patches = {'CODEX_HOME': home, 'DATA_ROOT': data, 'CODEX_CONFIG_PATH': home / 'config.toml',
                       'SCHEMA_PATH': data / 'schema.json', 'AUTH_QR_DIR': data / 'qr'}
            keys = []
            for name, socket in [('First room', 'one.sock'), ('Other room', 'two.sock')]:
                with patch.multiple(server, **patches), patch.object(server, 'api_token', return_value='synthetic'), \
                        patch.object(server, 'task_root', return_value=data / 'tasks'), \
                        patch.object(server.protected_policy, 'install_instructions'):
                    server.ensure_runtime_files()
                config = tomllib.loads((home / 'config.toml').read_text())
                self.assertEqual(config['sqlite_home'], str(home))
                self.assertNotIn('developer_instructions', config)
                sup = rc.Supervisor('codex', data / 'remote_control.json', dict, lambda: name,
                                    run=lambda *a, **k: subprocess.CompletedProcess(a, 0))
                command = sup._command('app-server')
                self.assertEqual(command[command.index('--hostname') + 1], name)
                transport = Transport()
                client = rc.ControlClient(data / socket, connector=lambda *_: transport)
                client.status()
                actual_name = transport.messages[0]['params']['clientInfo']['name']
                keys.append(('wss://relay.invalid', 'synthetic-account', actual_name))
                with sqlite3.connect(db) as conn:
                    row = conn.execute('SELECT server_id FROM remote_control_enrollments '
                                       'WHERE websocket_url=? AND account_id=? AND app_server_client_name=?',
                                       keys[-1]).fetchone()
                    self.assertEqual(row, ('synthetic-id',))
                self.assertEqual(db.read_bytes(), before)
            self.assertEqual(keys[0], keys[1])
            self.assertEqual(keys[0][-1], 'ha_codex')
            first = rc.Supervisor('codex', data / 'remote_control.json', dict, lambda: 'First room', gate=lambda: '')
            first.save_enabled(True)
            second = rc.Supervisor('codex', data / 'remote_control.json', dict, lambda: 'Other room', gate=lambda: '')
            self.assertTrue(second.enabled())


if __name__ == '__main__':
    unittest.main()
