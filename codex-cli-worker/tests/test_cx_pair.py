"""Pairing uses the supervised server's own control socket, with page-only codes."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import remote_control as rc
import test_remote_control as base

# Synthetic fixture value only; tests never request a real relay pairing code.
FIXTURE_CODE = 'TEST-0000'


class FakeControl:
    def __init__(self, owner, path):self.owner,self.path=owner,path
    def enable(self, cancelled=lambda:False):
        self.owner.calls.append(('enable',self.path));return {'status':'connected','serverName':'Home Assistant'}
    def status(self, cancelled=lambda:False):return {'status':'connected','serverName':'Home Assistant'}
    def pair(self, cancelled=lambda:False):
        self.owner.calls.append(('pair',self.path))
        if self.owner.pair_error:raise ValueError('unavailable')
        return {'manualPairingCode':self.owner.code,'expiresAt':123}


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.calls=[];self.commands=[];self.procs=[];self.code=FIXTURE_CODE;self.pair_error=False

    def supervisor(self):
        def popen(command,**kw):
            self.commands.append(command);proc=base.FakeProc();self.procs.append(proc);return proc
        sup=rc.Supervisor('codex',Path(self.temp.name)/'state.json',dict,lambda:'Home Assistant',gate=lambda:'',
                          popen=popen,control_factory=lambda path:FakeControl(self,path))
        self.addCleanup(sup.stop);sup.start()
        self.assertTrue(base.wait_for(lambda:sup.snapshot()['state']=='ready'))
        return sup

    def test_owned_socket_launch_and_pair_do_not_use_daemon_subcommand(self):
        sup=self.supervisor();ok,state=sup.pair()
        self.assertTrue(ok);self.assertTrue(state['pairing_code']==FIXTURE_CODE)
        command=self.commands[0]
        self.assertIn('app-server',command);self.assertIn('--listen',command);self.assertIn('--remote-control',command)
        self.assertNotIn('--no-daemon',command);self.assertNotIn('pair',command)
        enabled=next(path for method,path in self.calls if method=='enable')
        paired=next(path for method,path in self.calls if method=='pair')
        self.assertEqual(enabled,paired);self.assertTrue(str(enabled).startswith(self.temp.name))
        self.assertFalse(any(FIXTURE_CODE in line for line in state['log']))

    def test_pair_failure_or_missing_manual_code_is_honest_and_clears_old_code(self):
        sup=self.supervisor();self.assertTrue(sup.pair()[0])
        for invalid in (True,False):
            self.pair_error=invalid;self.code=None
            ok,state=sup.pair();self.assertFalse(ok);self.assertFalse(state['pairing_code']);self.assertTrue(state['error'])
            self.assertFalse(any(FIXTURE_CODE in line for line in state['log']))

    def test_process_restart_clears_code_and_uses_a_new_socket(self):
        with patch.multiple(rc,BACKOFF_START=.02,BACKOFF_CAP=.03):
            sup=self.supervisor();self.assertTrue(sup.pair()[0]);old_path=self.calls[-1][1]
            self.procs[0].exit(1)
            self.assertTrue(base.wait_for(lambda:len(self.procs)>1 and sup.snapshot()['state']=='ready'))
            self.assertFalse(sup.snapshot()['pairing_code']);self.assertNotEqual(old_path,self.calls[-1][1])

    def test_legacy_pair_output_is_shown_on_page_but_never_in_log(self):
        sup=rc.Supervisor('codex',Path(self.temp.name)/'state.json',dict,lambda:'',gate=lambda:'')
        sup._parse('Pairing code: '+FIXTURE_CODE)
        self.assertTrue(sup.snapshot()['pairing_code']==FIXTURE_CODE)
        self.assertFalse(any(FIXTURE_CODE in line for line in sup.snapshot()['log']))

    def test_nonpage_state_read_cannot_retrieve_code_or_qr(self):
        server=base.server;client=server.app.test_client()
        state={'state':'ready','pairing_code':FIXTURE_CODE,'pairing_qr':'data:image/svg+xml;base64,fixture'}
        with patch.object(server,'api_token',return_value='fixture'),patch.object(server,'ingress_admin',return_value=({'user_id':'fixture-admin'},'')),patch.object(server.remote_control_supervisor,'snapshot',return_value=state):
            token=client.get('/remote-control',headers={'Authorization':'Bearer fixture'})
            page=client.get('/remote-control',headers={'X-Ingress-Path':'/api/hassio_ingress/test'},environ_overrides={'REMOTE_ADDR':server.INGRESS_PROXY_IP})
        self.assertNotIn('pairing_code',token.get_json()['remote_control'])
        self.assertNotIn('pairing_qr',token.get_json()['remote_control'])
        self.assertTrue(page.get_json()['remote_control']['pairing_code']==FIXTURE_CODE)


class Transport:
    def __init__(self, no_code=False, fail=False):self.messages=[];self.pending=[];self.closed=False;self.no_code=no_code;self.fail=fail
    def settimeout(self,_):pass
    def send(self,text):
        message=json.loads(text);self.messages.append(message)
        if 'id' not in message:return
        method=message['method']
        result={} if method=='initialize' else {'status':'connected','serverName':'Home Assistant'}
        if method=='remoteControl/pairing/start':result={'manualPairingCode':None if self.no_code else FIXTURE_CODE,'expiresAt':123}
        self.pending.append(json.dumps({'id':message['id'],'error':{'code':-32601,'message':'unsupported'}} if self.fail else {'id':message['id'],'result':result}))
    def recv(self):return self.pending.pop(0)
    def close(self, **kwargs):self.closed=True


class ProtocolTests(unittest.TestCase):
    def test_pinned_rpc_uses_experimental_initialize_and_manual_code(self):
        transport=Transport();client=rc.ControlClient(Path('/not-a-real-socket'),connector=lambda *_:transport)
        result=client.pair();self.assertTrue(result['manualPairingCode']==FIXTURE_CODE)
        self.assertEqual([m['method'] for m in transport.messages],['initialize','initialized','remoteControl/pairing/start'])
        self.assertTrue(transport.messages[0]['params']['capabilities']['experimentalApi'])
        self.assertEqual(transport.messages[-1]['params'],{'manualCode':True});self.assertTrue(transport.closed)

    def test_rpc_error_is_generic_and_closes_transport(self):
        transport=Transport(fail=True);client=rc.ControlClient(Path('/not-a-real-socket'),connector=lambda *_:transport)
        with self.assertRaises(rc.ControlError) as caught:client.pair()
        self.assertFalse(FIXTURE_CODE in str(caught.exception));self.assertTrue(transport.closed)


class LifecycleTests(unittest.TestCase):
    def test_rpc_cancellation_closes_connection_without_waiting_for_reply(self):
        class SilentTransport(Transport):
            def recv(self):
                raise rc.websocket.WebSocketTimeoutException("timeout")
        transport = SilentTransport()
        attempts = []
        def cancelled():
            attempts.append(1)
            return len(attempts) > 3
        with self.assertRaises(rc.ControlError):
            rc.ControlClient(Path('/fixture'), connector=lambda *_: transport).pair(cancelled=cancelled)
        self.assertTrue(transport.closed)

    def test_stop_during_pair_discards_reply_and_closes_owned_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            procs=[];entered=threading.Event();release=threading.Event()
            class DelayedControl(base.FakeControl):
                def pair(self, cancelled=lambda:False):
                    entered.set();release.wait(timeout=2)
                    return {"manualPairingCode": FIXTURE_CODE}
            def popen(*args, **kwargs):
                proc=base.FakeProc();procs.append(proc);return proc
            sup=rc.Supervisor('codex',Path(tmp)/'state',dict,lambda:'',gate=lambda:'',popen=popen,control_factory=DelayedControl)
            self.addCleanup(sup.stop);sup.start()
            self.assertTrue(base.wait_for(lambda:sup.snapshot()['state']=='ready'))
            private=sup.control_socket.parent;self.assertEqual(private.stat().st_mode & 0o777,0o700)
            results=[];worker=threading.Thread(target=lambda:results.append(sup.pair()));worker.start()
            try:
                self.assertTrue(entered.wait(timeout=1));sup.stop();release.set();worker.join(timeout=2)
                self.assertFalse(worker.is_alive());self.assertFalse(results[0][0]);self.assertFalse(results[0][1]['pairing_code'])
                self.assertFalse(private.exists())
            finally:
                release.set();worker.join(timeout=2)
