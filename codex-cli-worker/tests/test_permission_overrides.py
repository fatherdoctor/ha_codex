"""Prepared action policy: synthetic backends only; never a live Core system."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from permission_overrides import ApprovalRequired, Authority, DEFAULTS, Engine, MARKERS, Settings, guard_rules
import permission_overrides_api as api


class Backend:
    validate_service = api.Core.validate_service
    validate_operation = api.Core.validate_operation
    device_entry = lambda self, target: "synthetic"

    def __init__(self):
        self.writes = []
        self.definitions = {}
        self.expansion = {'device_id': ['light.sample'], 'area_id': ['light.sample'], 'label_id': ['light.sample']}

    def resolve(self, selector):
        if isinstance(selector, str):
            return [selector]
        if isinstance(selector, list):
            return selector
        if isinstance(selector, dict):
            if 'entity_id' in selector:
                value = selector['entity_id']
                return value if isinstance(value, list) else [value]
            return self.expansion.get(next(iter(selector)), [])
        return []

    def require_entity(self, target):
        return {'entity_id': target}

    def definition(self, target):
        return deepcopy(self.definitions.get(target))

    def validate_flow(self, operation, target):
        return None

    def references(self, target):
        return {'core_related': {}, 'coverage': 'synthetic fixture'}

    def execute(self, plan):
        self.writes.append(deepcopy(plan))
        return {'status': 'accepted'}


class OverridePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root / 'settings.json')
        self.backend = Backend()
        self.rules = ''
        self.audit = []
        self.engine = Engine(self.settings, self.backend, lambda: self.rules, self.audit.append)
        self.authority = Authority('workspace-write', 'on-request', 'user', 'worker')

    def dispatch(self, **args):
        return self.engine.dispatch(args, self.authority)

    def enable(self, **values):
        self.settings.save({**DEFAULTS, **values})

    def test_default_groups_and_private_atomic_settings(self):
        self.assertEqual(self.settings.read(), DEFAULTS)
        self.enable(bigger=True)
        self.assertEqual(self.settings.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(Settings(self.settings.path).read()['bigger'])
        for value in ({}, {**DEFAULTS, 'unknown': True}, {**DEFAULTS, 'apply': 1}):
            with self.assertRaises(ValueError):
                self.settings.save(value)

    def test_corrupt_settings_do_not_restore_permissive_defaults(self):
        self.settings.path.write_text('not JSON')
        self.assertFalse(self.dispatch(operation='reload', target='automation')['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_read_only_remote_custom_unknown_and_forged_modes_never_write(self):
        for authority in (Authority(), Authority('read-only', 'on-request', 'user', 'worker'),
                          Authority('workspace-write', 'never', 'user', 'worker'),
                          Authority('workspace-write', 'on-request', 'user', 'remote')):
            result = self.engine.dispatch({'operation': 'check_config'}, authority)
            self.assertTrue(result['approval_required'])
        self.assertTrue(self.dispatch(operation='check_config', sandbox='workspace-write')['approval_required'])
        self.assertEqual(self.backend.writes, [])

    def test_expired_turn_and_permission_change_stop_before_write(self):
        request = {'operation': 'check_config'}
        self.assertFalse(self.engine.dispatch(request, self.authority, lambda: False)['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_mixed_mode_threads_and_resumed_turn_recheck_authority(self):
        request = {'operation': 'check_config'}
        self.assertTrue(self.engine.dispatch(request, self.authority)['ok'])
        readonly = Authority('read-only', 'on-request', 'user', 'worker')
        self.assertFalse(self.engine.dispatch(request, readonly)['ok'])
        self.assertFalse(self.engine.dispatch(request, Authority())['ok'])
        self.assertEqual(len(self.backend.writes), 1)

    def test_on_off_groups_for_each_operation_family(self):
        cases = (
            ('apply', {'operation': 'reload', 'target': 'automation'}),
            ('everyday', {'operation': 'service', 'service': 'light.turn_on', 'target': ['light.sample']}),
            ('organize', {'operation': 'update_entity', 'target': 'light.sample', 'data': {'name': 'Sample'}}),
            ('bigger', {'operation': 'restart'}),
            ('security', {'operation': 'service', 'service': 'lock.lock', 'target': ['lock.sample']}),
        )
        for group, request in cases:
            with self.subTest(group=group):
                self.enable(**{group: False})
                self.assertTrue(self.engine.dispatch(request, self.authority)['approval_required'])
                self.enable(**{group: True})
                self.assertTrue(self.engine.dispatch(request, self.authority)['ok'])

    def test_owner_guard_rails_win_when_security_group_is_on(self):
        self.enable(security=True)
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        self.assertFalse(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_custom_malformed_or_additional_owner_limits_require_approval(self):
        for text in ('Do not change the sample device.', MARKERS[0],
                     MARKERS[0] + '\n- Custom rule\n' + MARKERS[1]):
            with self.subTest(text=text):
                self.rules = text
                self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_scene_cannot_reach_disabled_or_restricted_domain(self):
        self.backend.definitions['scene.sample'] = {'entities': {'light.sample': 'on', 'lock.sample': 'unlocked'}}
        self.assertFalse(self.dispatch(operation='service', service='scene.turn_on', target=['scene.sample'])['ok'])
        self.enable(security=True)
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        self.assertFalse(self.dispatch(operation='service', service='scene.turn_on', target=['scene.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_script_and_automation_branches_check_all_indirect_targets(self):
        self.enable(bigger=True)
        self.backend.definitions['automation.sample'] = {'actions': [{'action': 'script.sample'}]}
        self.backend.definitions['script.sample'] = {'sequence': [{'choose': [
            {'conditions': [], 'sequence': [{'action': 'lock.unlock', 'target': {'entity_id': 'lock.sample'}}]}]}]}
        with patch.object(self.backend, 'definition', wraps=self.backend.definition) as definition:
            result = self.dispatch(operation='service', service='automation.trigger', target=['automation.sample'])
        self.assertFalse(result['ok'])
        self.assertIn('group is off', result['reason'])
        self.assertIn(unittest.mock.call('script.sample'), definition.call_args_list)
        self.assertEqual(self.backend.writes, [])

    def test_recursive_unknown_and_template_actions_require_approval(self):
        self.enable(bigger=True)
        for sequence in ([{'action': 'script.sample', 'target': {'entity_id': 'script.sample'}}],
                         [{'action': '{{ service }}'}], [{'unknown': 'action'}]):
            self.backend.definitions['script.sample'] = {'sequence': sequence}
            self.assertFalse(self.dispatch(operation='service', service='script.turn_on', target=['script.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_definition_change_between_check_and_write_is_refused(self):
        self.backend.definitions['scene.sample'] = {'entities': {'light.sample': 'on'}}
        with patch.object(self.backend, 'definition', side_effect=[{'entities': {'light.sample': 'on'}}, {'entities': {'lock.sample': 'unlocked'}}]):
            self.assertFalse(self.dispatch(operation='service', service='scene.turn_on', target=['scene.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_device_and_area_expansion_cannot_hide_a_security_target(self):
        self.backend.expansion['area_id'] = ['light.sample', 'lock.sample']
        self.assertFalse(self.dispatch(operation='service', service='light.turn_on', target={'area_id': 'sample'})['ok'])
        self.backend.expansion['device_id'] = ['lock.sample']
        self.assertFalse(self.dispatch(operation='update_device', target='sample', data={'name_by_user': 'Sample'})['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_hard_refusals_and_unknown_fields_cannot_be_enabled(self):
        self.enable(bigger=True, security=True)
        for request in ({'operation': 'service', 'service': 'hassio.addon_restart'},
                        {'operation': 'service', 'service': 'light.turn_on', 'target': ['light.private_token']},
                        {'operation': 'flow_start', 'target': 'hassio'},
                        {'operation': 'flow_start', 'target': 'codex_cli'},
                        {'operation': 'shell', 'data': {'command': 'sample'}},
                        {'operation': 'reload', 'target': '../settings'},
                        {'operation': 'restart', 'data': {'url': 'sample'}}):
            self.assertFalse(self.engine.dispatch(request, self.authority)['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_success_names_action_and_audit_never_sees_payload(self):
        result = self.dispatch(operation='service', service='input_text.set_value', target=['input_text.sample'], data={'value': 'SYNTHETIC_PRIVATE_VALUE'})
        self.assertTrue(result['ok'])
        self.assertIn('input_text.set_value', result['performed'])
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', json.dumps(self.audit))
        self.assertEqual([row['outcome'] for row in self.audit], ['requested', 'completed'])

    def test_missing_audit_refuses_before_write_and_late_failure_does_not_retry(self):
        self.engine.observer = Mock(side_effect=OSError('synthetic audit error'))
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])
        self.engine.observer = Mock(side_effect=[None, OSError('synthetic audit error')])
        self.assertTrue(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(len(self.backend.writes), 1)

    def test_backend_failure_is_unknown_and_never_retried(self):
        with patch.object(self.backend, 'execute', side_effect=TimeoutError('SYNTHETIC_PRIVATE_BACKEND')) as execute:
            result = self.dispatch(operation='check_config')
        execute.assert_called_once()
        self.assertFalse(result['ok'])
        self.assertTrue(result['do_not_retry'])
        self.assertNotIn('SYNTHETIC_PRIVATE_BACKEND', json.dumps(result))


class PreparedAdapterTests(unittest.TestCase):
    def test_unavailable_typed_diagnostics_refuses(self):
        service = api.Service(SimpleNamespace(diagnostics=None))
        with self.assertRaises(ApprovalRequired):
            service.audit('synthetic-task', {'operation': 'restart', 'outcome': 'requested'})

    def test_remote_and_client_supplied_mode_fields_never_authorize(self):
        worker = SimpleNamespace(lock=threading.RLock(), ha_data=SimpleNamespace(
            lock=threading.RLock(), capabilities={}, active=lambda *_: False))
        service = api.Service(worker)
        for value in ({'arguments': {'operation': 'check_config'}, 'capability': 'forged'},
                      {'arguments': {'operation': 'check_config'}, 'capability': 'forged', 'mode': 'default'}):
            self.assertTrue(service.dispatch(value)['approval_required'])

    def test_dashboard_backup_failure_prevents_save(self):
        core = api.Core(SimpleNamespace())
        with patch.object(core, 'backup_dashboard', side_effect=OSError('synthetic')):
            with patch.object(core, 'ws') as ws:
                with self.assertRaises(OSError):
                    core.execute({'operation': 'save_dashboard', 'target': 'sample', 'data': {'config': {}}})
        ws.assert_not_called()

    def test_flow_result_never_echoes_credentials_or_raw_errors(self):
        core = api.Core(SimpleNamespace())
        with patch.object(core, 'rest', return_value={'flow_id': 'sample', 'type': 'form', 'step_id': 'user',
                                                    'data': {'password': 'SYNTHETIC_PRIVATE_VALUE'}}):
            result = core.execute({'operation': 'flow_start', 'target': 'sample', 'data': {}})
        self.assertEqual(result, {'flow_id': 'sample', 'type': 'form', 'step_id': 'user'})
