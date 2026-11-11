"""CX-RC-START.f review regressions in disposable configuration only."""
import json
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gate_rules
import remote_control as rc
import test_rc_start as original_tests


class MoveReviewTests(unittest.TestCase):
    def test_move_is_compared_to_existing_or_absent_yaml_destination(self):
        for destination in ('configuration.yaml','packages/new.yaml'):
            for hunk in ('','@@\n-# before\n+# after\n','@@\n shell_command:\n'):
                with tempfile.TemporaryDirectory() as folder, self.subTest(destination=destination,hunk=hunk):
                    root=Path(folder);(root/'packages').mkdir()
                    (root/'notes.txt').write_text('# before\nshell_command:\n  example: echo fixture\n')
                    if destination=='configuration.yaml':(root/destination).write_text('light:\n  name: existing\n')
                    body='*** Begin Patch\n*** Update File: notes.txt\n*** Move to: '+destination+'\n'+hunk+'*** End Patch'
                    with patch.object(gate_rules,'CONFIG_ROOT',root), patch.object(gate_rules,'category_refusals_enabled',return_value=False):
                        reason=gate_rules.check_tool_call('PreToolUse',{'tool_name':'apply_patch','tool_input':{'command':body}},True,gate_rules.load_rules())
                    self.assertTrue(reason,'Moving a planted definition into YAML must be refused')

    def test_benign_move_still_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'notes.txt').write_text('name: fixture\n')
            with patch.object(gate_rules,'CONFIG_ROOT',root):
                self.assertFalse(gate_rules.check_tool_call('PreToolUse',{'tool_name':'apply_patch','tool_input':{'command':'*** Begin Patch\n*** Update File: notes.txt\n*** Move to: configuration.yaml\n*** End Patch'}},True,gate_rules.load_rules()))


class DowngradeReviewTests(unittest.TestCase):
    def test_real_pinned_cli_wording_names_each_downgraded_setting(self):
        for setting,value in [('approval_policy','OnRequest'),('permission_profile','Managed { file_system: Restricted, network: Restricted }')]:
            for as_json in (False,True):
                with tempfile.TemporaryDirectory() as folder,self.subTest(setting=setting,as_json=as_json):
                    sup=rc.Supervisor('codex',Path(folder)/'state',dict,lambda:'',gate=lambda:'')
                    message=f'Configured value for `{setting}` is disallowed by requirements; falling back to required value {value}. Details: set by /etc/codex/requirements.toml'
                    sup._parse(json.dumps({'type':'item.completed','item':{'type':'error','message':message}}) if as_json else message.upper())
                    snapshot=sup.snapshot()
                    self.assertIn(setting,snapshot['policy_warning'])
                    self.assertIn('downgraded',snapshot['policy_warning'])
                    self.assertTrue(any(setting in line and 'downgraded' in line for line in snapshot['log']))
                    self.assertNotEqual(snapshot['state'],'error','A policy warning is not a failed connection')


class RecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'config';self.root.mkdir()
        self.data=Path(self.temp.name)/'data';self.data.mkdir()
        self.path=self.root/'configuration.yaml';self.before='name: baseline\n'
        self.path.write_text(self.before)
        self.watch=rc.ConfigWatch(self.root,self.data/'baseline.json');self.watch.take_baseline()
        self.edited='name: owner edit\nshell_command:\n  example: echo fixture\n'

    def test_replaced_bytes_are_saved_and_reported_before_undo(self):
        self.path.write_text(self.edited)
        found=self.watch.new_findings(remote_on=True)
        self.assertEqual(self.path.read_text(),self.before)
        self.assertTrue(found[0].get('recovery_copy'),'Finding must identify the saved replaced version')
        copy=Path(found[0]['recovery_copy'])
        self.assertTrue(copy.is_relative_to(self.data/'remote_control_undo'))
        self.assertEqual(copy.read_text(),self.edited)
        self.assertEqual(copy.stat().st_mode & 0o777,0o600)
        again=rc.ConfigWatch(self.root,self.watch.baseline_path)
        self.assertEqual(again.new_findings()[0]['recovery_copy'],str(copy))

    def test_new_planted_file_is_saved_before_removal(self):
        new=self.root/'new.yaml';new.write_text(self.edited)
        found=self.watch.new_findings(remote_on=True)
        self.assertFalse(new.exists())
        self.assertTrue(found[0].get('recovery_copy'))
        self.assertEqual(Path(found[0]['recovery_copy']).read_text(),self.edited)

    def test_backup_creation_failure_leaves_the_edited_file_untouched(self):
        (self.data/'remote_control_undo').write_text('not a directory')
        self.path.write_text(self.edited)
        found=self.watch.new_findings(remote_on=True)
        self.assertEqual(self.path.read_text(),self.edited)
        self.assertFalse(found[0]['undone'])
        self.assertIn('safely',found[0]['undo_note'])

    def test_backup_directory_symlink_does_not_escape_private_storage(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir()
        (self.data/'remote_control_undo').symlink_to(outside,target_is_directory=True)
        self.path.write_text(self.edited)
        self.watch.new_findings(remote_on=True)
        self.assertEqual(self.path.read_text(),self.edited)
        self.assertEqual(list(outside.iterdir()),[])

    def test_ui_owned_files_are_reported_without_whole_file_undo(self):
        for name in ('automations.yaml','scripts.yaml','scenes.yaml'):
            with self.subTest(name=name):
                path=self.root/name;path.write_text('name: baseline\n');self.watch.take_baseline()
                path.write_text(self.edited)
                found=self.watch.new_findings(remote_on=True)
                report=next(f for f in found if f['file']==name)
                self.assertEqual(path.read_text(),self.edited)
                self.assertFalse(report['undone'])
                self.assertIn('Home Assistant',report['undo_note'])
                self.assertIn('without automatic undo',report['undo_note'])


class HookCapReviewTests(unittest.TestCase):
    def test_hook_refuses_a_source_over_512_kib_without_changing_watch_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'configuration.yaml'
            path.write_text('# '+'x'*(512*1024)+'\nname: before\n')
            body='*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-name: before\n+name: after\n*** End Patch'
            with patch.object(gate_rules,'CONFIG_ROOT',root):
                self.assertTrue(gate_rules.check_tool_call('PreToolUse',{'tool_name':'apply_patch','tool_input':{'command':body}},True,gate_rules.load_rules()))
            self.assertEqual(gate_rules.read_config_bytes(root,path.name),path.read_bytes())

    def test_watch_test_waits_for_report_publication_after_restore(self):
        real_undo=rc.ConfigWatch._undo
        def delayed_publication(watch,*args):
            result=real_undo(watch,*args)
            time.sleep(.1)  # The bytes are restored; the report has not reached Supervisor.findings yet.
            return result
        with patch.object(rc.ConfigWatch,'_undo',delayed_publication):
            case=original_tests.FurtherRegressions('test_always_on_undo_is_wired_only_while_supervisor_is_enabled')
            case.debug()
