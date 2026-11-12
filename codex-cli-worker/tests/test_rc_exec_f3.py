"""HAOS parser ABI compatibility and AppArmor-specific attribute regressions."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
import remote_control as rc


def tool():
    loader = importlib.machinery.SourceFileLoader('tool_f3', str(ROOT / 'relay-tool-launcher'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AttributeTests(unittest.TestCase):
    def test_read_prefers_apparmor_specific_attribute(self):
        for module in [tool(), type('Fixture', (), runpy.run_path(str(ROOT / 'relay-launcher'), run_name='f3_fixture'))]:
            seen = []
            def read(path):
                seen.append(str(path))
                return 'sample_codex_cli_worker (enforce)\n'
            with patch.object(Path, 'read_text', autospec=True, side_effect=read):
                self.assertEqual(module.read_label(), 'sample_codex_cli_worker (enforce)')
            self.assertEqual(seen, ['/proc/self/attr/apparmor/current'])

    def test_missing_specific_attribute_uses_legacy_interface(self):
        mod = tool()
        seen = []
        def read(path):
            seen.append(str(path))
            if len(seen) == 1:
                raise FileNotFoundError
            return 'sample_codex_cli_worker (enforce)\n'
        with patch.object(Path, 'read_text', autospec=True, side_effect=read):
            self.assertEqual(mod.read_label(), 'sample_codex_cli_worker (enforce)')
        self.assertEqual(seen, ['/proc/self/attr/apparmor/current', '/proc/self/attr/current'])

    def test_permission_denial_never_falls_back_to_another_lsm(self):
        mod = tool()
        with patch.object(Path, 'read_text', side_effect=PermissionError), patch.object(mod.os, 'execv') as execute:
            with self.assertRaises(mod.LaunchError):
                mod.main()
            execute.assert_not_called()
        opened = mock_open()
        opened.side_effect = PermissionError
        with patch('builtins.open', opened):
            with self.assertRaises(PermissionError):
                mod.stack_label('synthetic-target')
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[0], '/proc/self/attr/apparmor/current')

    def test_stack_write_uses_specific_interface(self):
        mod = tool()
        opened = mock_open()
        with patch('builtins.open', opened):
            mod.stack_label('synthetic-target')
        opened.assert_called_once_with('/proc/self/attr/apparmor/current', 'w')
        opened().write.assert_called_once_with('stack synthetic-target')


class MappingTests(unittest.TestCase):
    def test_named_route_maps_root_to_nonzero_ids_before_dropping_caps(self):
        command = rc.build_command('codex', 'Synthetic relay', 'app-server')
        self.assertEqual(command[command.index('--uid') + 1], '1000')
        self.assertEqual(command[command.index('--gid') + 1], '1000')
        self.assertEqual(command[command.index('--hostname') + 1], 'Synthetic relay')
        self.assertLess(command.index('--uid'), command.index('--cap-drop'))
        self.assertEqual(command[-3:], ['--', 'codex', 'app-server'])

    def launcher(self, arguments, uid=0, gid=0):
        module = runpy.run_path(str(ROOT / 'relay-launcher'), run_name='mapped_fixture')
        dropped, executed = Mock(), Mock()
        with patch.dict(module['main'].__globals__, {
                'read_label': lambda: 'local_codex_cli_worker///opt/codex-sandbox/relay-launcher (enforce)',
                'drop_capabilities': dropped}), \
                patch.object(sys, 'argv', ['relay-launcher', *arguments]), \
                patch.object(os, 'getuid', return_value=uid), \
                patch.object(os, 'getgid', return_value=gid), \
                patch.object(os, 'execv', executed):
            module['main']()
        return dropped, executed

    def test_plain_maps_before_capability_drop_and_preserves_literal_arguments(self):
        dropped, executed = self.launcher(['--plain', '/bin/echo', '--uid', '0'])
        dropped.assert_not_called()  # Root mapping needs the setup capabilities.
        binary, command = executed.call_args.args
        self.assertEqual(binary, rc.RELAY_BWRAP)
        self.assertEqual(command[command.index('--uid') + 1], '1000')
        self.assertEqual(command[command.index('--gid') + 1], '1000')
        self.assertNotIn('--unshare-uts', command)
        self.assertEqual(command[-4:], ['--mapped-plain', '/bin/echo', '--uid', '0'])

    def test_mapped_plain_rejects_root_uid_or_gid(self):
        for uid, gid in ((0, 1000), (1000, 0)):
            with self.subTest(uid=uid, gid=gid), self.assertRaisesRegex(ValueError, 'non-root uid/gid'):
                self.launcher(['--mapped-plain', '/bin/true'], uid, gid)

    def test_mapped_plain_drops_and_verifies_caps_before_payload(self):
        dropped, executed = self.launcher(['--mapped-plain', '/bin/true'], 1000, 1000)
        dropped.assert_called_once_with()
        executed.assert_called_once_with('/bin/true', ['/bin/true'])


@unittest.skipUnless(os.environ.get('HA_TEST_RELAY_NAMESPACE') == '1', 'requires explicit native namespace probe')
class NativeMappingTests(unittest.TestCase):
    def test_real_root_start_preserves_owner_only_file_and_private_hostname(self):
        with tempfile.TemporaryDirectory(prefix='mapped-owner-') as directory:
            marker = Path(directory) / 'root-owned'
            marker.write_text('synthetic-only')
            marker.chmod(0o600)
            payload = ('import os,sys,subprocess;from pathlib import Path;'
                       'p=Path(sys.argv[1]);assert os.getuid()==os.getgid()==1000;'
                       'assert p.stat().st_uid==1000;assert p.read_text()=="synthetic-only";'
                       'p.write_text("updated");assert os.uname().nodename=="Synthetic relay";'
                       's=dict(x.split(":",1) for x in Path("/proc/self/status").read_text().splitlines() if ":" in x);'
                       'assert s["NoNewPrivs"].strip()=="1";'
                       'assert all(int(s[k],16)==0 for k in ("CapInh","CapPrm","CapEff","CapBnd","CapAmb"));'
                       'subprocess.run(["/usr/bin/bwrap","--unshare-user","--bind","/","/","--dev","/dev","--","/bin/true"],check=True)')
            command = rc.build_command(sys.executable, 'Synthetic relay', '-I', '-c', payload, str(marker))[1:]
            command[0] = '/usr/bin/bwrap'
            # Only the disposable container-root simulator has setup caps.
            outer = ['/usr/bin/bwrap', '--unshare-user', '--uid', '0', '--gid', '0',
                     '--bind', '/', '/', '--dev', '/dev', '--cap-add', 'ALL', '--']
            result = subprocess.run([*outer, *command], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text(), 'updated')


@unittest.skipUnless(os.environ.get('HA_TEST_APPARMOR_317'), 'requires explicitly built SHA-verified HAOS AppArmor 3.1.7 parser')
class HAOSParserTests(unittest.TestCase):
    def test_shipped_abi_compiles_and_missing_abi_fails_for_each_supervisor_name(self):
        parser = Path(os.environ['HA_TEST_APPARMOR_317'])
        includes = Path(os.environ['HA_TEST_APPARMOR_INCLUDES'])
        version = subprocess.run([str(parser), '--version'], capture_output=True, text=True, check=True)
        self.assertIn('AppArmor parser version 3.1.7\n', version.stdout)
        shipped = (ROOT / 'apparmor.txt').read_text()
        self.assertIn('abi <abi/3.0>,', shipped)
        for name in ('local_codex_cli_worker', 'f3c0ffee_codex_cli_worker'):
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'profile'
                source = shipped.replace('profile codex_cli_worker ', 'profile ' + name + ' ', 1)
                cmd = [str(parser), '-QTK', '-j1', '-b', str(includes), '--kernel-features', str(includes / 'abi/3.0'), str(path)]
                path.write_text(source.replace('abi <abi/3.0>,', ''))
                before = subprocess.run(cmd, capture_output=True, text=True)
                self.assertNotEqual(before.returncode, 0)
                self.assertIn('does not support stacking', before.stderr)
                path.write_text(source)
                after = subprocess.run(cmd, capture_output=True, text=True)
                self.assertEqual(after.returncode, 0, after.stderr)


if __name__ == '__main__':
    unittest.main()
