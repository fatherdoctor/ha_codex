"""CX-HA-URL.f: manifest, transport, attribution, privacy and relay locking."""
import io
import os
import hashlib
import threading
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import yaml
from test_server import server
import core_address
import relay_reads
import relay_reads_mcp
import remote_control

ROOT=Path(__file__).resolve().parents[1]


def response(data):
    value=Mock();value.status_code=200;value.iter_content.return_value=[data]
    value.__enter__=Mock(return_value=value);value.__exit__=Mock(return_value=False)
    client=Mock();client.get.return_value=value
    session=Mock();session.__enter__=Mock(return_value=client);session.__exit__=Mock(return_value=False)
    return value,client,session


class ReviewTests(unittest.TestCase):
    def test_f1_manifest_grants_only_default_supervisor_role(self):
        config=yaml.safe_load((ROOT/'config.yaml').read_text())
        self.assertIs(config.get('hassio_api'),True);self.assertNotIn('hassio_role',config)

    def test_f1_failed_supervisor_tries_fixed_core_ports_without_token(self):
        address=core_address.CoreAddress();probe=Mock(side_effect=[False,True])
        with patch.object(server,'core_address',address),patch.object(server,'ha_token',return_value='synthetic'), \
             patch.object(core_address,'get_json',side_effect=ValueError('synthetic 403')), \
             patch.object(server,'probe_core',probe,create=True),patch.object(server.time,'sleep'):
            value=server.refresh_core_address({})
        self.assertEqual(value['url'],'http://homeassistant:80')
        self.assertEqual([c.args for c in probe.call_args_list],[(8123,),(80,)])

    def test_f2_three_megabyte_state_listing_stays_bounded_at_output(self):
        raw=json.dumps([{'entity_id':'light.sample','state':'off','attributes':{'unselected':'x'*(3*1024*1024)}}]).encode()
        _,_,session=response(raw)
        broker=relay_reads.RelayReads(lambda:(types.SimpleNamespace(poll=lambda:None),False),lambda:'synthetic',lambda s:s)
        cap=broker.environment({})['HA_RELAY_CAPABILITY']
        with patch.object(core_address.requests,'Session',return_value=session):
            result=broker.dispatch({'capability':cap,'arguments':{'operation':'states','domain':'light'}})
        self.assertEqual(result['total'],1);self.assertLess(len(json.dumps(result)),65536)

    def test_f3_upstream_history_and_option_label_are_preserved(self):
        docs=(ROOT/'DOCS.md').read_text()
        self.assertIn('From **0.1.68**, a task that has `HA_TOKEN` also gets `HA_URL`,',docs)
        self.assertIn('From **0.1.81.2**',docs)
        paragraph=next(x for x in docs.split('\n\n') if x.startswith('From **0.1.68**'))
        self.assertEqual(hashlib.sha256((paragraph + '\n\n' + next(
            x for x in docs.split('\n\n') if x.startswith('From **0.1.81.2**')
        )).encode()).hexdigest(), 'a6806005f34bc7047dd7e3de83c8cab174483221c814554a074120183f55dcc5')
        description=yaml.safe_load((ROOT/'translations/en.yaml').read_text())['configuration']['ha_url']['description']
        self.assertEqual(hashlib.sha256(description[:462].encode()).hexdigest(),'00117f07856b0c78a57559b19be64cff20924a20f3db6e98bedb85481bc501d9')
        self.assertEqual(yaml.safe_load((ROOT/'translations/en.yaml').read_text())['configuration']['ha_url']['name'],'Home Assistant API URL')

    def test_f4_credential_substrings_and_password_mode_are_never_returned(self):
        for suffix in ('alarm_passcode','garage_codes','door_pincode','router_apikey','router_api_key','entry_otp','wifi_pw','secret_value','token_value'):
            with self.subTest(suffix=suffix),self.assertRaises(ValueError):relay_reads.RelayReads.entity_id('input_text.'+suffix)
        row={'entity_id':'input_text.sample','state':'synthetic-private-value','attributes':{'mode':'password'}}
        fetch=Mock(return_value=[row]);broker=relay_reads.RelayReads(lambda:(types.SimpleNamespace(poll=lambda:None),False),lambda:'synthetic',lambda s:s,fetch)
        cap=broker.environment({})['HA_RELAY_CAPABILITY']
        result=broker.dispatch({'capability':cap,'arguments':{'operation':'states'}})
        self.assertEqual(result['entities'],[])
        fetch.return_value=row
        with self.assertRaises(ValueError):broker.dispatch({'capability':cap,'arguments':{'operation':'state','entity_id':row['entity_id']}})

    def test_f5_transient_discovery_is_retried_for_this_launch(self):
        fetch=Mock(side_effect=[OSError('synthetic timeout'),{'result':'ok','data':{'port':80,'ssl':False}}])
        value=core_address.CoreAddress().refresh({},'synthetic',fetch)
        self.assertEqual(value['url'],'http://homeassistant:80');self.assertEqual(fetch.call_count,2)

    def test_f6_relay_environment_and_namespace_probe_run_outside_supervisor_lock(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(remote_control.threading,'Thread'):
            proc=types.SimpleNamespace(stdout=io.StringIO(''),wait=lambda:0,poll=lambda:None)
            supervisor=None
            def env():
                self.assertFalse(supervisor.lock._is_owned(),'Supervisor I/O holds relay lock')
                return {}
            supervisor=remote_control.Supervisor('/synthetic/codex',Path(tmp)/'state',env,lambda:'Sample',gate=lambda:'',popen=Mock(return_value=proc),run=Mock(return_value=types.SimpleNamespace(returncode=0)))
            supervisor.enabled=lambda:True;supervisor.wake=Mock();supervisor.wake.is_set.return_value=False;supervisor.wake.wait.return_value=True
            supervisor._loop()

    def test_f7_ssl_override_preserves_certificate_verification_guidance(self):
        docs=(ROOT/'DOCS.md').read_text()
        self.assertIn('certificate hostname',docs);self.assertIn('Never disable TLS verification',docs)

    def test_f8_design_note_has_no_private_mail_filename(self):
        design=(ROOT.parent/'docs/design/CX-HA-URL.md').read_text()
        self.assertNotRegex(design,r'\b\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9-]+\.md\b')


class TransportEdges(unittest.TestCase):
    def test_f1_probe_is_bounded_fixed_and_never_sends_supervisor_credential(self):
        raw,client,session=response(b'401: Unauthorized');raw.status_code=401
        with patch.object(core_address.requests,'Session',return_value=session):
            self.assertTrue(core_address.probe_core(80))
        self.assertFalse(client.trust_env)
        args,kwargs=client.get.call_args
        self.assertEqual(args,('http://homeassistant:80/api/',))
        self.assertEqual(kwargs['headers'],{});self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'],(1,2))
        for code,body in ((200,b'{"message":"API running."}'),(302,b''),(200,b'{"message":"unrelated"}'),(401,b'unrelated'),(200,b'x'*65537)):
            with self.subTest(code=code,length=len(body)),patch.object(core_address.requests,'Session',return_value=session):
                raw.status_code=code;raw.iter_content.return_value=[body]
                self.assertEqual(core_address.probe_core(8123),code==200 and body==b'{"message":"API running."}')

    def test_f1_fallback_order_and_both_failed_probes_stay_unavailable(self):
        for failure in (ValueError('403'),TimeoutError(),ValueError('JSON')):
            probe=Mock(return_value=False)
            result=core_address.CoreAddress().refresh({},'synthetic',Mock(side_effect=failure),probe=probe)
            self.assertIsNone(result['url']);self.assertEqual([c.args for c in probe.call_args_list],[(8123,),(80,)])
        probe=Mock(return_value=True)
        result=core_address.CoreAddress().refresh({},'synthetic',Mock(side_effect=ValueError()),probe=probe)
        self.assertEqual(result['url'],'http://homeassistant:8123');probe.assert_called_once_with(8123)

    def test_f2_above_sixteen_megabytes_has_clear_error(self):
        raw,_,session=response(b'x'*(16*1024*1024+1))
        with patch.object(core_address.requests,'Session',return_value=session),self.assertRaisesRegex(relay_reads.StateResponseTooLarge,'16 MiB'):
            relay_reads.fetch_states('/states','synthetic')

    def test_f4_password_mode_and_compound_names_are_filtered_in_listings(self):
        rows=[{'entity_id':'input_text.'+name,'state':'synthetic','attributes':{'mode':mode}} for name,mode in (('entry_passcode','text'),('sample','PASSWORD'),('safe_label','text'))]
        broker=relay_reads.RelayReads(lambda:(types.SimpleNamespace(poll=lambda:None),False),lambda:'synthetic',lambda s:s,Mock(return_value=rows))
        cap=broker.environment({})['HA_RELAY_CAPABILITY']
        result=broker.dispatch({'capability':cap,'arguments':{'operation':'states'}})
        self.assertEqual([r['entity_id'] for r in result['entities']],['input_text.safe_label'])

    def test_f5_each_relay_launch_retries_after_an_unavailable_launch(self):
        metadata=Mock(side_effect=[ValueError(),ValueError(),{'result':'ok','data':{'port':80,'ssl':False}}])
        with patch.object(server,'core_address',core_address.CoreAddress()),patch.object(server,'ha_token',return_value='synthetic'), \
             patch.object(server,'read_options',return_value={**server.DEFAULT_OPTIONS,'HA_TOKEN':''}), \
             patch.object(core_address,'get_json',metadata),patch.object(server,'probe_core',return_value=False),patch.object(server.time,'sleep'):
            first=server.relay_env();second=server.relay_env()
        self.assertNotIn('HA_URL',first);self.assertEqual(second['HA_URL'],'http://homeassistant:80')
        self.assertEqual(metadata.call_count,3)

    def test_f6_stop_during_refresh_never_starts_a_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            supervisor=None
            def env():
                self.assertFalse(supervisor.lock._is_owned())
                supervisor.wake.set()
                return {}
            popen=Mock();supervisor=remote_control.Supervisor('/synthetic/codex',Path(tmp)/'state',env,lambda:'Sample',gate=lambda:'',popen=popen,run=Mock(return_value=types.SimpleNamespace(returncode=0)))
            supervisor.enabled=lambda:True
            supervisor._loop();popen.assert_not_called()
            self.assertEqual(list((Path(tmp)/'remote_control_control').glob('rc-*')),[])


class NativeErrorTests(unittest.TestCase):
    def test_f2_limit_error_reaches_native_tool_without_raw_backend_data(self):
        if os.environ.get('PYTEST_CURRENT_TEST'):
            import pytest_socket
            pytest_socket.socket_allow_hosts([],allow_unix_socket=True)
        broker=relay_reads.RelayReads(lambda:(types.SimpleNamespace(poll=lambda:None),False),lambda:'synthetic',lambda s:s,
            Mock(side_effect=relay_reads.StateResponseTooLarge('Core state response exceeds 16 MiB. Request a single entity instead.')))
        cap=broker.environment({})['HA_RELAY_CAPABILITY']
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'read.sock';listener=broker.serve(path)
            try:
                with patch.dict(os.environ,{'HA_RELAY_CAPABILITY':cap,'HA_RELAY_SOCKET':str(path)}):
                    result=relay_reads_mcp.read({'operation':'states'})
                self.assertFalse(result['ok']);self.assertIn('16 MiB',result['error'])
            finally:listener.shutdown();listener.server_close()
