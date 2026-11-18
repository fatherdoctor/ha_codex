"""Managed rules and startup refusal use real protocol boundaries, without accounts."""
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
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import approvals
import gate_rules
import remote_control as rc
import tomllib
from test_gate import run_hook
from test_remote_control import FakeProc

WARNING = {"method": "configWarning", "params": {
    "summary": "Error parsing rules; custom rules not applied.", "details": "synthetic private path"}}


class ManagedRulesTests(unittest.TestCase):
    def setUp(self):
        self.data = tomllib.loads((ROOT / 'gate/requirements.toml').read_text())

    def test_catalogue_is_managed_and_never_allows(self):
        self.assertTrue(rc.exec_rules_enabled())
        self.assertEqual(len(self.data['rules']['prefix_rules']), 6)
        self.assertEqual({r['decision'] for r in self.data['rules']['prefix_rules']}, {'forbidden'})
        self.assertFalse(list(ROOT.rglob('*.rules')))
        self.assertIn('remote_control.py', (ROOT / 'Dockerfile').read_text())

    def test_remote_control_imports_with_the_apparmor_fixture_modules(self):
        # The existing enforcement fixture copies these four modules, not the
        # entire worker. Keep readiness helpers usable in that shipped boundary.
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('remote_control.py', 'gate_rules.py', 'shell_words.py', 'relay_diagnostics.py'):
                shutil.copyfile(ROOT / name, Path(tmp) / name)
            code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                    'import remote_control; assert remote_control.exec_rules_enabled()')
            result = subprocess.run([sys.executable, '-I', '-c', code, tmp],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

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
        self.assertIn('did not answer in time', self.probe(timeout=True))

    def test_incomplete_group_cleanup_cannot_report_success(self):
        stop = rc._stop_exec_rules_probe
        def clean_but_report_failure(proc):
            stop(proc)
            return False
        with patch.object(rc, '_stop_exec_rules_probe', side_effect=clean_but_report_failure):
            self.assertIn('could not stop its process group', self.probe())


@unittest.skipUnless(sys.platform.startswith('linux'), 'process-group cleanup is Linux-specific')
class DescendantCleanupTests(unittest.TestCase):
    SCRIPT = '''import ctypes,json,os,signal,subprocess,sys,time
from pathlib import Path
mode,root,outcome,marker=sys.argv[1:]
marker=Path(marker)
if mode in ('child','sentry'):
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 marker.write_text(str(os.getpid()))
 while True: time.sleep(1)
elif mode=='launcher':
 subprocess.Popen([sys.executable,__file__,'child',root,outcome,str(marker)],
                  stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 deadline=time.monotonic()+3
 while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)
 assert marker.exists()
 if outcome=='exit': sys.exit(0)
 for line in sys.stdin:
  message=json.loads(line)
  if message.get('id')==1:
   print(json.dumps({'id':1,'result':{}}),flush=True)
  if message.get('id')==2:
   if outcome=='timeout': time.sleep(30)
   if outcome=='warning':
    print(json.dumps({'method':'configWarning','params':{'summary':'Error parsing rules; custom rules not applied.'}}),flush=True)
   print(json.dumps({'id':2,'result':{'approvalPolicy':'on-request','thread':{'id':'synthetic'}}}),flush=True)
elif mode=='driver':
 # Adoption is limited to this disposable driver, never the worker or test runner.
 assert ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)==0
 sys.path.insert(0,root)
 import remote_control as rc
 launched=[]
 sentry=subprocess.Popen([sys.executable,__file__,'sentry',root,outcome,str(marker.with_suffix('.sentry'))],
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         start_new_session=True)
 def launch(argv,**kwargs):
  proc=subprocess.Popen([sys.executable,__file__,'launcher',root,outcome,str(marker)],**kwargs)
  launched.append(proc)
  return proc
 def exists(pid,group=False):
  try:
   (os.killpg if group else os.kill)(pid,0)
   return True
  except ProcessLookupError: return False
 try:
  problem=rc.exec_rules_self_test('fixture',{'PATH':os.environ.get('PATH','')},str(marker.parent),
                                popen=launch,timeout=.7 if outcome=='timeout' else 4)
  child=int(marker.read_text())
  record={'problem':problem,'alive':exists(child),'group_exists':exists(launched[0].pid,True),
          'launcher_exited':launched[0].poll() is not None,'unrelated_alive':exists(sentry.pid)}
 finally:
  # Also clean the deliberately failing pre-fix case, so it cannot leak a child.
  for proc in launched:
   try: os.killpg(proc.pid,signal.SIGKILL)
   except ProcessLookupError: pass
  os.killpg(sentry.pid,signal.SIGKILL)
  sentry.wait(timeout=3)
  deadline=time.monotonic()+3
  while time.monotonic()<deadline:
   try: pid,_=os.waitpid(-1,os.WNOHANG)
   except ChildProcessError: break
   if not pid: time.sleep(.01)
 print(json.dumps(record))
'''

    def check_outcome(self, outcome):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'descendant.py'
            script.write_text(self.SCRIPT)
            result = subprocess.run([sys.executable, '-B', str(script), 'driver', str(ROOT),
                                     outcome, str(Path(tmp) / 'ready')],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data['problem'], '' if outcome == 'success' else
                             rc.EXEC_RULES_WARNING if outcome == 'warning' else
                             rc.EXEC_RULES_TIMEOUT if outcome == 'timeout' else
                             'The Codex command-rules self-test did not complete. Restart the app after checking its rule files.')
            self.assertTrue(data['launcher_exited'])
            self.assertFalse(data['alive'], 'TERM-resistant descendant survived the preflight')
            self.assertFalse(data['group_exists'], 'owned process group is not empty')
            self.assertTrue(data['unrelated_alive'], 'an unrelated process group was stopped')

    def test_success_stops_descendant_after_launcher_exits(self):
        self.check_outcome('success')

    def test_warning_stops_descendant_after_launcher_exits(self):
        self.check_outcome('warning')

    def test_timeout_stops_descendant_after_launcher_exits(self):
        self.check_outcome('timeout')

    def test_early_launcher_exit_stops_descendant(self):
        self.check_outcome('exit')


class ExecRulesReviewTests(unittest.TestCase):
    def setUp(self):
        """Give each review case a private state file and warning latch."""
        latch = patch.object(rc, '_exec_rules_warning', '')
        latch.start()
        self.addCleanup(latch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state.json'

    def test_all_supervisor_aliases_are_managed(self):

        """Keep all three supported Supervisor names in the native catalogue."""
        data = tomllib.loads((ROOT / 'gate/requirements.toml').read_text())
        self.assertEqual(set(data['rules']['prefix_rules'][0]['pattern'][1]['any_of']),
                         {'supervisor', 'super', 'su'})

    def test_super_alias_is_refused_in_each_command_form_and_mode(self):

        """Refuse the short alias through each hook classifier, with relay on or off."""
        for remote in (False, True):
            for command in ('ha super info', 'env MODE=sample ha super info',
                            'ha --unknown=value super info', 'echo ha super info'):
                with self.subTest(remote=remote, command=command):
                    self.assertTrue(gate_rules.check_tool_call('PreToolUse',
                        {'tool_name': 'Bash', 'tool_input': {'command': command}},
                        remote, gate_rules.load_rules()))

    def test_unrelated_rules_errors_do_not_latch_or_disable(self):

        """Leave remote control enabled for unrelated plain and structured warnings."""
        supervisor = rc.Supervisor('unused', self.state, dict, lambda: '', gate=lambda: '')
        supervisor.save_enabled(True)
        for line in ('WARN relay: failed to read /synthetic/rules/notes.md',
                     'error: file automation_rules.yaml not found',
                     'thread titled house rules failed to sync',
                     json.dumps({'method': 'configWarning', 'params': {
                         'summary': 'A rules backup failed', 'details': 'synthetic detail'}})):
            with self.subTest(line=line), patch.object(rc, '_exec_rules_warning', ''):
                supervisor._parse(line)
                self.assertEqual(rc.exec_rules_problem(), '')
                self.assertTrue(supervisor.enabled())

    def test_process_output_warning_stops_and_saves_off_the_running_process(self):

        """Stop the process and persist off when its output reports a load warning."""
        proc = FakeProc()
        proc.stdout.lines.put(WARNING['params']['summary'])
        supervisor = rc.Supervisor('fixture', self.state, dict, lambda: 'Fixture',
                                   gate=lambda: '', popen=lambda *args, **kwargs: proc)
        supervisor._name_supported = False
        supervisor.save_enabled(True)
        # Keep the output route independent of the separate control warning route.
        with patch.object(rc.os, 'killpg', side_effect=ProcessLookupError), \
                patch.object(supervisor, '_monitor_control'):
            supervisor._loop()
        self.assertTrue(proc.done.is_set())
        self.assertFalse(supervisor.enabled())
        self.assertEqual(json.loads(self.state.read_text()), {'enabled': False})
        self.assertEqual(supervisor.snapshot()['state'], 'blocked')
        self.assertEqual(supervisor.snapshot()['blocked_reason'], rc.EXEC_RULES_WARNING)

    def test_concurrent_warning_saves_do_not_replace_each_others_temporary_file(self):

        """Let simultaneous warning handlers finish their state writes in order."""
        supervisor = rc.Supervisor('fixture', self.state, dict, lambda: '', gate=lambda: '')
        entered = threading.Event()
        release = threading.Event()
        second_write = threading.Event()
        errors = []
        original = Path.write_text

        def write(path, text, **kwargs):

            """Hold the first writer long enough to detect a competing write."""
            if threading.current_thread().name == 'first-save':
                entered.set()
                if not release.wait(2):
                    raise AssertionError('first writer was not released')
            else:
                second_write.set()
            return original(path, text, **kwargs)

        def save():

            """Capture background errors so the test cannot silently pass."""
            try:
                supervisor.save_enabled(False)
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=save, name='first-save')
        second = threading.Thread(target=save, name='second-save')
        with patch.object(Path, 'write_text', write):
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                second.start()
                overlapped = second_write.wait(0.05)
            finally:
                release.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(overlapped, 'two warning handlers wrote the same temporary file together')
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(self.state.read_text()), {'enabled': False})

    def test_control_warning_stops_and_saves_off_the_running_process(self):

        """Stop and persist off when the control socket reports a load warning."""
        class Transport:
            def settimeout(self, _): pass
            def send(self, _): pass
            def recv(self): return json.dumps(WARNING)
            def close(self, **_): pass
        proc = FakeProc()
        supervisor = rc.Supervisor('fixture', self.state, dict, lambda: 'Fixture', gate=lambda: '',
            control_factory=lambda path: rc.ControlClient(path, connector=lambda _: Transport()))
        supervisor.proc = proc
        supervisor.save_enabled(True)
        with patch.object(rc.os, 'killpg', side_effect=ProcessLookupError):
            supervisor._monitor_control(proc, Path(self.temp.name) / 'unused', threading.Event())
        self.assertTrue(proc.done.is_set())
        self.assertFalse(supervisor.enabled())
        self.assertEqual(json.loads(self.state.read_text()), {'enabled': False})
        self.assertEqual(supervisor.snapshot()['blocked_reason'], rc.EXEC_RULES_WARNING)

    def test_kind_off_ignores_new_and_cached_rule_warnings(self):

        """Keep a disabled kind from blocking through either warning path."""
        with patch.object(rc, 'exec_rules_enabled', return_value=False):
            self.assertEqual(rc.record_exec_rules_warning(WARNING), '')
            with patch.object(rc, '_exec_rules_warning', rc.EXEC_RULES_WARNING):
                self.assertEqual(rc.exec_rules_problem(), '')

    def test_cleanup_refuses_a_launcher_in_an_unowned_process_group(self):

        """Do not signal a process group that the probe launcher does not own."""
        proc = SimpleNamespace(pid=999999, poll=Mock(return_value=None))
        with patch.object(rc.os, 'getpgid', return_value=os.getpgrp()), \
                patch.object(rc.os, 'killpg', side_effect=ProcessLookupError) as signal_group:
            self.assertFalse(rc._stop_exec_rules_probe(proc))
            signal_group.assert_not_called()

    def test_cleanup_never_signals_the_workers_own_group(self):

        """Keep the worker group safe even if an injected launcher names it."""
        proc = SimpleNamespace(pid=os.getpgrp(), poll=Mock(return_value=None))
        with patch.object(rc.os, 'getpgid', return_value=proc.pid), \
                patch.object(rc.os, 'killpg', side_effect=ProcessLookupError) as signal_group:
            self.assertFalse(rc._stop_exec_rules_probe(proc))
            signal_group.assert_not_called()

    def test_timeout_gets_a_retryable_reason_after_cleanup(self):

        """Return a fixed retry reason after a timed-out probe is reaped."""
        problem = LoaderProbeTests.probe(self, timeout=True)
        self.assertEqual(problem, 'The Codex command-rules self-test did not answer in time. Try turning on remote control again.')

    def test_launch_error_gets_a_fixed_retryable_reason(self):

        """Hide launch exception details and allow a later explicit retry."""
        with patch.object(rc, '_stop_exec_rules_probe') as stop:
            problem = rc.exec_rules_self_test('fixture', {}, self.temp.name,
                                             popen=Mock(side_effect=OSError('synthetic private detail')))
        self.assertEqual(problem, 'The Codex command-rules self-test could not start. Try turning on remote control again.')
        stop.assert_not_called()

    def test_turn_on_retries_transient_probe_failure(self):

        """Preserve the reason until the next Turn on request retries the probe."""
        reasons = ('The Codex command-rules self-test did not answer in time. Try turning on remote control again.',
                   'The Codex command-rules self-test could not start. Try turning on remote control again.',
                   'The command-rules self-test could not prepare its environment.')
        for reason in reasons:
            with self.subTest(reason=reason), patch.object(rc, 'gate_problem', return_value=''), \
                    patch.object(rc, 'exec_rules_self_test', side_effect=[reason, '']) as probe:
                supervisor = rc.Supervisor('fixture', self.state, dict, lambda: '')
                supervisor.save_enabled(False)
                with patch.object(supervisor, '_launch', return_value={}) as launch:
                    self.assertFalse(supervisor.start()[0])
                    self.assertEqual(supervisor.snapshot()['blocked_reason'], reason)
                    self.assertTrue(supervisor.start()[0])
                    self.assertTrue(supervisor.enabled())
                    self.assertEqual(probe.call_count, 2)
                    launch.assert_called_once()

    def test_a_rule_warning_stays_blocked_without_an_automatic_retry(self):

        """Require repair and restart after an actual rule-load warning."""
        with patch.object(rc, 'gate_problem', return_value=''), \
                patch.object(rc, 'exec_rules_self_test', return_value=rc.EXEC_RULES_WARNING) as probe:
            supervisor = rc.Supervisor('fixture', self.state, dict, lambda: '')
            self.assertFalse(supervisor.start()[0])
            self.assertFalse(supervisor.start()[0])
            self.assertEqual(probe.call_count, 1)


