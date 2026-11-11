"""Remote hooks refuse named escape paths, otherwise preserve native approval."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'gate'))
import gate_hook
import gate_rules as rules
import remote_control as rc


class AskHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)/'config'; self.config.mkdir()
        self.state = Path(self.tmp.name)/'state.json'; self.state.write_text('{"enabled":true}')
        self.options = Path(self.tmp.name)/'options.json'; self.options.write_text('{"refuse_categories":true}')
        for key, value in [('CONFIG_ROOT', self.config), ('OPTIONS_PATH', self.options)]:
            p = patch.object(rules, key, value); p.start(); self.addCleanup(p.stop)

    def hook(self, command, event='PermissionRequest', **fields):
        payload = {'hook_event_name': event, 'tool_name':'Bash', 'cwd': str(self.config),
                   'tool_input': {'command':command, **fields}}
        out, err = io.StringIO(), io.StringIO()
        code = gate_hook.main(['--state',str(self.state)], stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_normal_actions_have_no_hook_decision_and_reach_native_ask(self):
        actions = [
            ('curl -X POST "$HA_URL/api/services/light/turn_on"', 'Turn on the sample light in the study; the room becomes lit.'),
            ('curl https://example.invalid/status', 'Read the public status page; no home state changes.'),
            ('curl -X POST "$HA_URL/api/services/lock/unlock"', 'Unlock the sample lock at the test entry; the entry becomes unlocked.'),
        ]
        for command, description in actions:
            for event in ('PreToolUse','PermissionRequest'):
                with self.subTest(command=command,event=event):
                    code,out,err=self.hook(command,event,description=description,sandbox_permissions='require_escalated')
                    self.assertEqual((code,out,err),(0,'',''))  # Never return an "allow" decision.

    def test_lock_file_edit_does_not_use_legacy_category_option(self):
        text='*** Begin Patch\n*** Add File: sample.yaml\n+- action: lock.unlock\n+  target:\n+    entity_id: lock.sample\n*** End Patch'
        self.assertEqual(self.hook(text),(0,'',''))

    def test_ordinary_edit_is_not_auto_approved(self):
        text='*** Begin Patch\n*** Add File: sample.yaml\n+input_boolean:\n+  sample:\n+    name: Sample\n*** End Patch'
        self.assertEqual(self.hook(text),(0,'',''))

    def test_named_security_restrictions_survive_permission_request(self):
        cases=[('cat /proc/3/environ','process environment'),('echo $SUPERVISOR_TOKEN','Supervisor token'),
               ('codex --sandbox danger-full-access','full access'),
               ('codex --dangerously-bypass-approvals-and-sandbox','full access'),
               ('echo x > /data/options.json','settings'),
               ('echo x > /usr/share/ha_codex/policy.json','policy'),
               ('echo x > /data/codex-home/AGENTS.md','rules')]
        for command, expected in cases:
            with self.subTest(command=command):
                code,out,err=self.hook(command)
                self.assertEqual(code,2);self.assertIn(expected,err)
                self.assertEqual(json.loads(out)['hookSpecificOutput']['decision']['behavior'],'deny')

    def test_literal_shell_definition_writer_is_refused(self):
        code,out,err=self.hook("printf 'shell_command:\\n  sample: value' > /config/sample.yaml")
        self.assertEqual(code,2);self.assertIn('shell_command',err)

    def test_full_access_mode_in_payload_is_refused(self):
        code,out,err=self.hook('ls /config',sandbox='danger-full-access')
        self.assertEqual(code,2);self.assertIn('full access',err)

    def test_policy_patch_is_refused_before_complete_file_read(self):
        for path, expected in [('/data/options.json','settings'),('/usr/share/ha_codex/policy.json','policy'),('/data/codex-home/AGENTS.md','rules')]:
            with self.subTest(path=path):
                text=f'*** Begin Patch\n*** Add File: {path}\n+sample\n*** End Patch'
                code,out,err=self.hook(text)
                self.assertEqual(code,2);self.assertIn(expected,err)

    def test_definition_additions_are_refused_by_name(self):
        for key in ('shell_command','rest_command','command_line','python_script','pyscript','template'):
            with self.subTest(key=key):
                text=f'*** Begin Patch\n*** Add File: sample.yaml\n+{key}:\n+  sample: value\n*** End Patch'
                code,out,err=self.hook(text)
                self.assertEqual(code,2);self.assertIn(key,err)
                inline=f'*** Begin Patch\n*** Add File: sample.yaml\n+{key}: {{sample: value}}\n*** End Patch'
                code,out,err=self.hook(inline)
                self.assertEqual(code,2);self.assertIn(key,err)

    def test_definition_body_edit_checks_complete_file(self):
        p=self.config/'sample.yaml';p.write_text('shell_command:\n  sample: old\n')
        text='*** Begin Patch\n*** Update File: sample.yaml\n@@\n-  sample: old\n+  sample: changed\n*** End Patch'
        code,out,err=self.hook(text)
        self.assertEqual(code,2);self.assertIn('shell_command',err)

    def test_python_definition_file_and_move_are_refused(self):
        for text in ('*** Begin Patch\n*** Add File: python_scripts/sample.py\n+print(1)\n*** End Patch',
                     '*** Begin Patch\n*** Add File: pyscript/sample.py\n+print(1)\n*** End Patch',
                     '*** Begin Patch\n*** Update File: sample.py\n*** Move to: python_scripts/sample.py\n@@\n-old\n+new\n*** End Patch'):
            with self.subTest(text=text):
                code,out,err=self.hook(text)
                self.assertEqual(code,2);self.assertRegex(err,'python_script|pyscript')

    def test_startup_self_test_expects_normal_ask_and_security_deny(self):
        permission = [case for case in rc.SELF_TEST_CASES if case[0]=='PermissionRequest' and case[1]]
        self.assertTrue(any(not case[3] for case in permission))
        self.assertTrue(any(case[3] for case in permission))
