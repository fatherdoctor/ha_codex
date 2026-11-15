"""The loaded-policy helper distinguishes visible direct pids from nested pids."""
import ast
import errno
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock


class ProcAssertionTests(unittest.TestCase):
    def check(self, codes, mode=None):
        source = Path(__file__).with_name('apparmor_317_check.py').read_text()
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'check_child')
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('RELAY', 'TOOL') for t in node.targets):
                constants[node.targets[0].id] = ast.literal_eval(node.value)
        relay = 'synthetic' + constants['RELAY']
        sibling = Mock(pid=101)
        marker = Mock()
        marker.stat.return_value = types.SimpleNamespace(st_uid=1000, st_gid=1000)
        marker.read_text.return_value = 'synthetic-owner'
        if mode == 'read-only':
            marker.write_text.side_effect = PermissionError(errno.EACCES, 'synthetic denial')
        values = dict(constants, label=lambda: relay+'//&'+relay+constants['TOOL']+' (enforce)', nnp=lambda: '1',
                      os=types.SimpleNamespace(getuid=lambda: 1000, getgid=lambda: 1000),
                      subprocess=types.SimpleNamespace(Popen=Mock(return_value=sibling), PIPE=object()),
                      sys=types.SimpleNamespace(executable=sys.executable), errno_read=Mock(side_effect=codes),
                      Path=Mock(return_value=marker), json=json, print=Mock())
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<loaded-policy-check_child>', 'exec'), values)
        try:
            values['check_child']('synthetic', 100, '/synthetic', mode)
        finally:
            sibling.stdin.close.assert_called_once()
            sibling.wait.assert_called_once_with(timeout=5)

    def test_direct_all_eacces_is_accepted(self):
        self.check([13, 13, 13, 13])

    def test_direct_missing_or_other_denial_never_counts_as_visible_proc_denial(self):
        for index in range(4):
            for code in (1, 2):
                with self.subTest(index=index, code=code), self.assertRaises(AssertionError):
                    values = [13]*4; values[index] = code
                    self.check(values)

    def test_pinned_read_only_keeps_hidden_pid_denials_and_no_config_write(self):
        self.check([1, 2, 13, 13], 'read-only')

    def test_pinned_workspace_write_keeps_hidden_pid_denials_and_config_write(self):
        self.check([2, 1, 13, 13], 'workspace-write')

    def test_readable_proc_view_always_fails_and_child_is_closed(self):
        for mode in (None, 'read-only', 'workspace-write'):
            with self.subTest(mode=mode), self.assertRaises(AssertionError):
                self.check(['READABLE', 13, 13, 13], mode)
