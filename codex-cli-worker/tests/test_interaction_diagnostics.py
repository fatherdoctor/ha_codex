"""Synthetic golden export and bounded, anonymized rolling storage."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import interaction_diagnostics as diag


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'rolling.json';self.store=diag.RollingDiagnostics(self.path)

    def sample(self):
        return {'source':'addon_page','start':1700000000,'end':1700000010,'duration_seconds':10,
                'outcome':'done','addon_version':'0.1.81.2','model':'gpt-6.1-sol',
                'entities':['light.sample','light.sample'],'paths':['/config/scripts/private_canary.yaml'],
                'events':[{'kind':'tool','name':'commandExecution','result':'done','duration_seconds':1},
                          {'kind':'approval','decision':'denied'}, {'kind':'gate','rule':'credential'},
                          {'kind':'error','type':'ValueError','message':'PRIVATE_CANARY'},
                          {'kind':'ha_read','operation':'state','domain':'light'}]}

    def test_golden_export_drops_all_unstructured_canaries(self):
        record=self.sample();record.update(prompt='PROMPT_CANARY',answer='ANSWER_CANARY',
            token='TOKEN_CANARY',person='PERSON_CANARY',location='LOCATION_CANARY',
            host='HOST_CANARY',email='EMAIL_CANARY',phone='PHONE_CANARY',
            address='.'.join(map(str,(192,0,2,10))))
        self.store.append(record)
        with patch.object(diag.secrets,'token_bytes',return_value=b'x'*32):export=self.store.export()
        raw=json.dumps(export)
        for value in ('PROMPT_CANARY','ANSWER_CANARY','TOKEN_CANARY','PERSON_CANARY','LOCATION_CANARY',
                      'HOST_CANARY','EMAIL_CANARY','PHONE_CANARY','PRIVATE_CANARY','private_canary','light.sample',record['address']):
            self.assertNotIn(value,raw);self.assertNotIn(value,self.path.read_text())
        result=export['interactions'][0]
        self.assertEqual(result['source'],'addon_page');self.assertEqual(result['outcome'],'done')
        self.assertEqual(result['start'],'2023-11-14T22:13:20+00:00')
        self.assertEqual(result['end'],'2023-11-14T22:13:30+00:00')
        self.assertEqual(result['events'][-2]['message'],'Invalid operation.')
        self.assertEqual(result['paths'],[{'folder':'scripts','extension':'.yaml'}])
        self.assertEqual(result['entities'][0],result['entities'][1])
        self.assertEqual(result['events'][-1],{'kind':'ha_read','operation':'state','domain':'light','time':None})

    def test_export_rekeys_the_same_entity_between_downloads(self):
        self.store.append(self.sample())
        self.assertNotEqual(self.store.export()['interactions'][0]['entities'][0]['id'],self.store.export()['interactions'][0]['entities'][0]['id'])

    def test_last_ten_bounded_private_records_survive_restart(self):
        for i in range(14):self.store.append({**self.sample(),'duration_seconds':i})
        rows=diag.RollingDiagnostics(self.path).export()['interactions']
        self.assertEqual([x['duration_seconds'] for x in rows],list(range(4,14)))
        self.assertLessEqual(self.path.stat().st_size,diag.MAX_BYTES)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_event_caps_and_untrusted_identifiers_are_allowlisted(self):
        record=self.sample();record.update(source='SOURCE_CANARY',model='MODEL_CANARY',addon_version='VERSION_CANARY')
        record['events']=[{'kind':'tool','name':'TOOL_CANARY','result':'RESULT_CANARY'}]*1000
        self.store.append(record);out=self.store.export()['interactions'][0]
        self.assertEqual(len(out['events']),diag.MAX_EVENTS)
        self.assertNotIn('CANARY',json.dumps(out))
        self.assertEqual(out['events'][0]['name'],'other')

    def test_export_revalidates_tampered_disk_and_nonfinite_numbers(self):
        self.path.write_text(json.dumps([{'prompt':'PROMPT_CANARY','source':'SOURCE_CANARY','events':[{'kind':'error','type':'PERSON_CANARY','message':'PRIVATE_CANARY'}], 'start':float('nan')}]))
        out=self.store.export();self.assertNotIn('CANARY',json.dumps(out));self.assertIsNone(out['interactions'][0]['start'])

    def test_corrupt_oversized_and_symlink_storage_is_not_imported(self):
        self.path.write_text('not JSON');self.assertEqual(self.store.export()['interactions'],[])
        self.path.write_text('x'*(diag.MAX_BYTES+1));self.assertEqual(self.store.export()['interactions'],[])
        self.path.unlink();other=self.path.with_name('other');other.write_text('[]');self.path.symlink_to(other)
        self.assertEqual(self.store.export()['interactions'],[])
