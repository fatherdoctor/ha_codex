"""Synthetic Core/ingress/stdio integration; never contact a running HA instance."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_server import server
from permission_overrides import ApprovalRequired, Authority, DEFAULTS, Engine, Settings
import permission_overrides_api as api
import permission_overrides_mcp as mcp
from interaction_diagnostics import InteractionRecorder, RollingDiagnostics
from diagnostics_scrub import sanitize_export
from approvals import touches_protected_path, command_names_protected_path
from test_permission_overrides import Backend


class RuntimeOverrides(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.config = self.root / 'config'; self.config.mkdir()
        self.store = RollingDiagnostics(self.root / 'interaction-diagnostics.json')
        self.recorder = InteractionRecorder(self.store, '0.1.81.2', '0.166.0')
        self.recorder.begin('task', 'addon_page')
        self.active = True
        self.options = dict(server.DEFAULT_OPTIONS)
        self.data = SimpleNamespace(lock=threading.RLock(), capabilities={'task': 'synthetic-cap'},
                                    active=lambda task, cap: self.active and task == 'task' and cap == 'synthetic-cap')
        self.worker = SimpleNamespace(DATA_ROOT=self.root, CONFIG_ROOT=self.config,
            diagnostics=self.recorder, lock=threading.RLock(), ha_data=self.data,
            protected_policy=server.protected_policy, task_options=lambda _: self.options)
        self.service = api.Service(self.worker)
        self.service.begin('task', 'synthetic-cap', self.options)
        self.backend = Backend()

    def dispatch(self, **arguments):
        with patch.object(api, 'Core', return_value=self.backend):
            return self.service.dispatch({'arguments': arguments, 'capability': 'synthetic-cap'})

    def test_registered_worker_tool_and_packaging(self):
        self.assertIn('/permissions-overrides', {r.rule for r in server.app.url_map.iter_rules()})
        for args in (server.build_app_server_args(), server.relay_read_mcp_args()):
            self.assertIsInstance(args, list)
        args = ' '.join(server.build_app_server_args())
        self.assertIn('permission_overrides_mcp.py', args)
        self.assertIn('tools.act.approval_mode', args)
        docker = (Path(server.__file__).parent / 'Dockerfile').read_text()
        for name in ('permission_overrides.py', 'permission_overrides_api.py', 'permission_overrides_mcp.py'):
            self.assertIn(name, docker)

    def test_actual_scrubbed_audit_is_persisted_before_the_write(self):
        def execute(plan):
            rows = self.store.export()['interactions']
            event = rows[0]['events'][-1]
            self.assertEqual((event['kind'], event['operation'], event['outcome']), ('override', 'service', 'requested'))
            return {'status': 'accepted'}
        self.backend.execute = execute
        result = self.dispatch(operation='service', service='input_text.set_value', target=['input_text.sample'], data={'value': 'PRIVATE_VALUE_CANARY'})
        self.assertTrue(result['ok'])
        exported = sanitize_export(self.store.export())
        events = exported['interactions'][0]['events']
        self.assertEqual([e['outcome'] for e in events], ['requested', 'completed'])
        self.assertNotIn('PRIVATE_VALUE_CANARY', json.dumps(exported))
        self.assertNotIn('input_text.sample', json.dumps(exported))
        self.assertNotIn('PRIVATE_VALUE_CANARY', self.store.path.read_text())
        self.assertEqual(events[-1]['group'], 'everyday')
        self.assertEqual(events[-1]['decision'], 'preapproved')
        self.assertIsNotNone(events[-1]['duration_seconds'])

    def test_missing_active_recorder_and_failed_persistence_make_zero_writes(self):
        for failure in ('missing', 'inactive', 'disk'):
            with self.subTest(failure=failure):
                self.worker.diagnostics = self.recorder
                if failure == 'missing': self.worker.diagnostics = None
                elif failure == 'inactive': self.recorder.active.clear()
                else:
                    self.recorder.begin('task', 'addon_page')
                    self.store.unavailable = True
                self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_remote_forged_expired_replayed_and_read_only_cannot_write(self):
        for payload in (
            {'capability': '', 'arguments': {'operation': 'check_config'}},
            {'capability': 'remote', 'arguments': {'operation': 'check_config'}},
            {'capability': 'synthetic-cap', 'arguments': {'operation': 'check_config', 'mode': 'default'}},
            {'capability': 'synthetic-cap', 'arguments': {'operation': 'check_config'}, 'mode': 'default'},
        ):
            with patch.object(api, 'Core', return_value=self.backend):
                self.assertFalse(self.service.dispatch(payload)['ok'])
        self.active = False
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.active = True
        self.options['codex_sandbox'] = 'read-only'
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.service.begin('task', 'synthetic-cap', self.options)
        self.options['codex_sandbox'] = 'workspace-write'
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.service.end('task')
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_cancellation_during_audit_does_not_write(self):
        with patch.object(self.service, 'audit', side_effect=lambda *_: setattr(self, 'active', False)):
            self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_guard_file_is_reread_and_unreadable_refuses(self):
        path = self.config / 'AGENTS.md'
        path.write_text('Custom: ask before any changes.')
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        path.unlink(); path.symlink_to(self.root / 'absent')
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_settings_path_protected_in_existing_remote_hook_and_approval_adapter(self):
        path = '/data/codex-home/house-rules/permission-overrides.json'
        self.assertTrue(touches_protected_path(path))
        self.assertTrue(command_names_protected_path('cat ' + path))
        self.assertTrue(command_names_protected_path('rm -rf /data/codex-home/house-rules'))
        self.assertTrue(server.gate_rules._policy_restriction('echo x > ' + path))
        self.assertFalse(touches_protected_path('/config/scripts.yaml'))

    def test_private_settings_and_capability_socket_roundtrip(self):
        # Match the HA pytest harness: permit this Unix transport only, with
        # every IP connection still blocked.
        if os.environ.get('PYTEST_CURRENT_TEST'):
            import pytest_socket
            pytest_socket.socket_allow_hosts([], allow_unix_socket=True)
        self.service.settings().save(DEFAULTS)
        self.assertEqual(self.service.settings().path.stat().st_mode & 0o777, 0o600)
        sock = self.root / 'override.sock'
        with patch.object(api, 'Core', return_value=self.backend):
            listener = self.service.serve(sock)
            try:
                with patch.dict(os.environ, {'HA_OVERRIDE_SOCKET': str(sock), 'HA_DATA_CAPABILITY': 'synthetic-cap'}):
                    self.assertTrue(mcp.act({'operation': 'check_config'})['ok'])
                    with patch.dict(os.environ, {'HA_DATA_CAPABILITY': ''}):
                        self.assertTrue(mcp.act({'operation': 'check_config'})['approval_required'])
            finally:
                listener.shutdown(); listener.server_close()
        self.assertEqual(sock.stat().st_mode & 0o777, 0o600)

    def test_admin_only_get_and_post_fail_closed_with_no_worker_token_bypass(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(server, 'DATA_ROOT', self.root))
            stack.enter_context(patch.object(server, 'api_token', return_value='synthetic-api'))
            identity = stack.enter_context(patch.object(server, 'ingress_admin', return_value=(None, 'Not an administrator.')))
            ingress = stack.enter_context(patch.object(server, 'is_ingress_request', return_value=True))
            client = server.app.test_client()
            for method in ('get', 'post'):
                self.assertEqual(getattr(client, method)('/permissions-overrides', json=DEFAULTS).status_code, 403)
            identity.return_value = ({'user_id': 'synthetic-admin'}, '')
            self.assertEqual(client.get('/permissions-overrides').json['groups'], DEFAULTS)
            value = {**DEFAULTS, 'everyday': False}
            self.assertEqual(client.post('/permissions-overrides', json=value).json['groups'], value)
            for bad in ({}, {**DEFAULTS, 'extra': True}, {**DEFAULTS, 'apply': 1}):
                self.assertEqual(client.post('/permissions-overrides', json=bad).status_code, 400)
            server.override_service.settings().path.write_text('corrupt')
            self.assertEqual(client.get('/permissions-overrides').status_code, 400)
            ingress.return_value = False
            self.assertIn(client.post('/permissions-overrides', json=DEFAULTS, headers={'Authorization': 'Bearer synthetic-api'}).status_code, (401, 403))


class CoreEndpoints(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worker = SimpleNamespace(DATA_ROOT=self.root)
        self.core = api.Core(self.worker)

    def test_fixed_service_paths_and_payloads(self):
        cases = [({'operation': 'service', 'service': 'light.turn_on', 'target': ['light.sample'], 'data': {'brightness': 100}}, 'services/light/turn_on', {'brightness': 100, 'entity_id': ['light.sample']}),
                 ({'operation': 'reload', 'target': 'automation'}, 'services/automation/reload', {}),
                 ({'operation': 'check_config'}, 'services/homeassistant/check_config', {}),
                 ({'operation': 'backup'}, 'services/backup/create', {}),
                 ({'operation': 'restart'}, 'services/homeassistant/restart', {})]
        for plan, path, payload in cases:
            with self.subTest(plan=plan), patch.object(self.core, 'rest', return_value={}) as rest:
                self.core.execute(plan); rest.assert_called_once_with('POST', path, payload)

    def test_registry_mutations_and_reference_report(self):
        for operation, target, data, kind, payload in (
            ('update_entity', 'light.sample', {'name': 'Sample'}, 'config/entity_registry/update', {'entity_id': 'light.sample', 'name': 'Sample'}),
            ('rename_entity', 'light.sample', {'new_entity_id': 'light.renamed'}, 'config/entity_registry/update', {'entity_id': 'light.sample', 'new_entity_id': 'light.renamed'}),
            ('delete_entity', 'light.sample', {}, 'config/entity_registry/remove', {'entity_id': 'light.sample'}),
            ('update_device', 'sample', {'name_by_user': 'Sample'}, 'config/device_registry/update', {'device_id': 'sample', 'name_by_user': 'Sample'}),
        ):
            with self.subTest(operation=operation), patch.object(self.core, 'ws') as ws:
                self.core.execute({'operation': operation, 'target': target, 'data': data})
                ws.assert_called_once_with(kind, **payload)
        with patch.object(self.core, 'ws', return_value={'automation': ['sample']}) as ws:
            self.assertIn('coverage', self.core.references('light.sample'))
            ws.assert_called_once_with('search/related', item_type='entity', item_id='light.sample')

    def test_device_delete_uses_the_supported_core_config_entry_contract(self):
        # The 2026.6.1 and 2026.10.3 handlers take device_id + config_entry_id.
        # The newer dev-only /remove command is not assumed on supported releases.
        with patch.object(self.core, 'ws', side_effect=[[{'id': 'sample', 'config_entries': ['synthetic']}], None]) as ws:
            self.core.execute({'operation': 'delete_device', 'target': 'sample'})
            self.assertEqual(ws.call_args_list[-1].args, ('config/device_registry/remove_config_entry',))
            self.assertEqual(ws.call_args_list[-1].kwargs, {'device_id': 'sample', 'config_entry_id': 'synthetic'})
        for row in ({'id': 'sample', 'config_entries': []}, {'id': 'sample', 'config_entries': ['first', 'second']}):
            with patch.object(self.core, 'ws', return_value=[row]) as ws:
                with self.assertRaises(ApprovalRequired): self.core.execute({'operation': 'delete_device', 'target': 'sample'})
                ws.assert_called_once_with('config/device_registry/list')

    def test_config_definition_and_delete_endpoints(self):
        for domain in ('script', 'automation', 'scene'):
            with self.subTest(domain=domain), patch.object(self.core, 'config_id', return_value='sample'), patch.object(self.core, 'rest', return_value={}) as rest:
                self.core.definition(domain + '.sample')
                rest.assert_called_once_with('GET', 'config/' + domain + '/config/sample')
                rest.reset_mock()
                self.core.execute({'operation': 'delete_' + domain, 'target': domain + '.sample'})
                rest.assert_called_once_with('DELETE', 'config/' + domain + '/config/sample')

    def test_dashboard_backup_is_private_and_precedes_save_or_delete(self):
        calls = []
        def ws(kind, **data):
            calls.append(kind)
            if kind == 'lovelace/config': return {'views': []}
            if kind == 'lovelace/dashboards/list': return [{'url_path': 'sample', 'id': 'synthetic'}]
            backups = list((self.root / 'permission-overrides-backups').glob('dashboard-*.json'))
            self.assertTrue(backups)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        with patch.object(self.core, 'ws', side_effect=ws):
            self.core.execute({'operation': 'save_dashboard', 'target': 'sample', 'data': {'config': {}}})
            self.core.execute({'operation': 'delete_dashboard', 'target': 'sample'})
        self.assertEqual(calls, ['lovelace/config', 'lovelace/config/save', 'lovelace/config', 'lovelace/dashboards/list', 'lovelace/dashboards/delete'])

    def test_flow_paths_and_own_integration_refusal(self):
        with patch.object(self.core, 'rest', return_value={'flow_id': 'synthetic', 'type': 'form', 'step_id': 'user'}) as rest:
            self.core.execute({'operation': 'flow_start', 'target': 'sample'})
            rest.assert_called_once_with('POST', 'config/config_entries/flow', {'handler': 'sample'})
            rest.reset_mock()
            self.core.execute({'operation': 'flow_continue', 'target': 'synthetic', 'data': {'field': 'synthetic'}})
            rest.assert_called_once_with('POST', 'config/config_entries/flow/synthetic', {'field': 'synthetic'})
        for handler in ('hassio', 'supervisor', 'codex_cli', 'ha_codex', None):
            with patch.object(self.core, 'rest', return_value={'handler': handler}):
                with self.assertRaises(ApprovalRequired): self.core.validate_flow('flow_continue', 'synthetic')

    def test_selector_expansion_and_registry_platform_authority(self):
        entities = [{'entity_id': 'light.sample', 'device_id': 'sample', 'area_id': None, 'labels': ['sample']},
                    {'entity_id': 'lock.sample', 'device_id': 'sample', 'area_id': None}]
        with patch.object(self.core, 'ws', side_effect=lambda kind, **_: entities if kind == 'config/entity_registry/list' else [{'id': 'sample', 'area_id': 'sample'}]), patch.object(self.core, 'require_entity'):
            for field in ('device_id', 'area_id', 'label_id'):
                self.assertIn('light.sample', self.core.resolve({field: 'sample'}))
            with self.assertRaises(ApprovalRequired): self.core.resolve({'area_id': 'missing'})
        with patch.object(self.core, 'rest', return_value={'entity_id': 'switch.sample'}), patch.object(self.core, 'ws', return_value={'platform': 'codex_cli'}):
            with self.assertRaises(ApprovalRequired): self.core.require_entity('switch.sample')

    def test_rest_transport_bounded_no_redirect_no_retries(self):
        response = Mock(); response.status_code = 200; response.raw.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        self.worker.ha_token = lambda: 'synthetic-token'
        self.worker.ha_api_url = lambda p: p
        self.worker.requests = SimpleNamespace(request=Mock(return_value=response))
        self.core.rest('POST', 'services/light/turn_on', {})
        self.worker.requests.request.assert_called_once()
        self.assertFalse(self.worker.requests.request.call_args.kwargs['allow_redirects'])
        response.raw.read.return_value = b'x' * (1024 * 1024 + 1)
        with self.assertRaises(ApprovalRequired): self.core.rest('GET', 'sample')
        self.worker.requests.request.side_effect = TimeoutError('PRIVATE_CANARY')
        with self.assertRaises(TimeoutError): self.core.rest('POST', 'sample')
        self.assertEqual(self.worker.requests.request.call_count, 3)

    def test_websocket_transport_authentication_and_fixed_command(self):
        stream = Mock(); stream.recv.side_effect = [json.dumps({'type': 'auth_required'}), json.dumps({'type': 'auth_ok'}), json.dumps({'id': 1, 'success': True, 'result': []})]
        self.worker.ha_token = lambda: 'synthetic-token'
        self.worker.ha_ws_url = lambda: 'synthetic'
        self.worker.websocket = SimpleNamespace(create_connection=Mock(return_value=stream))
        self.assertEqual(self.core.ws('config/entity_registry/list'), [])
        self.assertEqual(json.loads(stream.send.call_args_list[-1].args[0]), {'type': 'config/entity_registry/list', 'id': 1})
        stream.close.assert_called_once()

    def test_deadline_refuses_late_dispatch_and_target_expansion_is_bounded(self):
        self.core.deadline = 0
        with patch.object(self.core, 'rest') as rest:
            with self.assertRaises(ApprovalRequired): self.core.execute({'operation': 'restart'})
        rest.assert_not_called()
        with patch.object(self.core, 'require_entity') as read:
            with self.assertRaises(ApprovalRequired): self.core.resolve(['light.sample'] * 65)
        read.assert_not_called()

    def test_stdio_transport_failure_is_never_a_retry_instruction(self):
        with patch.object(mcp, 'act', side_effect=TimeoutError('PRIVATE_CANARY')):
            response = mcp.respond({'method': 'tools/call', 'params': {'name': 'act', 'arguments': {'operation': 'restart'}}})
        result = json.loads(response['content'][0]['text'])
        self.assertTrue(result['do_not_retry']); self.assertFalse(result['approval_required'])
        self.assertNotIn('PRIVATE_CANARY', json.dumps(response))
