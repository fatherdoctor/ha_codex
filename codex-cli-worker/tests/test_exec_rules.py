"""Managed rules and startup refusal use real protocol boundaries, without accounts."""
import copy
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import approvals
import gate_rules
import remote_control as rc
import tomllib
from test_gate import run_hook

WARNING = {"method": "configWarning", "params": {
    "summary": "Error parsing rules; custom rules not applied.", "details": "synthetic private path"}}


class ManagedRulesTests(unittest.TestCase):
    def setUp(self):
        self.data = tomllib.loads((ROOT / 'gate/requirements.toml').read_text())

    def test_catalogue_is_managed_and_never_allows(self):
        self.assertEqual(rc.managed_exec_rules_problem(self.data), '')
        self.assertTrue(rc.exec_rules_enabled())
        self.assertEqual(len(self.data['rules']['prefix_rules']), 6)
        self.assertEqual({r['decision'] for r in self.data['rules']['prefix_rules']}, {'forbidden'})
        self.assertFalse(list(ROOT.rglob('*.rules')))
        self.assertIn('remote_control.py', (ROOT / 'Dockerfile').read_text())

    def test_remote_control_imports_with_the_apparmor_fixture_modules(self):
        # The existing enforcement fixture copies these three modules, not the
        # entire worker. Keep readiness helpers usable in that shipped boundary.
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('remote_control.py', 'gate_rules.py', 'relay_diagnostics.py'):
                shutil.copyfile(ROOT / name, Path(tmp) / name)
            code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                    'import remote_control; assert remote_control.exec_rules_enabled()')
            result = subprocess.run([sys.executable, '-I', '-c', code, tmp],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_and_permissive_rules_fail_readiness(self):
        cases = [{}, {'prefix_rules': []}]
        for decision in (None, 'allow', 'unknown'):
            rule = copy.deepcopy(self.data['rules']['prefix_rules'][0]); rule['decision'] = decision
            cases.append({'prefix_rules': [rule]})
        for rules in cases:
            with self.subTest(rules=rules):
                self.assertTrue(rc.managed_exec_rules_problem({**self.data, 'rules': rules}))

    def test_invalid_tokens_fail_readiness(self):
        for token in ({}, {'token': ''}, {'any_of': []}, {'any_of': ['ha', None]},
                      {'token': 'ha', 'any_of': ['ha']}, 'ha'):
            data = copy.deepcopy(self.data); data['rules']['prefix_rules'][0]['pattern'] = [token]
            with self.subTest(token=token):
                self.assertTrue(rc.managed_exec_rules_problem(data))

    def test_malformed_managed_file_fails_existing_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'requirements.toml'; p.write_text('[[rules.prefix_rules]\n')
            self.assertIn('could not be read', rc.requirements_problem(p))

    def test_every_catalogued_literal_is_already_refused_by_the_hook(self):
        for rule in self.data['rules']['prefix_rules']:
            options = [t.get('any_of', [t.get('token')]) for t in rule['pattern']]
            for tokens in itertools.product(*options):
                command = shlex.join(tokens)
                with self.subTest(command=command):
                    self.assertTrue(gate_rules.check_tool_call('PreToolUse',
                        {'tool_name': 'Bash', 'tool_input': {'command': command}},
                        True, gate_rules.load_rules()))

    def test_env_and_assignment_wrappers_remain_refused_by_unchanged_hook(self):
        for command in ('env MODE=sample ha supervisor info', 'MODE=sample ha supervisor info',
                        'env MODE=sample cat /proc/1/environ', 'MODE=sample cat /proc/1/environ'):
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as tmp:
                    result = run_hook({'tool_name': 'Bash', 'tool_input': {'command': command}}, True, tmp)
                    self.assertEqual(result.returncode, 2)
                    self.assertTrue(result.stderr)


class WarningTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(rc, '_exec_rules_warning', ''); p.start(); self.addCleanup(p.stop)

    def test_rules_warning_latches_only_a_fixed_reason(self):
        self.assertEqual(rc.record_exec_rules_warning(WARNING), rc.EXEC_RULES_WARNING)
        self.assertEqual(rc.exec_rules_problem(), rc.EXEC_RULES_WARNING)
        self.assertNotIn('synthetic private', rc.exec_rules_problem())
        self.assertFalse(rc.exec_rules_warning_problem({'method': 'configWarning', 'params': {'summary': 'Unrelated setting.'}}))
        self.assertFalse(rc.exec_rules_warning_problem({'method': 'configWarning', 'params': {
            'summary': 'Unrelated setting.', 'path': '/synthetic/rules', 'details': 'rules directory'}}))
        self.assertFalse(rc.exec_rules_warning_problem({'method': 'item/completed', 'params': {'text': 'rules error'}}))

    def test_chat_session_stops_on_rule_warning_without_approval(self):
        writer = io.StringIO(); records = []
        session = approvals.AppServerSession(writer, lock=threading.RLock(), thread_params={}, turn_params={},
            on_event=lambda _: None, on_approvals=records.append, on_final=lambda _: None,
            is_active=lambda: True, redact=lambda s: s)
        session.receive(WARNING)
        self.assertEqual(session.error, rc.EXEC_RULES_WARNING)
        self.assertTrue(session._closed)
        self.assertEqual(records, [])

    def test_observed_relay_warning_blocks_turn_on_and_later_ready_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = rc.Supervisor('unused', Path(tmp) / 'state.json', dict, lambda: '', gate=lambda: '')
            sup._parse(json.dumps(WARNING))
            sup._parse('available for remote control as Synthetic.')
            ok, state = sup.start()
            self.assertFalse(ok); self.assertFalse(state['available']); self.assertFalse(state['enabled'])
            self.assertEqual(state['blocked_reason'], rc.EXEC_RULES_WARNING)
            self.assertEqual(state['state'], 'blocked')

    def test_plain_rules_error_also_blocks_turn_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = rc.Supervisor('unused', Path(tmp) / 'state.json', dict, lambda: '', gate=lambda: '')
            sup._parse('Error parsing rules; custom rules not applied.')
            self.assertFalse(sup.snapshot()['available'])

    def test_native_preflight_failure_blocks_start_and_survives_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(rc, 'gate_problem', return_value=''), \
                patch.object(rc, 'exec_rules_self_test', return_value=rc.EXEC_RULES_WARNING) as check:
            sup = rc.Supervisor('fixture', Path(tmp) / 'state.json', dict, lambda: '')
            ok, state = sup.start()
            self.assertFalse(ok); self.assertFalse(state['available']); self.assertFalse(state['enabled'])
            self.assertEqual(sup.snapshot()['blocked_reason'], rc.EXEC_RULES_WARNING)
            check.assert_called_once()

    def test_control_connection_does_not_discard_rules_notification(self):
        class Transport:
            def settimeout(self, _): pass
            def send(self, _): pass
            def recv(self): return json.dumps(WARNING)
            def close(self, **_): pass
        client = rc.ControlClient(Path('/synthetic'), connector=lambda _: Transport())
        with self.assertRaisesRegex(rc.ControlError, 'could not load command rules'):
            client.enable()
        self.assertEqual(rc.exec_rules_problem(), rc.EXEC_RULES_WARNING)

    def test_unreadable_probe_environment_keeps_turn_on_unavailable(self):
        def unavailable():
            raise OSError('synthetic private detail')
        with tempfile.TemporaryDirectory() as tmp, patch.object(rc, 'gate_problem', return_value=''):
            sup = rc.Supervisor('unused', Path(tmp) / 'state', unavailable, lambda: '')
            ok, state = sup.start()
            self.assertFalse(ok)
            self.assertFalse(state['available'])
            self.assertIn('could not prepare', state['blocked_reason'])
            self.assertNotIn('private detail', state['blocked_reason'])


class LoaderProbeTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(rc, '_exec_rules_warning', ''); p.start(); self.addCleanup(p.stop)

    def probe(self, warning_at=0, timeout=False):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'stub.py'
            script.write_text('''import json,sys,time
for line in sys.stdin:
 m=json.loads(line)
 if m.get('id')==1:
  print(json.dumps({'id':1,'result':{}}),flush=True)
 if m.get('id')==2:
  assert m['method']=='thread/start' and m['params']['ephemeral'] is True
  if TIMEOUT: time.sleep(30)
  if WARNING: print(json.dumps({'method':'configWarning','params':{'summary':'Error parsing rules; custom rules not applied.'}}),flush=True)
  print(json.dumps({'id':2,'result':{'approvalPolicy':'on-request','thread':{'id':'synthetic'}}}),flush=True)
'''.replace('TIMEOUT', repr(timeout)).replace('WARNING', repr(warning_at)))
            def launch(argv, **kwargs):
                self.assertEqual(argv, ['fixture', 'app-server'])
                return subprocess.Popen([sys.executable, '-u', str(script)], **kwargs)
            return rc.exec_rules_self_test('fixture', {'PATH': os.environ.get('PATH', '')}, tmp,
                                        popen=launch, timeout=0.3 if timeout else 3)

    def test_good_loader_opens_only_an_ephemeral_thread(self):
        self.assertEqual(self.probe(), '')

    def test_warning_fails_preflight(self):
        self.assertEqual(self.probe(warning_at=1), rc.EXEC_RULES_WARNING)

    def test_timeout_fails_closed_and_reaps_the_probe(self):
        self.assertIn('did not complete', self.probe(timeout=True))


if __name__ == '__main__':
    unittest.main()
