"""HAOS parser ABI compatibility and AppArmor-specific attribute regressions."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[1]


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
