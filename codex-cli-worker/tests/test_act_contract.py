"""Contract tests: the exact Home Assistant call behind each pre-approved action.

The chat's "act" tool is driven the way Codex drives it: the MCP tools/call, then the worker's
socket, the permission engine and the real Core client. Only Home Assistant is fake. It records
every REST and WebSocket request, so a test checks the method, path, target and data that would
reach Home Assistant, not only the guard's yes or no. No network is used.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_server import server
from permission_overrides import DEFAULTS, SERVICES
import permission_overrides_api as api
import permission_overrides_mcp as mcp
from interaction_diagnostics import InteractionRecorder, RollingDiagnostics

CAPABILITY = 'synthetic-cap'


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.raw = SimpleNamespace(read=lambda size, decode_content=True: body)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSocket:
    """One WebSocket exchange: auth, then one command answered with the registered result."""
    def __init__(self, home):
        self.home, self.queue = home, [json.dumps({'type': 'auth_required'})]

    def recv(self):
        return self.queue.pop(0)

    def send(self, text):
        message = json.loads(text)
        if message['type'] == 'auth':
            self.queue.append(json.dumps({'type': 'auth_ok'}))
            return
        self.home.calls.append(('WS', message['type'], {k: v for k, v in message.items() if k not in ('type', 'id')}))
        result = self.home.ws_results.get(message['type'], {})
        self.queue.append(json.dumps({'id': message['id'], 'success': True, 'result': result}))

    def settimeout(self, _):
        pass

    def close(self):
        pass


class FakeHome:
    """Records each request. Every entity exists and is an ordinary one unless a test says otherwise."""
    def __init__(self):
        self.calls = []
        self.status = {}
        self.ws_results = {'config/entity_registry/get': {'platform': 'demo'},
                           'config/device_registry/list': [{'id': 'dev1', 'config_entries': ['entry1']}],
                           'search/related': {}}
        self.requests = SimpleNamespace(request=self.request)
        self.websocket = SimpleNamespace(create_connection=lambda url, timeout=None: FakeSocket(self))

    def request(self, method, url, json=None, **_):
        path = url.split('/api/', 1)[1]
        self.calls.append((method, path, json))
        status = self.status.get((method, path), 200)
        if method == 'GET' and path.startswith('states/'):
            body = {'entity_id': path[len('states/'):], 'attributes': {'id': 'abc'}}
        else:
            body = {}
        return FakeResponse(status, __import__('json').dumps(body).encode())

    def ha_token(self):
        return 'synthetic-token'

    def ha_api_url(self, path):
        return 'http://ha.invalid/api/' + path

    def ha_ws_url(self):
        return 'ws://ha.invalid/api/websocket'

    def writes(self):
        """Everything except the preflight reads of one entity and its registry row."""
        return [c for c in self.calls
                if c[0] not in ('GET',) and c[1] not in ('config/entity_registry/get',)]


class ActContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = root / 'config'
        config.mkdir()
        recorder = InteractionRecorder(RollingDiagnostics(root / 'interaction-diagnostics.json'), '0.1.81.2', '0.166.0')
        recorder.begin('task', 'addon_page')
        options = dict(server.DEFAULT_OPTIONS)
        data = SimpleNamespace(lock=threading.RLock(), capabilities={'task': CAPABILITY},
                               active=lambda task, cap: task == 'task' and cap == CAPABILITY)
        self.home = FakeHome()
        worker = SimpleNamespace(
            DATA_ROOT=root, CONFIG_ROOT=config, diagnostics=recorder, lock=threading.RLock(), ha_data=data,
            protected_policy=server.protected_policy, task_options=lambda _: options,
            requests=self.home.requests, websocket=self.home.websocket, ha_token=self.home.ha_token,
            ha_api_url=self.home.ha_api_url, ha_ws_url=self.home.ha_ws_url)
        self.service = api.Service(worker)
        self.service.begin('task', CAPABILITY, options)
        self.groups({})
        # Path under tmp stays short enough for a Unix socket.
        self.socket_path = root / 'o.sock'
        listener = self.service.serve(self.socket_path)
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        patcher = patch.dict(os.environ, {'HA_OVERRIDE_SOCKET': str(self.socket_path), 'HA_DATA_CAPABILITY': CAPABILITY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def groups(self, values):
        """All five groups on, then the given changes."""
        self.service.settings().save({**{key: True for key in DEFAULTS}, **values})

    def act(self, **arguments):
        """The MCP act call, as Codex sends it, answered by the real engine and Core client."""
        reply = mcp.respond({'method': 'tools/call', 'params': {'name': 'act', 'arguments': arguments}})
        return json.loads(reply['content'][0]['text'])

    def assert_service_call(self, service, target, data, **arguments):
        result = self.act(operation='service', service=service, target=target, data=data, **arguments)
        self.assertTrue(result['ok'], result)
        path = 'services/' + service.replace('.', '/')
        self.assertEqual(self.home.writes(), [('POST', path, {**data, **({'entity_id': [target]} if target else {})})])


class LightTests(ActContract):
    def test_light_brightness_reaches_the_named_light(self):
        self.assert_service_call('light.turn_on', 'light.kitchen', {'brightness_pct': 10})

    def test_light_turn_off(self):
        self.assert_service_call('light.turn_off', 'light.kitchen', {})

    def test_everyday_group_off_asks_instead_of_writing(self):
        self.groups({'everyday': False})
        result = self.act(operation='service', service='light.turn_on', target='light.kitchen', data={})
        self.assertFalse(result['ok'])
        self.assertEqual(self.home.writes(), [])


# One valid example per domain in SERVICES: (service, target, data).
EXAMPLES = {
    'light': ('light.toggle', 'light.hall', {}),
    'switch': ('switch.turn_on', 'switch.fan', {}),
    'input_boolean': ('input_boolean.turn_off', 'input_boolean.guest', {}),
    'input_number': ('input_number.set_value', 'input_number.target', {'value': 21}),
    'input_select': ('input_select.select_option', 'input_select.mode', {'option': 'home'}),
    'input_text': ('input_text.set_value', 'input_text.note', {'value': 'hello'}),
    'scene': ('scene.turn_on', 'scene.evening', {'transition': 2}),
    'automation': ('automation.trigger', 'automation.porch', {'skip_condition': True}),
    'script': ('script.turn_on', 'script.goodnight', {'variables': {'level': 1}}),
    'lock': ('lock.lock', 'lock.front', {}),
    'alarm_control_panel': ('alarm_control_panel.alarm_arm_home', 'alarm_control_panel.house', {}),
    'cover': ('cover.set_cover_position', 'cover.blind', {'position': 40}),
    'camera': ('camera.turn_off', 'camera.yard', {}),
    'persistent_notification': ('persistent_notification.create', None, {'message': 'Hi', 'title': 'T'}),
}


class EveryDomainTests(ActContract):
    def test_examples_cover_every_supported_domain(self):
        self.assertEqual(set(EXAMPLES), set(SERVICES))

    def test_each_supported_domain_sends_its_exact_call(self):
        for domain, (service, target, data) in EXAMPLES.items():
            with self.subTest(domain=domain):
                self.home.calls.clear()
                self.assert_service_call(service, target, data)

    def test_group_of_each_domain_gates_it(self):
        """Turning a domain's own group off stops the write; the others do not."""
        groups = {'light': 'everyday', 'scene': 'everyday', 'script': 'bigger', 'lock': 'security',
                  'cover': 'security', 'persistent_notification': 'security'}
        for domain, group in groups.items():
            with self.subTest(domain=domain):
                self.home.calls.clear()
                self.groups({group: False})
                service, target, data = EXAMPLES[domain]
                result = self.act(operation='service', service=service, target=target, data=data)
                self.assertFalse(result['ok'])
                self.assertEqual(self.home.writes(), [])


