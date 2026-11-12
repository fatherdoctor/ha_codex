"""Parsed YAML spellings and include dependencies keep the same command boundary."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'gate'))
import gate_rules as rules
import remote_control as rc
import gate_hook
import test_remote_control as base
import json,io

SPELLINGS=[
    'shell_command: {sample: echo sample}\n',
    '"shell_command":\n  sample: echo sample\n',
    '- {action: shell_command.sample}\n',
    '- {service: shell_command.sample}\n',
    '- action: script.turn_on\n  data: {entity_id: script.sample}\n',
]


class YamlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'config';self.root.mkdir()
        self.policy=rules.load_rules();self.server=base.server
        for obj,key in [(rules,'CONFIG_ROOT'),(self.server,'CONFIG_ROOT')]:
            p=patch.object(obj,key,self.root);p.start();self.addCleanup(p.stop)

    def write(self,name,text):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);return p

    def hook(self,name,before,after):
        self.write(name,before)
        body='*** Begin Patch\n*** Update File: '+name+'\n@@\n'+''.join('-'+l+'\n' for l in before.splitlines())+''.join('+'+l+'\n' for l in after.splitlines())+'*** End Patch\n'
        return rules.check_tool_call('PreToolUse',{'cwd':str(self.root),'tool_input':{'command':body}},True,self.policy)

    def exchange(self,name,before,after):
        target=self.write(name,after);run=Path(self.tmp.name)/'run';run.mkdir(exist_ok=True)
        copy=run/'earlier';copy.write_text(before)
        manifest={name:{'sha256':hashlib.sha256(before.encode()).hexdigest()}}
        with patch.object(self.server,'previous_versions',return_value=[{'path':name,'status':'saved','copy':str(copy)}]):
            result=self.server.refuse_planted_edits(run,{'changed':[name]},manifest)
        return result,target.read_text()

    def test_flow_quoted_action_and_script_spellings_in_all_three_checks(self):
        for i,text in enumerate(SPELLINGS):
            with self.subTest(spelling=i):
                self.assertTrue(rules.find_refused(text,self.policy,{'sample'}))
                self.write('scripts.yaml','sample:\n  sequence:\n    - action: shell_command.sample\n')
                name='case'+str(i)+'.yaml';before='input_boolean: {}\n';p=self.write(name,before)
                watch=rc.ConfigWatch(self.root,Path(self.tmp.name)/('base'+str(i)));watch.take_baseline();p.write_text(text)
                self.assertTrue(any(f['file']==name for f in watch.new_findings()))
                self.assertTrue(self.hook(name,before,text))
                result,current=self.exchange(name,before,text);self.assertTrue(result);self.assertEqual(current,before)

    def test_flow_definition_body_changes_are_hashed(self):
        before='"shell_command": {sample: echo before}\n';after=before.replace('before','after')
        self.assertTrue(rules.new_refused(before,after,self.policy))
        self.assertTrue(self.hook('configuration.yaml',before,after))

    def test_include_body_is_refused_restored_and_watched_whole(self):
        self.write('configuration.yaml','shell_command: !include commands.yaml\n')
        before='sample: echo before\nother: echo same\n';after=before.replace('before','after')
        p=self.write('commands.yaml',before);watch=rc.ConfigWatch(self.root,Path(self.tmp.name)/'baseline');watch.take_baseline();p.write_text(after)
        found=watch.new_findings(remote_on=True);self.assertTrue(found);self.assertEqual(p.read_text(),before)
        self.assertTrue(any(f.get('undone')=='restored' for f in found))
        self.assertTrue(self.hook('commands.yaml',before,after))
        result,current=self.exchange('commands.yaml',before,after);self.assertTrue(result);self.assertEqual(current,before)

    def test_include_dir_named_per_file_script_and_transitive_flow_reference(self):
        self.write('configuration.yaml','script: !include_dir_named scripts/\n')
        self.write('scripts/sample.yaml','alias: Sample\nsequence: [{action: shell_command.sample}]\n')
        self.write('scripts/next.yaml','sequence: [{action: script.sample}]\n')
        names=rules.config_refused_scripts(self.policy)
        self.assertIn('sample',names);self.assertIn('next',names);self.assertNotIn('sequence',names)
        self.assertTrue(self.hook('automations.yaml','[]\n','- {action: script.next}\n'))

    def test_include_definition_directory_hashes_each_member(self):
        self.write('configuration.yaml','shell_command: !include_dir_merge_named commands/\n')
        before='sample: echo before\n';after=before.replace('before','after')
        p=self.write('commands/a.yaml',before);watch=rc.ConfigWatch(self.root,Path(self.tmp.name)/'baseline');watch.take_baseline();p.write_text(after)
        self.assertTrue(watch.new_findings());self.assertTrue(self.hook('commands/a.yaml',before,after))

    def test_include_graph_is_bounded_and_does_not_read_secrets_or_links(self):
        self.write('configuration.yaml','shell_command: !include secrets.yaml\nrest_command: !include linked.yaml\n')
        outside=Path(self.tmp.name)/'outside.yaml';outside.write_text('sample: hidden\n')
        (self.root/'linked.yaml').symlink_to(outside)
        seen=[];read=rules.read_config_bytes
        def tracking(root,rel,*args):
            seen.append(rel)
            if rel=='secrets.yaml':raise AssertionError('secret read')
            return read(root,rel,*args)
        with patch.object(rules,'read_config_bytes',side_effect=tracking):
            self.assertTrue(self.hook('ordinary.yaml','input_boolean: {}\n','input_boolean: {sample: {}}\n'))
        self.assertNotIn('secrets.yaml',seen);self.assertEqual(outside.read_text(),'sample: hidden\n')

    def test_tags_anchors_aliases_and_ordinary_target_have_safe_semantics(self):
        self.assertTrue(rules.find_refused('commands: &x {sample: echo sample}\n"shell_command": *x\n',self.policy))
        self.assertTrue(rules.find_refused('base: &x {shell_command: {sample: echo sample}}\npackage: {<<: *x}\n',self.policy))
        self.assertFalse(rules.find_refused('value: !secret example\nlight: !include ordinary.yaml\n',self.policy))
        self.assertFalse(rules.find_refused('- action: light.turn_on\n  target: {entity_id: light.sample}\n',self.policy))
        # A cyclic YAML alias must finish, never recursively expand forever.
        self.assertFalse(rules.find_refused('sample: &loop {next: *loop}\n',self.policy))

    def test_configured_script_directory_and_inline_scripts_resolve(self):
        self.write('configuration.yaml','script: !include_dir_named actions/\n')
        self.write('actions/sample.yaml','sequence: [{action: shell_command.sample}]\n')
        self.assertIn('sample',rules.config_refused_scripts(self.policy))
        self.write('configuration.yaml','script: {sample: {sequence: [{action: shell_command.sample}]}}\n')
        self.assertIn('sample',rules.config_refused_scripts(self.policy))

    def test_non_yaml_include_suffix_is_still_a_whole_definition(self):
        self.write('configuration.yaml','shell_command: !include commands.inc\n')
        before='sample: echo before\n';after=before.replace('before','after')
        p=self.write('commands.inc',before);watch=rc.ConfigWatch(self.root,Path(self.tmp.name)/'baseline');watch.take_baseline();p.write_text(after)
        self.assertTrue(watch.new_findings(remote_on=True));self.assertEqual(p.read_text(),before)
        self.assertTrue(self.hook('commands.inc',before,after))
        result,current=self.exchange('commands.inc',before,after);self.assertTrue(result);self.assertEqual(current,before)
