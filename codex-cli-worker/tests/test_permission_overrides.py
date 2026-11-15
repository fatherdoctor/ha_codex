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
        self.expansion = {'device_id': ['light.sample'], 'area_id': ['light.sample'], 'label_id': ['light.sample']}

    def resolve(self, selector, service_domain=None):
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

    def test_disabled_groups_are_named_in_refusal_diagnostics(self):
        cases = (
            ('apply', {'operation': 'reload', 'target': 'automation'}),
            ('everyday', {'operation': 'service', 'service': 'light.turn_on', 'target': ['light.sample']}),
            ('organize', {'operation': 'update_entity', 'target': 'light.sample', 'data': {'name': 'Sample'}}),
            ('bigger', {'operation': 'backup'}),
            ('security', {'operation': 'service', 'service': 'lock.unlock', 'target': ['lock.sample']}),
        )
        for group, request in cases:
            with self.subTest(group=group):
                self.enable(**{group: False})
                self.audit.clear()
                result = self.engine.dispatch(request, self.authority)
                self.assertTrue(result.get('approval_required', False), result)
                self.assertEqual(self.backend.writes, [])
                self.assertEqual([(row['group'], row['outcome']) for row in self.audit],
                                 [(group, 'refused')])
                self.assertNotIn('light.sample', json.dumps(self.audit))
                self.assertNotIn('lock.sample', json.dumps(self.audit))

    def test_malformed_requests_keep_unknown_diagnostic_group(self):
        for request in ({'operation': 'unknown'},
                        {'operation': 'service', 'service': 'unsupported'},
                        {'operation': 'service', 'service': 'light.turn_on', 'extra': True}):
            with self.subTest(request=request):
                self.audit.clear()
                self.assertTrue(self.engine.dispatch(request, self.authority).get('approval_required', False))
                self.assertEqual(self.audit[-1]['group'], 'unknown')
        self.assertEqual(self.backend.writes, [])

    def test_read_only_remote_custom_unknown_and_forged_modes_never_write(self):
        for authority in (Authority(), Authority('read-only', 'on-request', 'user', 'worker'),
                          Authority('workspace-write', 'never', 'user', 'worker'),
                          Authority('workspace-write', 'on-request', 'user', 'remote')):
            result = self.engine.dispatch({'operation': 'check_config'}, authority)
            self.assertTrue(result.get('approval_required', False))
        self.assertTrue(self.dispatch(operation='check_config', sandbox='workspace-write').get('approval_required', False))
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
                self.assertTrue(self.engine.dispatch(request, self.authority).get('approval_required', False))
                self.enable(**{group: True})
                self.assertTrue(self.engine.dispatch(request, self.authority)['ok'])

    def test_owner_guard_rails_win_when_security_group_is_on(self):
        self.enable(security=True)
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        self.assertFalse(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_outside_instructions_do_not_change_the_marked_block_policy(self):
        self.enable(security=True)
        for text in ('Keep locks unchanged.', 'Before each change wait for my yes.',
                     'Ask first when my request is unclear.', MARKERS[0] + '\n' + MARKERS[1] + '\nKeep locks unchanged.'):
            with self.subTest(text=text):
                self.rules = text
                self.assertTrue(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['ok'])

    def test_selected_block_keeps_domain_specific_limits(self):
        self.enable(security=True)
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        self.assertTrue(self.dispatch(operation='service', service='cover.open_cover', target=['cover.sample'])['ok'])

    def test_custom_malformed_or_additional_owner_limits_require_approval(self):
        for text in (MARKERS[0],
                     MARKERS[0] + '\n- Custom rule\n' + MARKERS[1]):
            with self.subTest(text=text):
                self.rules = text
                self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])


    def test_device_and_area_expansion_cannot_hide_a_security_target(self):
        self.backend.expansion['area_id'] = ['light.sample', 'lock.sample']
        self.assertTrue(self.dispatch(operation='service', service='light.turn_on', target={'area_id': 'sample'})['ok'])
        self.backend.writes.clear()
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
    def test_first_use_note_explains_confirmation_without_confirming_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config'
            config.mkdir()
            path = config / 'AGENTS.md'
            path.write_text('Synthetic owner instructions.')
            service = api.Service(SimpleNamespace(DATA_ROOT=root, CONFIG_ROOT=config))
            note = service.guard_note()
            self.assertIn('start using these groups', note)
            self.assertIn('Review and confirm', note)
            self.assertFalse(service.rules_path().exists())
            with self.assertRaises(ApprovalRequired):
                service.guards()
            service.confirm_rules()
            self.assertEqual(service.guard_note(), '')
            path.write_text('Changed synthetic owner instructions.')
            self.assertIn('changed since confirmation', service.guard_note())
            self.assertNotIn('start using these groups', service.guard_note())
            with self.assertRaises(ApprovalRequired):
                service.guards()

    def test_missing_instructions_need_no_first_use_confirmation_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = api.Service(SimpleNamespace(DATA_ROOT=root, CONFIG_ROOT=root / 'config'))
            self.assertEqual(service.guard_note(), '')
            self.assertFalse(service.rules_path().exists())

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
            self.assertTrue(service.dispatch(value).get('approval_required', False))

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
                                                    'data_schema': [], 'data': {'password': 'SYNTHETIC_PRIVATE_VALUE'}}):
            result = core.execute({'operation': 'flow_start', 'target': 'sample', 'data': {}})
        self.assertEqual(result, {'flow_id': 'sample', 'type': 'form', 'step_id': 'user', 'fields': [], 'errors': {}})