class NotifyTests(ActContract):
    @unittest.expectedFailure
    def test_notify_send_message_keeps_its_target(self):
        # CX-FB5-NOTIFY: the target is dropped today, so Home Assistant gets no entity_id.
        # The fix lands its own contract test and removes this mark.
        result = self.act(operation='service', service='notify.send_message',
                          target='notify.test_target', data={'message': 'Test'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.home.writes(),
                         [('POST', 'services/notify/send_message', {'message': 'Test', 'entity_id': ['notify.test_target']})])

    def test_notify_stays_in_the_security_group(self):
        self.groups({'security': False})
        result = self.act(operation='service', service='notify.send_message', data={'message': 'Test'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.home.writes(), [])


class OperationTests(ActContract):
    def test_reload_check_backup_restart(self):
        cases = [(dict(operation='reload', target='automation'), 'services/automation/reload', {}),
                 (dict(operation='check_config'), 'services/homeassistant/check_config', {}),
                 (dict(operation='backup'), 'services/backup/create', {}),
                 (dict(operation='restart'), 'services/homeassistant/restart', {})]
        for arguments, path, body in cases:
            with self.subTest(operation=arguments['operation']):
                self.home.calls.clear()
                self.assertTrue(self.act(**arguments)['ok'])
                self.assertEqual(self.home.writes(), [('POST', path, body)])

    def test_update_entity_uses_the_registry(self):
        result = self.act(operation='update_entity', target='light.kitchen', data={'name': 'Kitchen'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.home.writes(),
                         [('WS', 'config/entity_registry/update', {'entity_id': 'light.kitchen', 'name': 'Kitchen'})])

    def test_delete_entity_uses_the_registry(self):
        self.assertTrue(self.act(operation='delete_entity', target='light.kitchen')['ok'])
        self.assertEqual(self.home.writes(), [('WS', 'config/entity_registry/remove', {'entity_id': 'light.kitchen'})])

    def test_flow_start_posts_the_handler(self):
        self.act(operation='flow_start', target='demo')
        self.assertEqual(self.home.writes(), [('POST', 'config/config_entries/flow', {'handler': 'demo'})])


class RefusalTests(ActContract):
    def test_missing_entity_is_a_refusal_before_any_write(self):
        self.home.status[('GET', 'states/light.kitchen')] = 404
        result = self.act(operation='service', service='light.turn_on', target='light.kitchen', data={})
        self.assertFalse(result['ok'])
        self.assertTrue(result['approval_required'])
        self.assertFalse(result.get('do_not_retry', False))
        self.assertEqual(self.home.writes(), [])

    def test_refusal_of_the_write_itself_is_reported_as_unknown(self):
        # Once the POST is sent, a non-2xx answer cannot prove nothing changed: never retry.
        self.home.status[('POST', 'services/light/turn_on')] = 500
        result = self.act(operation='service', service='light.turn_on', target='light.kitchen', data={})
        self.assertFalse(result['ok'])
        self.assertTrue(result['do_not_retry'])
        self.assertFalse(result['approval_required'])

    def test_target_in_data_is_never_sent(self):
        result = self.act(operation='service', service='light.turn_on', target='light.kitchen',
                          data={'entity_id': 'light.other'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.home.writes(), [])


if __name__ == '__main__':
    unittest.main()
