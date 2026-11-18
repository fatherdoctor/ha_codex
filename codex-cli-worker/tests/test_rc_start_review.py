"""CX-RC-START.f review regressions in disposable configuration only."""
import json
from pathlib import Path
import tempfile
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import remote_control as rc


class DowngradeReviewTests(unittest.TestCase):
    def test_real_pinned_cli_wording_names_each_downgraded_setting(self):
        for setting,value in [('approval_policy','OnRequest'),('permission_profile','Managed { file_system: Restricted, network: Restricted }')]:
            for as_json in (False,True):
                with tempfile.TemporaryDirectory() as folder,self.subTest(setting=setting,as_json=as_json):
                    sup=rc.Supervisor('codex',Path(folder)/'state',dict,lambda:'')
                    message=f'Configured value for `{setting}` is disallowed by requirements; falling back to required value {value}. Details: set by /etc/codex/requirements.toml'
                    sup._parse(json.dumps({'type':'item.completed','item':{'type':'error','message':message}}) if as_json else message.upper())
                    snapshot=sup.snapshot()
                    self.assertIn(setting,snapshot['policy_warning'])
                    self.assertIn('downgraded',snapshot['policy_warning'])
                    self.assertTrue(any(setting in line and 'downgraded' in line for line in snapshot['log']))
                    self.assertNotEqual(snapshot['state'],'error','A policy warning is not a failed connection')


