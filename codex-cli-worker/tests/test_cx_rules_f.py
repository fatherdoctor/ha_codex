"""Regressions for the second-model review of CX-RULES PR 19."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gate_rules
import remote_control
import server


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.run = self.root / 'run'
        self.run.mkdir()
        self.rules = gate_rules.load_rules()
        for obj, name, value in [(server, 'CONFIG_ROOT', self.config),
                                 (gate_rules, 'OPTIONS_PATH', self.root / 'options.json')]:
            mock = patch.object(obj, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_f1_changed_file_without_verified_copy_is_unchecked(self):
        target = self.config / 'sample.yaml'
        original = 'shell_command:\n  sample: echo fake\n'
        target.write_text(original)
        before = {'sample.yaml': {'sha256': server.file_hash(target)}}
        target.write_text(original + 'group:\n  sample: {}\n')
        result = server.refuse_planted_edits(self.run, {'changed': ['sample.yaml']}, before)
        self.assertEqual(result, [{'path': 'sample.yaml', 'checked': False, 'undone': '', 'findings': []}])
        self.assertIn('group:', target.read_text())

    def test_f1_unknown_manifest_does_not_invent_new_calls(self):
        target = self.config / 'sample.yaml'
        target.write_text('shell_command:\n  sample: echo fake\n')
        result = server.refuse_planted_edits(self.run, {'changed': ['sample.yaml']}, {})
        self.assertFalse(result[0]['checked'])
        self.assertEqual(result[0]['findings'], [])
        self.assertTrue(target.exists())

    def test_f2_repair_translation_preserves_both_issues_and_placeholders(self):
        data = json.loads((ROOT.parent / 'custom_components/codex_cli/translations/en.json').read_text())
        self.assertIn('codex_not_signed_in', data['issues'])
        repair = data['issues']['remote_control_config_change']
        self.assertTrue(repair['title'])
        for name in ('count', 'where', 'what', 'text'):
            self.assertIn('{' + name + '}', repair['description'])

    def script_text(self):
        return ('run_shell:\n  sequence:\n    - action: shell_command.sample\n'
                'run_nested:\n  sequence:\n    - action: script.run_shell\n'
                'run_device:\n  sequence:\n    - action: lock.lock\n')

    def test_f4_watch_resolves_only_security_scripts_when_categories_off(self):
        (self.config / 'scripts.yaml').write_text(self.script_text())
        target = self.config / 'sample.yaml'
        target.write_text('- action: light.turn_on\n')
        watch = remote_control.ConfigWatch(self.config, self.root / 'baseline.json')
        watch.take_baseline()
        target.write_text('- action: script.run_device\n')
        self.assertEqual(watch.new_findings(), [])
        for text in ('- action: script.run_shell\n', '- action: script.run_nested\n', '- action: script.turn_on\n  target:\n    entity_id: script.run_nested\n'):
            target.write_text(text)
            with self.subTest(text=text):
                self.assertTrue(watch.new_findings())

    def test_f4_exchange_rejects_new_indirect_security_call(self):
        (self.config / 'scripts.yaml').write_text(self.script_text())
        target = self.config / 'sample.yaml'
        target.write_text('- action: script.run_nested\n')
        result = server.refuse_planted_edits(self.run, {'added': ['sample.yaml']}, {})
        self.assertEqual(result[0]['undone'], 'removed')

    def test_f4_hook_checks_security_script_calls_without_categories(self):
        (self.config / 'scripts.yaml').write_text(self.script_text())
        patch_text = '*** Begin Patch\n*** Add File: sample.yaml\n+- action: script.run_nested\n*** End Patch\n'
        with patch.object(gate_rules, 'CONFIG_ROOT', self.config, create=True):
            reason = gate_rules.check_tool_call('PreToolUse', {'tool_input': {'patch': patch_text}}, False, self.rules)
        self.assertIn('script.run_nested', reason)

    def test_f5_changed_trigger_with_same_call_is_not_new(self):
        before = '- alias: sample\n  trigger: old\n  action: shell_command.sample\n'
        after = before.replace('old', 'new')
        self.assertEqual(gate_rules.new_refused(before, after, self.rules, categories=False), [])
        self.assertTrue(gate_rules.new_refused(before, after, self.rules, categories=True))
        for text in (after.replace('shell_command.sample', 'shell_command.other'), after + 'rest_command:\n  sample: {}\n', after + '- action: \"{{ selected }}\"\n'):
            with self.subTest(text=text):
                self.assertTrue(gate_rules.new_refused(before, text, self.rules, categories=False))

    def test_f5_watch_does_not_flag_changed_trigger_with_same_call(self):
        target = self.config / 'sample.yaml'
        before = '- alias: sample\n  trigger: old\n  action: shell_command.sample\n'
        target.write_text(before)
        watch = remote_control.ConfigWatch(self.config, self.root / 'baseline.json')
        watch.take_baseline()
        target.write_text(before.replace('old', 'new'))
        self.assertEqual(watch.new_findings(), [])

    def test_n4_refused_blocks_default_honors_option(self):
        self.assertEqual(gate_rules.refused_blocks('- action: lock.lock', self.rules), {})


class CompletedExchangeTests(unittest.TestCase):
    def exercise(self, *, missing_manifest=False, forged_copy=False, verified_copy=False):
        import test_approvals as fixtures
        fixture = fixtures.ApprovalRunTests('test_running_task_can_be_approved_then_finishes_and_reaps')
        fixture.setUp()
        worker = fixtures.server
        runner = None
        try:
            config = fixture.root / 'config'
            config.mkdir()
            target = config / 'sample.yaml'
            original = '- alias: sample\n  trigger: old\n  action: shell_command.sample\n'
            target.write_text(original)
            fixture.options['full_snapshot'] = False
            fixture.stack.enter_context(patch.object(worker, 'CONFIG_ROOT', config))
            fixture.stack.enter_context(patch.object(worker, 'build_manifest', fixture.real_build_manifest))
            fixture.stack.enter_context(patch.object(gate_rules, 'OPTIONS_PATH', fixture.root / 'absent.json'))
            def edit():
                target.write_text(original.replace('old', 'new'))
                run = worker.get_run_dir('task')
                if verified_copy or forged_copy:
                    backup = run / worker.BACKUP_DIR / 'sample.yaml'
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_text(original if verified_copy else 'forged bytes')
                if missing_manifest:
                    (run / worker.BASELINE_FILE).unlink()
            proc = fixture.process(on_turn=edit)
            fixture.stack.enter_context(patch.object(worker.subprocess, 'Popen', return_value=proc))
            runner = threading.Thread(target=worker.run_task, args=('task', 'Edit the sample trigger'))
            runner.start()
            self.assertTrue(fixture.published.wait(5))
            record = worker.tasks['task']['approvals'][0]
            response = worker.app.test_client().post('/tasks/task/approvals/' + record['approval_id'],
                json={'turn_id': 'worker-turn', 'decision': 'allow'}, **fixtures.ingress_as())
            self.assertEqual(response.status_code, 200)
            runner.join(5)
            self.assertFalse(runner.is_alive())
            task = worker.tasks['task']
            self.assertEqual(task['approvals'][0]['status'], 'allowed')
            self.assertEqual(task['status'], 'completed')
            self.assertEqual(task['error'], '')
            self.assertEqual(target.read_text(), original.replace('old', 'new'))
            if not verified_copy:
                self.assertEqual(task['unchecked_edits'], ['sample.yaml'])
                self.assertIn('could not check', task['details'])
                self.assertEqual(task['refused_edits'], [])
        finally:
            if runner is not None and runner.is_alive():
                worker.tasks['task']['cancellation_requested'] = True
                runner.join(5)
            fixture.doCleanups()

    def test_f1_completed_edit_without_copy_is_reported_without_failure(self):
        self.exercise()

    def test_f1_missing_manifest_is_reported_without_failure(self):
        self.exercise(missing_manifest=True)

    def test_f1_forged_copy_is_never_used_or_marked_as_new_actions(self):
        self.exercise(forged_copy=True)

    def test_f5_verified_unchanged_call_keeps_normal_approval(self):
        self.exercise(verified_copy=True)
