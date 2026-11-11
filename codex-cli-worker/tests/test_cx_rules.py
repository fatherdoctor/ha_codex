"""Category preferences never disable the escape-prevention checks."""
import io
import json
import subprocess
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'gate'))
import gate_hook
import gate_rules
import remote_control
import server


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.options = self.root / 'options.json'
        option_patch = patch.object(gate_rules, 'OPTIONS_PATH', self.options, create=True)
        option_patch.start()
        self.addCleanup(option_patch.stop)
        self.rules = gate_rules.load_rules()

    def enable(self, value):
        self.options.write_text(json.dumps({'refuse_categories': value}))

    def hook(self, payload, remote=False):
        state = self.root / 'state.json'
        state.write_text(json.dumps({'enabled': remote}))
        stdout, stderr = io.StringIO(), io.StringIO()
        code = gate_hook.main(['--state', str(state)], stdin=io.StringIO(json.dumps(payload)), stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def edit(self, text):
        return {'hook_event_name': 'PreToolUse', 'tool_input': {'patch': '*** Begin Patch\n*** Add File: sample.yaml\n' + '\n'.join('+' + line for line in text.splitlines()) + '\n*** End Patch\n'}}

    def test_only_json_true_enables_categories(self):
        self.assertFalse(gate_rules.category_refusals_enabled())
        for value in [False, None, 1, 'true', [], {}]:
            with self.subTest(value=value):
                self.enable(value)
                self.assertFalse(gate_rules.category_refusals_enabled())
        self.enable(True)
        self.assertTrue(gate_rules.category_refusals_enabled())
        for text in ['invalid', '[]', 'null']:
            self.options.write_text(text)
            self.assertFalse(gate_rules.category_refusals_enabled())

    def test_gate_category_edits_are_opt_in(self):
        for domain in ['lock', 'alarm_control_panel', 'cover', 'camera', 'notify', 'tts']:
            for enabled in [False, True]:
                with self.subTest(domain=domain, enabled=enabled):
                    self.enable(enabled)
                    code, out, _ = self.hook(self.edit('- action: ' + domain + '.sample'))
                    self.assertEqual(code, 2 if enabled else 0)
                    if not enabled:
                        self.assertEqual(out, '')

    def test_planted_commands_and_templates_are_always_refused(self):
        for text in ['shell_command:\n  sample: echo fake', 'rest_command:\n  sample: {}', '  platform: rest', '- action: python_script.sample', '- action: hassio.sample', '- action: "{{ chosen }}"', '- action: light.turn_on\n  entity_id: "{{ chosen }}"']:
            for enabled in [False, True]:
                with self.subTest(text=text, enabled=enabled):
                    self.enable(enabled)
                    self.assertEqual(self.hook(self.edit(text))[0], 2)

    def test_credentials_escalation_and_permission_boundaries_stay_on(self):
        self.enable(False)
        for command in ['cat /proc/1/environ', 'echo $SUPERVISOR_TOKEN', 'cat secrets.yaml']:
            self.assertEqual(self.hook({'tool_input': {'command': command}})[0], 2)
        self.assertEqual(self.hook({'hook_event_name': 'PermissionRequest'}, remote=True)[0], 2)
        self.assertEqual(self.hook({'tool_input': {'sandbox_permissions': 'require_escalated'}}, remote=True)[0], 2)
        self.assertEqual(self.hook({'tool_input': {'command': 'ls'}})[0], 0)

    def test_indirect_device_categories_are_off(self):
        self.enable(False)
        scripts = gate_rules.refused_scripts({'scripts.yaml': 'sample:\n  sequence:\n    - action: lock.lock\n'}, self.rules)
        self.assertEqual(gate_rules.active_findings('- action: script.sample', self.rules, scripts), [])
        self.enable(True)
        self.assertTrue(gate_rules.active_findings('- action: script.sample', self.rules, {'sample'}))

    def test_watch_keeps_security_findings_with_categories_off(self):
        config = self.root / 'config'; config.mkdir()
        target = config / 'sample.yaml'; target.write_text('- action: light.turn_on\n')
        watch = remote_control.ConfigWatch(config, self.root / 'baseline.json')
        self.enable(False); watch.take_baseline()
        target.write_text('- action: lock.lock\n')
        self.assertEqual(watch.new_findings(), [])
        target.write_text('shell_command:\n  sample: echo fake\n')
        self.assertTrue(watch.new_findings())
        self.enable(True)
        target.write_text('- action: lock.lock\n')
        self.assertTrue(watch.new_findings())

    def test_added_security_file_is_removed_but_category_file_stays(self):
        config = self.root / 'config'; config.mkdir()
        run = self.root / 'run'; run.mkdir()
        self.enable(False)
        with patch.object(server, 'CONFIG_ROOT', config):
            target = config / 'sample.yaml'; target.write_text('shell_command:\n  sample: echo fake\n')
            results = server.refuse_planted_edits(run, {'added': ['sample.yaml']}, {})
            self.assertEqual(results[0]['undone'], 'removed')
            self.assertFalse(target.exists())
            target.write_text('- action: lock.lock\n')
            self.assertEqual(server.refuse_planted_edits(run, {'added': ['sample.yaml']}, {}), [])
            self.assertTrue(target.exists())

    def test_verified_copy_restores_new_security_call_in_both_modes(self):
        for enabled in [False, True]:
            with self.subTest(enabled=enabled):
                self.enable(enabled)
                config = self.root / str(enabled); config.mkdir()
                run = self.root / ('run' + str(enabled)); run.mkdir()
                target = config / 'sample.yaml'
                original = '- alias: original\n  actions:\n    - action: shell_command.sample\n'
                target.write_text(original)
                backup = run / server.BACKUP_DIR / 'sample.yaml'; backup.parent.mkdir(); backup.write_text(original)
                before = {'sample.yaml': {'sha256': server.file_hash(target)}}
                target.write_text(original.replace('shell_command.sample', 'shell_command.other'))
                with patch.object(server, 'CONFIG_ROOT', config):
                    result = server.refuse_planted_edits(run, {'changed': ['sample.yaml']}, before)
                self.assertEqual(result[0]['undone'], 'restored')
                self.assertEqual(target.read_text(), original)

    def test_unverified_backup_is_never_restored(self):
        config = self.root / 'config'; config.mkdir()
        run = self.root / 'run'; run.mkdir()
        target = config / 'sample.yaml'; target.write_text('- action: light.turn_on\n')
        before = {'sample.yaml': {'sha256': server.file_hash(target)}}
        target.write_text('shell_command:\n  sample: echo fake\n')
        backup = run / server.BACKUP_DIR / 'sample.yaml'; backup.parent.mkdir(); backup.write_text('unverified')
        with patch.object(server, 'CONFIG_ROOT', config):
            result = server.refuse_planted_edits(run, {'changed': ['sample.yaml']}, before)
        self.assertFalse(result[0]['undone'])
        self.assertNotEqual(target.read_text(), 'unverified')

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_merged_chat_script_parses(self):
        result = subprocess.run(['node', '--check', str(ROOT / 'web/assets/chat.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_option_and_shipped_rules_contract(self):
        import yaml
        config = yaml.safe_load((ROOT / 'config.yaml').read_text())
        self.assertIs(config['options']['refuse_categories'], False)
        self.assertIs(server.DEFAULT_OPTIONS['refuse_categories'], False)
        self.assertEqual(config['schema']['refuse_categories'], 'bool')
        rules = (ROOT / 'policy/house-rules.md').read_text().lower()
        for word in ['lock', 'alarm', 'cover', 'camera', 'publish', 'ignore']:
            self.assertIn(word, rules)


class ExchangeRulesTests(unittest.TestCase):
    def exercise(self, failure, *, cancelled=False, recoverable=True, baseline_missing=False):
        import test_approvals as fixture_module
        fixture = fixture_module.ApprovalRunTests('test_failed_turn_still_reviews_edits_and_recovery')
        fixture.setUp()
        worker = fixture_module.server
        try:
            config = fixture.root / 'config'; config.mkdir()
            target = config / 'sample.yaml'
            original = '- action: light.turn_on\n'
            target.write_text(original)
            fixture.options['full_snapshot'] = recoverable
            fixture.stack.enter_context(patch.object(worker, 'CONFIG_ROOT', config))
            fixture.stack.enter_context(patch.object(worker, 'build_manifest', fixture.real_build_manifest))
            fixture.stack.enter_context(patch.object(gate_rules, 'OPTIONS_PATH', fixture.root / 'missing-options.json'))
            def edit():
                target.write_text('shell_command:\n  sample: echo fake\n')
                if baseline_missing:
                    (worker.get_run_dir('task') / worker.BASELINE_FILE).unlink()
            proc = fixture.process(failure=failure, on_turn=edit)
            if cancelled:
                original_wait = proc.wait
                def wait(timeout=None):
                    result = original_wait(timeout)
                    worker.tasks['task'].update(cancellation_requested=True, status='cancelled')
                    return result
                proc.wait = wait
            fixture.stack.enter_context(patch.object(worker.subprocess, 'Popen', return_value=proc))
            worker.run_task('task', 'Edit the sample configuration')
            if recoverable and not baseline_missing:
                self.assertEqual(target.read_text(), original)
            else:
                self.assertIn('shell_command', target.read_text())
                self.assertIn('sample.yaml', worker.tasks['task']['unchecked_edits'])
            self.assertEqual(worker.tasks['task']['status'], 'cancelled' if cancelled else 'failed')
        finally:
            fixture.doCleanups()

    def test_failed_or_disconnected_exchange_still_undoes_planted_commands(self):
        for failure in ['failed', 'eof', 'no-final']:
            with self.subTest(failure=failure):
                self.exercise(failure)

    def test_cancelled_exchange_still_undoes_planted_commands(self):
        self.exercise('failed', cancelled=True)

    def test_cancelled_exchange_without_a_copy_records_manual_review(self):
        self.exercise('failed', cancelled=True, recoverable=False)

    def test_unknown_baseline_never_deletes_an_existing_file(self):
        self.exercise('failed', baseline_missing=True)
