"""Owner-phone argument shapes and safe refusal text across the actual bridge."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import test_core_address as fixtures
import relay_reads_mcp as mcp


class RequestShapeTests(unittest.TestCase):
    setUp=fixtures.RelayReadTests.setUp
    call=fixtures.RelayReadTests.call

    @contextmanager
    def transport(self):
        if os.environ.get('PYTEST_CURRENT_TEST'):
            import pytest_socket
            pytest_socket.socket_allow_hosts([],allow_unix_socket=True)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'relay.sock';service=self.broker.serve(path)
            try:
                with patch.dict(os.environ,{'HA_RELAY_SOCKET':str(path),'HA_RELAY_CAPABILITY':self.cap}):yield
            finally:service.shutdown();service.server_close()

    def test_states_with_all_empty_or_null_optional_fields(self):
        for extra in ({'entity_id':'','attributes':[],'domain':'','offset':None,'limit':None},
                      {'entity_id':None,'attributes':None,'domain':None,'offset':None,'limit':None},
                      {'entity_id':'  ','attributes':[],'domain':'  '}):
            with self.subTest(extra=extra):
                result=self.call(operation='states',**extra)
                self.assertEqual(result['entities'][0]['entity_id'],'light.sample')

    def test_missing_and_null_paging_use_zero_and_ten(self):
        self.fetch.return_value=[{'entity_id':f'light.sample_{i:02d}','state':'on'} for i in range(25)]
        for values in ({},{'offset':None,'limit':None}):
            with self.subTest(values=values):
                result=self.call(operation='states',**values)
                self.assertEqual(len(result['entities']),10);self.assertEqual(result['next_offset'],10)
                self.assertEqual(result['entities'][0]['entity_id'],'light.sample_00')

    def test_state_ignores_empty_list_only_fields_and_null_attributes(self):
        self.fetch.return_value={'entity_id':'light.sample','state':'on','attributes':{'brightness':42}}
        result=self.call(operation='state',entity_id='light.sample',attributes=None,domain='',offset=None,limit=None)
        self.assertTrue(result['ok']);self.assertEqual(result['attributes'],{})

    def test_limit_above_fifty_is_clamped_with_note_and_correct_page(self):
        self.fetch.return_value=[{'entity_id':f'light.sample_{i:02d}','state':'on'} for i in range(75)]
        for limit in (51,100,10**100):
            with self.subTest(limit=limit):
                result=self.call(operation='states',limit=limit)
                self.assertEqual(len(result['entities']),50);self.assertEqual(result['next_offset'],50)
                self.assertIn('50',' '.join(result['notes']))

    def test_nonempty_wrong_operation_fields_remain_refused(self):
        cases=({'operation':'states','entity_id':'light.sample'}, {'operation':'states','attributes':['brightness']},
               {'operation':'state','entity_id':'light.sample','domain':'light'},
               {'operation':'state','entity_id':'light.sample','offset':0},
               {'operation':'state','entity_id':'light.sample','limit':10})
        for args in cases:
            with self.subTest(args=args),self.assertRaises(ValueError):self.call(**args)
        self.fetch.assert_not_called()

    def test_unknown_empty_fields_are_still_refused(self):
        with self.assertRaises(ValueError):self.call(operation='states',approval_mode=None)
        self.fetch.assert_not_called()

    def test_invalid_non_null_paging_is_not_coerced(self):
        for values in ({'offset':-1},{'offset':True},{'offset':100001},{'limit':0},{'limit':True},{'limit':1.5},{'limit':'100'}):
            with self.subTest(values=values),self.assertRaises(ValueError):self.call(operation='states',**values)
        self.fetch.assert_not_called()

    def test_specific_safe_refusal_reaches_native_client(self):
        with self.transport():result=mcp.read({'operation':'states','domain':'INVALID'})
        self.assertFalse(result['ok']);self.assertEqual(result['error'],'Invalid domain.')
        self.fetch.assert_not_called()

    def test_inactive_capability_reason_never_echoes_supplied_value(self):
        with self.transport(),patch.dict(os.environ,{'HA_RELAY_CAPABILITY':'synthetic-wrong-capability'}):
            result=mcp.read({'operation':'states'})
        self.assertEqual(result['error'],'Inactive relay capability.')
        self.assertNotIn('synthetic-wrong',json.dumps(result));self.fetch.assert_not_called()

    def test_backend_value_error_text_is_never_forwarded(self):
        self.fetch.side_effect=ValueError('synthetic-private-backend-detail')
        with self.transport():result=mcp.read({'operation':'states'})
        self.assertFalse(result['ok']);self.assertNotIn('synthetic-private',json.dumps(result))
        self.assertIn('unavailable',result['error'].lower())

    def test_unexpected_backend_error_stays_generic(self):
        self.fetch.side_effect=RuntimeError('synthetic-private-backend-detail')
        with self.transport():result=mcp.read({'operation':'states'})
        self.assertFalse(result['ok']);self.assertNotIn('synthetic-private',json.dumps(result))

    def test_client_validation_reason_reaches_model(self):
        result=mcp.respond({'method':'tools/call','params':{'name':'read','arguments':{'operation':'states','unknown':None}}})
        self.assertTrue(result['isError']);self.assertEqual(json.loads(result['content'][0]['text'])['error'],'Unsupported read arguments.')

    def test_schema_is_operation_specific_nullable_and_describes_clamping(self):
        props=mcp.TOOL['inputSchema']['properties']
        for field in ('entity_id','attributes','domain','offset','limit'):
            with self.subTest(field=field):
                self.assertIn('null',props[field]['type']);self.assertTrue(props[field].get('description'))
        self.assertNotIn('maximum',props['limit']);self.assertIn('50',props['limit']['description'])
        self.assertIn('states',props['domain']['description']);self.assertIn('state',props['entity_id']['description'])

    def test_model_filled_fields_and_clamp_roundtrip_through_tool_response(self):
        arguments={'operation':'states','domain':'light','entity_id':None,'attributes':[],'offset':None,'limit':100}
        with self.transport():result=mcp.respond({'method':'tools/call','params':{'name':'read','arguments':arguments}})
        self.assertFalse(result['isError']);value=json.loads(result['content'][0]['text'])
        self.assertTrue(value['ok']);self.assertTrue(value['notes'])


    def test_known_client_response_error_reaches_model(self):
        client=MagicMock();client.__enter__.return_value=client
        client.makefile.return_value.__enter__.return_value.readline.return_value=b''
        with patch.object(mcp.socket,'socket',return_value=client):
            result=mcp.respond({'method':'tools/call','params':{'name':'read','arguments':{'operation':'states'}}})
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text'])['error'],'Read response is unavailable or oversized.')