class ExecRulesStartRaceTests(unittest.TestCase):
    """A warning and Turn on share one decision about the saved choice."""

    def setUp(self):
        """Keep state and warning data private to each synthetic supervisor."""
        latch = patch.object(rc, '_exec_rules_warning', '')
        latch.start()
        self.addCleanup(latch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.supervisor = rc.Supervisor('fixture', Path(self.temp.name) / 'state.json',
                                        dict, lambda: '', gate=lambda: '')

    def test_warning_between_gate_and_enable_keeps_saved_choice_off(self):
        """A baseline callback models the review's deterministic warning interleaving."""
        supervisor = self.supervisor

        def warn_during_baseline():
            """Publish the real warning and persist off before the enable commit."""
            rc.record_exec_rules_warning(WARNING)
            supervisor.save_enabled(False)

        supervisor.watch = SimpleNamespace(take_baseline=warn_during_baseline)
        with patch.object(supervisor, '_launch') as launch, patch.object(supervisor, '_start_watch'):
            accepted, state = supervisor.start()
        self.assertFalse(accepted)
        self.assertFalse(supervisor.enabled())
        self.assertEqual(state['blocked_reason'], rc.EXEC_RULES_WARNING)
        launch.assert_not_called()

    def test_warning_publication_cannot_split_the_enable_commit(self):
        """A second thread tries to latch and save off while Turn on commits its choice."""
        supervisor = self.supervisor
        writing = threading.Event()
        release = threading.Event()
        published = threading.Event()
        errors = []
        save = supervisor.save_enabled

        def held_save(value):
            """Hold only the on write at the decision's persistence boundary."""
            if value:
                writing.set()
                if not release.wait(2):
                    raise AssertionError('enable writer was not released')
            save(value)

        def start():
            """Expose thread failures to the parent test."""
            try:
                supervisor.start()
            except Exception as error:
                errors.append(error)

        def warn():
            """Run the same latch-then-save order as a warning handler."""
            try:
                rc.record_exec_rules_warning(WARNING)
                published.set()
                supervisor.save_enabled(False)
            except Exception as error:
                errors.append(error)

        starter = threading.Thread(target=start)
        warning = threading.Thread(target=warn)
        with patch.object(supervisor, 'save_enabled', held_save), \
                patch.object(supervisor, '_launch'), patch.object(supervisor, '_start_watch'):
            starter.start()
            try:
                self.assertTrue(writing.wait(2))
                warning.start()
                published_early = published.wait(0.05)
            finally:
                release.set()
                starter.join(3)
                if warning.ident is not None:
                    warning.join(3)
        self.assertFalse(starter.is_alive() or warning.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(published_early, 'a warning split the enable decision from its file write')
        self.assertTrue(published.is_set())
        self.assertFalse(supervisor.enabled())
        self.assertEqual(rc.exec_rules_problem(), rc.EXEC_RULES_WARNING)

    def test_warning_after_enable_but_before_launch_saves_off(self):
        """A late chat warning must persist off even before a control monitor exists."""
        supervisor = self.supervisor
        supervisor.popen = Mock(side_effect=AssertionError('a blocked relay must not launch'))
        with patch.object(supervisor, '_start_watch',
                          side_effect=lambda: rc.record_exec_rules_warning(WARNING)):
            accepted, _ = supervisor.start()
        supervisor.thread.join(2)
        self.assertTrue(accepted)  # The on decision precedes this warning.
        self.assertFalse(supervisor.thread.is_alive())
        supervisor.popen.assert_not_called()
        self.assertFalse(supervisor.enabled())
        self.assertEqual(supervisor.snapshot()['blocked_reason'], rc.EXEC_RULES_WARNING)

    def test_disabled_kind_does_not_block_the_enable_commit(self):
        """A future disabled kind must ignore an earlier latch at this boundary too."""
        rc.record_exec_rules_warning(WARNING)
        with patch.object(rc, 'exec_rules_enabled', return_value=False), \
                patch.object(self.supervisor, '_launch'), patch.object(self.supervisor, '_start_watch'):
            accepted, _ = self.supervisor.start()
        self.assertTrue(accepted)
        self.assertTrue(self.supervisor.enabled())


class ExecRulesDocumentationTests(unittest.TestCase):
    """Preserve the disclosures that define the managed prefix layer's limits."""

    def test_owner_allow_rule_sandbox_and_precedence_are_disclosed(self):
        """Keep the native allow decision's sandbox and precedence effects explicit."""
        text = (ROOT / 'DOCS.md').read_text()
        self.assertIn('A valid owner `allow` rule can let a matching command run outside the sandbox.', text)
        self.assertIn('An exact-executable allow rule can take precedence over a managed basename refusal.', text)
        self.assertIn('it does not scan valid owner rules for allow decisions.', text)

    def test_prefix_misses_are_disclosed(self):
        """Name the syntactic forms that a literal prefix policy cannot cover."""
        text = (ROOT / 'DOCS.md').read_text()
        paragraph = text.split('**Prefix limits.**', 1)[1].split('  - **', 1)[0]
        for phrase in ('Relative paths', 'Redirection', 'command substitution', 'nested shell scripts',
                       'codex exec --dangerously-bypass-approvals-and-sandbox',
                       '--sandbox=danger-full-access', '-s danger-full-access'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, paragraph)

    def test_rules_apply_with_remote_control_off(self):
        """Do not present the always-on requirements as a relay-only feature."""
        text = (ROOT / 'DOCS.md').read_text()
        paragraph = text.split('**Prefix limits.**', 1)[1].split('  - **', 1)[0]
        self.assertIn('These rules apply with remote control off too.', paragraph)


if __name__ == '__main__':
    unittest.main()
