"""NNP-safe relay-tool transition and fail-closed launcher regressions."""
import importlib.machinery
import importlib.util
import os
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_control as rc
import relay_diagnostics as diagnostics
import test_rc_exec_f as previous

R = 'local_codex_cli_worker///opt/codex-sandbox/relay-launcher'
T = R + '///opt/codex-sandbox/bwrap.real'

def helper():
    loader = importlib.machinery.SourceFileLoader('relay_tool_fixture', str(ROOT / 'relay-tool-launcher'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RelayFollowupTests(unittest.TestCase):
    def test_tool_stacks_under_nnp_and_checks_the_actual_result(self):
        mod = helper()
        with patch.object(mod, 'read_label', side_effect=[R + ' (enforce)', R + '//&' + T + ' (enforce)']), \
                patch.object(mod, 'read_nnp', return_value='1'), patch.object(mod, 'stack_label') as stack:
            mod.prepare_tool_label()
            stack.assert_called_once_with(T)

    def test_tool_refuses_missing_stack_or_nnp(self):
        mod = helper()
        for after in (R + ' (enforce)', T + ' (enforce)'):
            with self.subTest(after=after), \
                    patch.object(mod, 'read_label', side_effect=[R + ' (enforce)', after]), \
                    patch.object(mod, 'read_nnp', return_value='1'), patch.object(mod, 'stack_label'):
                with self.assertRaises(mod.LaunchError):
                    mod.prepare_tool_label()
        with patch.object(mod, 'read_label', return_value=R + ' (enforce)'), \
                patch.object(mod, 'read_nnp', return_value='0'), patch.object(mod, 'stack_label') as stack:
            with self.assertRaises(mod.LaunchError):
                mod.prepare_tool_label()
            stack.assert_not_called()

    def test_already_stacked_tool_preserves_both_components(self):
        mod = helper()
        with patch.object(mod, 'read_label', return_value=T + '//&' + R + ' (enforce)'), \
                patch.object(mod, 'read_nnp', return_value='1'), patch.object(mod, 'stack_label') as stack:
            mod.prepare_tool_label()
            stack.assert_not_called()

    def test_stack_denial_never_executes_native_bwrap(self):
        mod = helper()
        with patch.object(mod, 'read_label', return_value=R + ' (enforce)'), \
                patch.object(mod, 'read_nnp', return_value='1'), \
                patch.object(mod, 'stack_label', side_effect=PermissionError('synthetic-private-argument')), \
                patch.object(mod.os, 'execv') as execute:
            with self.assertRaises(mod.LaunchError) as caught:
                mod.main()
            self.assertEqual(str(caught.exception), 'AppArmor relay-tool stack transition')
            execute.assert_not_called()

    def test_chat_tool_keeps_its_existing_label(self):
        mod = helper()
        with patch.object(mod, 'read_label', return_value='local_codex_cli_worker///opt/codex-sandbox/bwrap.real (enforce)'), \
                patch.object(mod, 'stack_label') as stack:
            mod.prepare_tool_label()
            stack.assert_not_called()

    def test_only_exact_stack_change_is_allowed(self):
        source = (ROOT / 'apparmor.txt').read_text()
        relay = source.split('  profile /opt/codex-sandbox/relay-launcher', 1)[1]
        self.assertIn('change_profile -> &@{profile_name}///opt/codex-sandbox/bwrap.real,', relay)
        self.assertEqual(sum(line.strip().startswith('change_profile ') for line in source.splitlines()), 1)
        # The public mask/proc wrapper remains unchanged; bwrap.real is now the immutable guard.
        docker = (ROOT / 'Dockerfile').read_text()
        self.assertIn('COPY relay-tool-launcher /opt/codex-sandbox/bwrap.real', docker)
        self.assertIn('/opt/codex-sandbox/bwrap.exec', docker)

    def test_outer_devices_are_minimal_and_shared_memory_is_preserved(self):
        cmd = rc.build_command('codex', 'Synthetic relay', 'app-server')
        start = cmd.index('--bind')
        self.assertEqual(cmd[start:start + 9], ['--bind', '/', '/', '--dev', '/dev', '--bind-try', '/dev/shm', '/dev/shm', '--cap-drop'])
        self.assertNotIn('--dev-bind', cmd)

    def test_probe_requires_nnp_and_both_label_components(self):
        good = {'label': T + '//&' + R + ' (enforce)', 'nnp': '1',
                'environ': 13, 'root': 13, 'direct': 2, 'cwd': 13, 'proc_write': 13}
        diagnostics.verify_tool_state(good, R, ('environ', 'root', 'direct', 'cwd'))
        for key, value in [('nnp', '0'), ('label', R + ' (enforce)'), ('label', T + ' (enforce)'), ('root', 'READABLE')]:
            with self.subTest(key=key, value=value), self.assertRaises(AssertionError):
                diagnostics.verify_tool_state({**good, key: value}, R, ('environ', 'root', 'direct', 'cwd'))

    def test_probe_collects_pty_output_notifications(self):
        import base64
        import json
        from types import SimpleNamespace
        payload = '{"nnp":"1"}\r\n'
        frames = iter([json.dumps({'id': 1, 'result': {}}),
                       json.dumps({'method': 'command/exec/outputDelta', 'params':
                                   {'processId': 'other', 'stream': 'stdout', 'deltaBase64': 'eA=='}}),
                       json.dumps({'method': 'command/exec/outputDelta', 'params':
                                   {'processId': 'synthetic', 'stream': 'stdout',
                                    'deltaBase64': base64.b64encode(payload.encode()).decode()}}),
                       json.dumps({'id': 2, 'result': {'exitCode': 0, 'stdout': '', 'stderr': ''}})])
        transport = SimpleNamespace(recv=lambda: next(frames), send=lambda _: None,
                                    close=lambda **_: None, settimeout=lambda _: None)
        with patch.object(rc.ControlClient, '_connect', return_value=transport):
            result = diagnostics.exec_probe_command(Path('/synthetic-control'),
                                                     {'processId': 'synthetic', 'tty': True})
        self.assertEqual(result['stdout'], payload)
        self.assertEqual(result['exitCode'], 0)

    def test_capability_test_skips_after_lsm_refusal_even_with_setpcap(self):
        refused = subprocess.CompletedProcess([], 1, '', 'Relay launch failed closed at capability bounding-set drop.\n')
        state = 'CapBnd:\t00000100\nCapEff:\t00000100\n'
        with patch.object(previous.subprocess, 'run', return_value=refused), \
                patch.object(previous.Path, 'read_text', return_value=state):
            with self.assertRaises(unittest.SkipTest):
                previous.RelayFixTests('test_plain_launcher_drops_actual_capabilities').test_plain_launcher_drops_actual_capabilities()

    def test_launchers_use_isolated_python_and_no_raw_exception(self):
        source = (ROOT / 'relay-launcher').read_text()
        self.assertTrue(source.startswith('#!/usr/bin/python3 -I\n'))
        self.assertIn('KeyError', source)
        self.assertIn('Relay launch failed closed at', source)
        self.assertIn('relay AppArmor label', source)

    def test_missing_status_field_fails_without_a_raw_key_error(self):
        import runpy
        from types import SimpleNamespace
        module = runpy.run_path(str(ROOT / 'relay-launcher'), run_name='launcher_fixture')
        fake_libc = SimpleNamespace(prctl=lambda *args: 0, capset=lambda *args: 0)
        with patch.object(module['ctypes'], 'CDLL', return_value=fake_libc), \
                patch.object(module['Path'], 'read_text', return_value='NoNewPrivs: 1\n'):
            with self.assertRaises(module['LaunchError']) as caught:
                module['drop_capabilities']()
            self.assertEqual(str(caught.exception), 'capability/status verification')

    def test_lab_instructions_require_real_profile_update(self):
        source = (ROOT / 'SANDBOX_TESTING.md').read_text()
        self.assertIn('app_local_codex_cli_worker', source)
        self.assertIn('rebuild does not reload', source)
        self.assertIn('version update', source)


@unittest.skipUnless(os.environ.get('HA_TEST_RELAY_PROFILE'), 'requires the installed enforcing HAOS profile; run shipped relay_diagnostics.py')
class LoadedRelayStackTests(unittest.TestCase):
    def test_actual_tool_command_retains_nnp_and_distinct_stack(self):
        profile = os.environ['HA_TEST_RELAY_PROFILE']
        parent = Path('/proc/self/attr/current').read_text().strip()
        self.assertEqual(parent, profile + ' (enforce)')
        binary = os.environ.get('HA_TEST_CODEX', '/usr/local/bin/codex')
        for plain in (False, True):
            factory = ((lambda command, *args: [rc.RELAY_LAUNCHER, '--plain', command, *args]) if plain
                       else (lambda command, *args: rc.build_command(command, 'Synthetic relay', *args)))
            with self.subTest(plain=plain), tempfile.TemporaryDirectory(prefix='relay-proof-', dir='/data') as folder:
                results = diagnostics.probe(binary, factory, Path(folder), profile)
                self.assertEqual(len(results), 4)
                for item in results:
                    self.assertEqual(item['exit'], 0)
                    self.assertEqual(item['nnp'], '1')


if __name__ == '__main__':
    unittest.main()
