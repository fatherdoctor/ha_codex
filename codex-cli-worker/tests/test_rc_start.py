"""CX-RC-START regressions using disposable configuration and fake transports."""
import io
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import approvals
import gate_rules as rules
import remote_control as rc


class StartRegressions(unittest.TestCase):
    def test_name_uses_child_profile_without_parent_capabilities(self):
        command = rc.build_command('codex', 'Home Assistant', 'remote-control', '--no-daemon')
        self.assertEqual(command[:2], [rc.RELAY_LAUNCHER, rc.RELAY_BWRAP])
        self.assertIn('--unshare-uts', command)
        self.assertEqual(command[command.index('--hostname') + 1], 'Home Assistant')
        self.assertIn('--cap-drop', command)
        profile = (ROOT / 'apparmor.txt').read_text()
        parent, child = profile.split('  profile /opt/codex-sandbox/bwrap.real', 1)
        self.assertNotIn('capability sys_admin,', parent)
        self.assertIn('/opt/codex-sandbox/bwrap rix,', child)

    def test_namespace_failure_falls_back_once_and_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = rc.Supervisor('codex', Path(tmp)/'state', dict, lambda: 'Home Assistant', gate=lambda: '',
                                run=lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'namespace denied'))
            self.assertEqual(sup._command('remote-control', '--no-daemon'), [rc.RELAY_LAUNCHER, '--plain', 'codex', 'remote-control', '--no-daemon'])
            self.assertEqual(sup.snapshot()['name_state'], 'namespace-unavailable')
            self.assertIn('without', sup.snapshot()['name_warning'])
            self.assertEqual(sup._command('remote-control', 'pair'), [rc.RELAY_LAUNCHER, '--plain', 'codex', 'remote-control', 'pair'])

    def test_requirements_exclude_never_and_self_test_checks_it(self):
        import tomllib
        text = (ROOT/'gate/requirements.toml').read_text()
        self.assertNotIn('never', tomllib.loads(text)['allowed_approval_policies'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'req.toml'
            path.write_text(text.replace('["on-request"]', '["on-request", "never"]'))
            self.assertIn('approval', rc.requirements_problem(path))

    def test_relay_fallback_is_honest_without_inventing_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = rc.Supervisor('codex', Path(tmp)/'state', dict, lambda: '', gate=lambda: '')
            sup._parse('configWarning: sandbox mode danger-full-access not allowed; falling back to read-only')
            self.assertIn('downgraded', sup.snapshot()['policy_warning'])
            self.assertTrue(any('downgraded' in line for line in sup.snapshot()['log']))
            self.assertTrue('read-only' in (ROOT/'web/index.html').read_text())

    def test_adapter_refuses_full_access_before_initialization(self):
        for thread, turn in [({'sandbox':'danger-full-access'}, {}),
                             ({'config':{'sandbox_mode':'danger-full-access'}}, {}),
                             ({}, {'sandboxPolicy':{'type':'dangerFullAccess'}})]:
            with self.subTest(thread=thread, turn=turn):
                writer = io.StringIO()
                session = approvals.AppServerSession(writer, lock=threading.RLock(), thread_params=thread,
                    turn_params=turn, on_event=lambda _:None, on_approvals=lambda _:None,
                    on_final=lambda _:None, is_active=lambda:True, redact=lambda v:v)
                with self.assertRaisesRegex(ValueError, '(?i)full.access'):
                    session.start()
                self.assertEqual(writer.getvalue(), '')


class PatchRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.object(rules, 'CONFIG_ROOT', self.root)
        patcher.start(); self.addCleanup(patcher.stop)

    def check(self, body):
        return rules.check_tool_call('PreToolUse', {'hook_event_name':'PreToolUse',
            'tool_name':'apply_patch','tool_input':{'command':body}}, True, rules.load_rules())

    def test_edits_below_existing_definition_are_refused(self):
        for key in ['shell_command', 'rest_command', 'command_line']:
            with self.subTest(key=key):
                (self.root/'configuration.yaml').write_text(f'{key}:\n  harmless: old\n')
                self.assertTrue(self.check('*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-  harmless: old\n+  harmless: changed\n*** End Patch'))

    def test_ordinary_edit_around_unchanged_definition_is_allowed(self):
        (self.root/'configuration.yaml').write_text('shell_command:\n  existing: unchanged\nlight:\n  name: before\n')
        self.assertFalse(self.check('*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-  name: before\n+  name: after\n*** End Patch'))

    def test_unknown_context_and_symlink_fail_closed(self):
        (self.root/'x.yaml').symlink_to(self.root/'missing.yaml')
        self.assertTrue(self.check('*** Begin Patch\n*** Update File: x.yaml\n@@\n-old\n+new\n*** End Patch'))
        (self.root/'plain.yaml').write_text('name: real\n')
        self.assertTrue(self.check('*** Begin Patch\n*** Update File: plain.yaml\n@@\n-name: absent\n+name: new\n*** End Patch'))

    def test_multi_hunk_and_new_file(self):
        (self.root/'plain.yaml').write_text('name: first\nother: stable\nlast: old\n')
        self.assertFalse(self.check('*** Begin Patch\n*** Update File: plain.yaml\n@@\n-name: first\n+name: second\n@@\n-last: old\n+last: new\n*** End Patch'))
        self.assertTrue(self.check('*** Begin Patch\n*** Add File: new.yaml\n+shell_command:\n+  start: echo example\n*** End Patch'))


class WatchRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'config'; self.root.mkdir()
        self.path = self.root/'configuration.yaml'
        self.before = 'shell_command:\n  example: echo before\n'
        self.path.write_text(self.before)
        self.watch = rc.ConfigWatch(self.root, Path(self.tmp.name)/'private/baseline.json')
        self.watch.take_baseline()

    def test_always_on_block_change_is_undone_and_survives_restart(self):
        self.path.write_text(self.before.replace('before', 'after'))
        found = self.watch.new_findings(remote_on=True)
        self.assertEqual(self.path.read_text(), self.before)
        self.assertEqual(found[0]['undone'], 'restored')
        again = rc.ConfigWatch(self.root, self.watch.baseline_path)
        self.assertEqual(again.new_findings(remote_on=False)[0]['undone'], 'restored')

    def test_off_reports_but_does_not_undo(self):
        self.path.write_text(self.before.replace('before', 'after'))
        self.assertTrue(self.watch.new_findings(remote_on=False))
        self.assertIn('after', self.path.read_text())

    def test_new_planted_file_removed_but_benign_file_kept(self):
        bad = self.root/'new.yaml'; bad.write_text('rest_command:\n  example: {}\n')
        good = self.root/'good.yaml'; good.write_text('name: example\n')
        found = self.watch.new_findings(remote_on=True)
        self.assertFalse(bad.exists()); self.assertTrue(good.exists())
        self.assertEqual(found[0]['undone'], 'removed')

    def test_corrupt_copy_never_overwrites_current_file(self):
        data = json.loads(self.watch.baseline_path.read_text())
        data['copies']['configuration.yaml']['data'] = 'Y29ycnVwdA=='
        self.watch.baseline_path.write_text(json.dumps(data))
        self.path.write_text(self.before.replace('before', 'after'))
        found = self.watch.new_findings(remote_on=True)
        self.assertIn('after', self.path.read_text())
        self.assertFalse(found[0]['undone'])

    def test_symlink_directory_not_restored(self):
        other = self.root.parent/'outside'; other.mkdir()
        (other/'x.yaml').write_text('name: unchanged\n')
        (self.root/'linked').symlink_to(other, target_is_directory=True)
        self.watch.new_findings(remote_on=True)
        self.assertEqual((other/'x.yaml').read_text(), 'name: unchanged\n')
        self.assertEqual(self.watch.baseline_path.stat().st_mode & 0o777, 0o600)

    def test_device_edit_is_neither_reported_nor_undone(self):
        self.path.write_text('- action: camera.turn_off\n')
        self.assertEqual(self.watch.new_findings(remote_on=True), [])
        self.assertEqual(self.path.read_text(), '- action: camera.turn_off\n')

class FurtherRegressions(unittest.TestCase):
    def test_captured_fixture_shapes_and_existing_definition_cases(self):
        fixture = json.loads((ROOT/'tests/fixtures/gate_tool_calls.json').read_text())
        for case in fixture['cases']:
            value = case['input']
            if not isinstance(value, dict):
                continue
            self.assertIn(value['tool_name'], {'Bash', 'apply_patch'})
            self.assertIsInstance(value['tool_input']['command'], str)
            self.assertNotIn('sandbox_permissions', value['tool_input'])
        self.assertTrue(any('existing shell_command' in c['name'] for c in fixture['cases']))

    def test_successful_name_probe_and_pair_share_namespace_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def probe(cmd, **kw):
                calls.append((cmd, kw))
                return subprocess.CompletedProcess(cmd, 0, '', '')
            sup = rc.Supervisor('codex', Path(tmp)/'state', lambda:{'HA_TOKEN':'synthetic', 'PATH':'/bin'},
                                lambda:'Home Assistant', gate=lambda:'', run=probe)
            command = sup._command('remote-control', '--no-daemon')
            pair = sup._command('remote-control', 'pair')
            self.assertEqual(command[:-2], pair[:-2]); self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][-1], '/bin/true')
            self.assertEqual(calls[0][1]['env']['HA_TOKEN'], 'synthetic')
            self.assertEqual(sup.snapshot()['name_state'], 'private-uts')

    def test_real_launch_namespace_failure_does_not_restart_in_name_loop(self):
        from test_remote_control import FakeControl, FakeProc, wait_for
        with tempfile.TemporaryDirectory() as tmp:
            commands = []; procs = []
            def popen(cmd, **kw):
                commands.append(cmd); proc = FakeProc(); procs.append(proc)
                if len(procs) == 1:
                    proc.stdout.lines.put('bwrap: Creating new namespace failed: Operation not permitted\n')
                    proc.exit(1)
                return proc
            sup = rc.Supervisor('codex', Path(tmp)/'state', dict, lambda:'Home Assistant', gate=lambda:'',
                popen=popen, control_factory=FakeControl, run=lambda cmd, **kw:subprocess.CompletedProcess(cmd, 0, '', ''))
            try:
                sup.start()
                self.assertTrue(wait_for(lambda:len(commands) == 2))
                self.assertEqual(commands[1][:5], [rc.RELAY_LAUNCHER,'--plain','codex','app-server','--listen'])
                self.assertEqual(commands[1][-1], '--remote-control')
                self.assertEqual(sup.snapshot()['restarts'], 0)
                self.assertEqual(sup.snapshot()['name_state'], 'namespace-unavailable')
            finally:
                sup.stop()

    def test_always_on_undo_is_wired_only_while_supervisor_is_enabled(self):
        from test_remote_control import wait_for
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'config'; root.mkdir()
            path = root/'configuration.yaml'; before = 'shell_command:\n  example: before\n'; path.write_text(before)
            watch = rc.ConfigWatch(root, Path(tmp)/'baseline.json')
            sup = rc.Supervisor('codex', Path(tmp)/'state', dict, lambda:'', gate=lambda:'', watch=watch, watch_interval=.02)
            sup._launch = lambda:sup.snapshot()
            try:
                sup.start(); path.write_text(before.replace('before','after'))
                self.assertTrue(wait_for(lambda:path.read_text() == before and any(
                    item.get('undone') for item in sup.snapshot()['config_findings'])))
                self.assertTrue(any(item.get('undone') for item in sup.snapshot()['config_findings']))
                sup.stop()
                path.write_text(before.replace('before','after'))
                sup.watch_wake.set()
                self.assertTrue(wait_for(lambda:bool(sup.snapshot()['config_findings'])))
                self.assertIn('after', path.read_text())
            finally:
                watch.clear(); sup.watch_wake.set()
                if sup.watch_thread:sup.watch_thread.join(2)

    def test_skipped_existing_file_is_not_mistaken_for_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'config';root.mkdir()
            path=root/'configuration.yaml'; path.write_text('x' * (rc.WATCH_MAX_BYTES+1))
            watch=rc.ConfigWatch(root,Path(tmp)/'baseline.json');watch.take_baseline()
            path.write_text('shell_command:\n  example: changed\n')
            found=watch.new_findings(remote_on=True)
            self.assertTrue(path.exists());self.assertFalse(found[0]['undone'])

    def test_old_baseline_without_verified_copy_only_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'config';root.mkdir()
            path=root/'configuration.yaml';path.write_text('shell_command:\n  example: before\n')
            watch=rc.ConfigWatch(root,Path(tmp)/'baseline.json');watch.take_baseline()
            data=json.loads(watch.baseline_path.read_text());data.pop('copies');data.pop('complete')
            watch.baseline_path.write_text(json.dumps(data));path.write_text('shell_command:\n  example: after\n')
            found=watch.new_findings(remote_on=True)
            self.assertIn('after',path.read_text());self.assertFalse(found[0]['undone'])

    def test_oversized_patch_refused_promptly(self):
        body='*** Begin Patch\n*** Add File: x.yaml\n+'+'x'*300000+'\n*** End Patch'
        with tempfile.TemporaryDirectory() as tmp, patch.object(rules,'CONFIG_ROOT',Path(tmp)):
            self.assertTrue(rules.check_tool_call('PreToolUse', {'tool_name':'apply_patch','tool_input':{'command':body}},True,rules.load_rules()))


class HookBudgetTests(unittest.TestCase):
    def test_clock_speed_does_not_change_an_ordinary_command_decision(self):
        with patch('time.monotonic', side_effect=AssertionError('clock-based work limit')):
            self.assertEqual(rules.check_tool_call('PreToolUse',
                {'tool_input': {'command': 'ls /config'}}, True, rules.load_rules()), '')

    def test_large_command_input_refused_without_configuration_io(self):
        with patch.object(rules, 'read_config_bytes', side_effect=AssertionError('must not read')):
            self.assertIn('size budget', rules.check_tool_call('PreToolUse',
                {'tool_input':{'command':'x'*(1024*1024+1)}}, True, rules.load_rules()))
