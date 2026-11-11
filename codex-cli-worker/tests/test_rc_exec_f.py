"""Regression checks for the relay's device mounts and distinct security context."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_control as rc
from test_server import server


class RelayFixTests(unittest.TestCase):
    def test_outer_namespace_preserves_devices_after_root_bind(self):
        cmd = rc.build_command('codex', 'Synthetic relay', 'app-server')
        self.assertEqual(cmd[:2], [rc.RELAY_LAUNCHER, rc.RELAY_BWRAP])
        root = cmd.index('--bind')
        self.assertEqual(cmd[root:root + 9], ['--bind', '/', '/', '--dev', '/dev', '--bind-try', '/dev/shm', '/dev/shm', '--cap-drop'])
        self.assertLess(cmd.index('--dev'), cmd.index('--cap-drop'))

    def test_plain_fallback_still_enters_relay_label(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = rc.Supervisor('codex', Path(folder) / 'state', dict, lambda: 'Synthetic',
                                run=lambda *a, **k: subprocess.CompletedProcess(a, 1))
            self.assertEqual(sup._command('app-server'), [rc.RELAY_LAUNCHER, '--plain', 'codex', 'app-server'])

    def test_relay_profile_is_distinct_and_sandbox_network_stays_narrow(self):
        profile = (ROOT / 'apparmor.txt').read_text()
        self.assertIn('/opt/codex-sandbox/relay-launcher cx,', profile)
        relay = profile.split('profile /opt/codex-sandbox/relay-launcher', 1)[1]
        self.assertIn('/opt/codex-sandbox/relay-bwrap rix,', relay)
        self.assertEqual(profile.count('network netlink raw,'), 2)
        self.assertEqual(profile.count('profile /opt/codex-sandbox/bwrap.real'), 2)
        self.assertNotIn('/opt/codex/*/bin/**', profile)  # bare file, already includes inherited exec

    def test_plain_launcher_drops_actual_capabilities(self):
        # Exercise the real capability-drop routine without needing to enter
        # a loaded HAOS relay label in this host-only test.
        program = ("import runpy,sys;from pathlib import Path;"
                   "m=runpy.run_path(sys.argv[1],run_name='capability_fixture');"
                   "\ntry:m['drop_capabilities']()\n"
                   "except m['LaunchError'] as e:print('Relay launch failed closed at '+str(e)+'.',file=sys.stderr);sys.exit(1)\n"
                   "print(Path('/proc/self/status').read_text())")
        result = subprocess.run([sys.executable, '-I', '-c', program, str(ROOT / 'relay-launcher')],
                                capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            self.assertNotIn('CapEff:', result.stdout)
            self.assertNotIn('Traceback', result.stderr)
            self.assertTrue(result.stderr.startswith('Relay launch failed closed at capability'), result.stderr)
            self.skipTest('Host refused capability removal; actual fail-closed exit verified')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = dict(line.split(':', 1) for line in result.stdout.splitlines() if ':' in line)
        for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
            self.assertEqual(int(state[key].strip(), 16), 0, key)
        self.assertEqual(state['NoNewPrivs'].strip(), '1')

    def test_startup_creates_control_mask_anchor_before_any_relay(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            home = data / 'home'
            with patch.multiple(server, CODEX_HOME=home, DATA_ROOT=data, CODEX_CONFIG_PATH=home / 'config.toml',
                                SCHEMA_PATH=data / 'schema.json', AUTH_QR_DIR=data / 'qr'), \
                    patch.object(server, 'api_token', return_value='synthetic'), \
                    patch.object(server, 'task_root', return_value=data / 'tasks'), \
                    patch.object(server.protected_policy, 'install_instructions'):
                server.ensure_runtime_files()
            parent = data / rc.CONTROL_FOLDER
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn('developer_instructions', (home / 'config.toml').read_text())

    def test_relay_instructions_are_fresh_argv_only(self):
        instructions = ['relay-only first']
        with tempfile.TemporaryDirectory() as folder:
            sup = rc.Supervisor('codex', Path(folder) / 'state', dict, lambda: '',
                                instructions_factory=lambda: instructions[0],
                                run=lambda *a, **k: subprocess.CompletedProcess(a, 1))
            for text in ('relay-only first', 'relay-only next'):
                instructions[0] = text
                cmd = sup._command('app-server')
                self.assertEqual(cmd[-3:], ['-c', 'developer_instructions=' + json.dumps(text), 'app-server'])

    def test_control_parent_rejects_symlink_and_insecure_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            other = root / 'other'
            other.mkdir()
            anchor = root / rc.CONTROL_FOLDER
            anchor.symlink_to(other, target_is_directory=True)
            with self.assertRaises(OSError):
                rc.prepare_control_parent(root)
            anchor.unlink()
            anchor.mkdir(mode=0o755)
            anchor.chmod(0o755)
            with self.assertRaises(OSError):
                rc.prepare_control_parent(root)


@unittest.skipUnless(os.environ.get('HA_TEST_CODEX') and os.environ.get('HA_TEST_RELAY_NAMESPACE') == '1',
                     'requires explicit native namespace probe and pinned account-free CLI')
class NativeUnifiedExecTests(unittest.TestCase):
    def test_production_mount_flags_fix_pipe_and_pty_exec(self):
        import shutil
        from relay_diagnostics import probe
        binary = os.environ['HA_TEST_CODEX']
        def factory(command, *args):
            argv = rc.build_command(command, 'Synthetic relay', *args)[1:]
            argv[0] = shutil.which('bwrap')
            return argv
        with tempfile.TemporaryDirectory() as folder:
            results = probe(binary, factory, Path(folder))
        self.assertEqual(len(results), 6)
        self.assertTrue(all(item['exit'] == 0 for item in results))

    def test_nodev_root_bind_reproduces_exec_failure(self):
        import shutil
        from relay_diagnostics import probe
        def broken_factory(command, *args):
            argv = rc.build_command(command, 'Synthetic relay', *args)[1:]
            argv[0] = shutil.which('bwrap')
            index = argv.index('--dev')
            del argv[index:index + 5]
            return argv
        actual_connect = rc.ControlClient._connect
        messages = []
        class ErrorCapture:
            def __init__(self, transport):
                self.transport = transport
            def __getattr__(self, name):
                return getattr(self.transport, name)
            def recv(self):
                frame = self.transport.recv()
                value = json.loads(frame)
                if 'error' in value:
                    messages.append(value['error'].get('message', ''))
                return frame
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(rc.ControlClient, '_connect', side_effect=lambda path: ErrorCapture(actual_connect(path))):
            with self.assertRaises(rc.ControlError):
                probe(os.environ['HA_TEST_CODEX'], broken_factory, Path(folder))
        self.assertTrue(any('Permission denied (os error 13)' in message for message in messages),
                        'Negative must reproduce the specific spawn EACCES, not another RPC failure')


if __name__ == '__main__':
    unittest.main()
