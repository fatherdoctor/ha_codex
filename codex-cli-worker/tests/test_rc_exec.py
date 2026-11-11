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
    def test_installed_package_exec_is_inherited_in_both_profiles(self):
        parent, child = (ROOT / 'apparmor.txt').read_text().split(
            '  profile /opt/codex-sandbox/bwrap.real', 1)
        for profile in (parent, child):
            self.assertIn('/opt/codex/*/bin/** rix,', profile)
        self.assertNotIn('capability sys_admin,', parent)
        self.assertNotIn('/data/** rix', child)

    def test_named_wrapper_keeps_capability_drop_and_api_transport(self):
        cmd = rc.build_command('/usr/local/bin/codex', rc.DEFAULT_NAME, 'remote-control', '--no-daemon')
        self.assertEqual(cmd[cmd.index('--cap-drop') + 1], 'ALL')
        child = (ROOT / 'apparmor.txt').read_text().split(
            '  profile /opt/codex-sandbox/bwrap.real', 1)[1]
        self.assertIn('    network,', child)

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

    def test_startup_keeps_enrollment_and_publishes_api_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'
            home = data / 'codex-home'
            home.mkdir(parents=True)
            db = home / 'state_5.sqlite'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE remote_control_enrollments (server_id TEXT, server_name TEXT)')
                conn.execute('INSERT INTO remote_control_enrollments VALUES (?, ?)', ('synthetic-id', rc.DEFAULT_NAME))
            before = db.read_bytes()
            patches = {'CODEX_HOME': home, 'DATA_ROOT': data, 'CODEX_CONFIG_PATH': home / 'config.toml',
                       'SCHEMA_PATH': data / 'schema.json', 'AUTH_QR_DIR': data / 'qr'}
            with patch.multiple(server, **patches), patch.object(server, 'api_token', return_value='synthetic'), \
                    patch.object(server, 'task_root', return_value=data / 'tasks'), \
                    patch.object(server, 'read_options', return_value={'HA_TOKEN': 'synthetic'}), \
                    patch.object(server.protected_policy, 'install_instructions'):
                for _image in ('old', 'updated'):
                    server.ensure_runtime_files()
                    config = tomllib.loads((home / 'config.toml').read_text())
                    self.assertEqual(config['sqlite_home'], str(home))
                    self.assertIn('HA_URL and HA_TOKEN', config['developer_instructions'])
                    self.assertIn('/config/AGENTS.md', config['developer_instructions'])
                    self.assertNotIn('synthetic', config['developer_instructions'])
                    self.assertEqual(db.read_bytes(), before)
            env = rc.remote_env({'CODEX_HOME': str(home), 'HOME': str(data)})
            first = rc.Supervisor('codex', data / 'remote_control.json', lambda: env, lambda: '', gate=lambda: '')
            first.save_enabled(True)
            second = rc.Supervisor('codex', data / 'remote_control.json', lambda: env, lambda: '', gate=lambda: '')
            self.assertTrue(second.enabled())
            self.assertEqual(rc.machine_name(first.name_factory()), rc.machine_name(second.name_factory()))
            self.assertEqual(second.env_factory()['CODEX_HOME'], str(home))
            self.assertEqual(db.read_bytes(), before)


@unittest.skipUnless(os.environ.get('HA_CODEX_PROFILE_EXEC_TEST') == '1',
                     'requires the installed worker image and its loaded AppArmor profile; lead lab check')
class ShippedProfileExecutionTests(unittest.TestCase):
    def test_named_remote_child_executes_sandbox_and_parent_commands(self):
        """No relay/auth/HA access: exercise the exact private-name process tree."""
        self.assertIsNotNone(shutil.which('aa-exec'), 'run on the host with the shipped profile loaded')
        home = tempfile.TemporaryDirectory(prefix='rc-exec-proof-', dir='/data')
        self.addCleanup(home.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('HA_', 'HASS_', 'CODEX_', 'OPENAI_'))
               and k not in ('SUPERVISOR_TOKEN', 'HASSIO_TOKEN')}
        env.update(CODEX_HOME=home.name, HOME=home.name)
        for mode in ('read-only', 'workspace-write'):
            command = rc.build_command('/usr/local/bin/codex', rc.DEFAULT_NAME,
                                      'sandbox', '--config', 'sandbox_mode=' + json.dumps(mode),
                                      '--', '/bin/sh', '-c', 'printf profile-sandbox-ok')
            result = subprocess.run(['aa-exec', '-p', 'codex_cli_worker', '--', *command],
                                    cwd='/config', env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('profile-sandbox-ok', result.stdout)
        # Represents execution after approval, not an automatic approval.
        command = rc.build_command('/bin/sh', rc.DEFAULT_NAME, '-c', 'printf profile-parent-ok')
        result = subprocess.run(['aa-exec', '-p', 'codex_cli_worker', '--', *command],
                                cwd='/config', env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('profile-parent-ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
