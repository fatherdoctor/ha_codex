"""Second peer review regressions; only synthetic Core forms and effects."""
import json
import unittest
from unittest.mock import patch
import test_permission_overrides as policy
import test_permission_overrides_runtime as runtime
from permission_overrides import ApprovalRequired, MARKERS


class PeerPolicy(unittest.TestCase):
    setUp = policy.OverridePolicyTests.setUp
    dispatch = policy.OverridePolicyTests.dispatch
    enable = policy.OverridePolicyTests.enable

    def test_selected_external_guard_checks_only_direct_targets(self):
        self.enable(bigger=True, security=True)
        self.rules = MARKERS[0] + '\n- Do not publish content or changes externally.\n' + MARKERS[1]
        for domain, service in [('camera', 'turn_on'), ('lock', 'unlock'), ('cover', 'open_cover'),
                                ('alarm_control_panel', 'alarm_disarm')]:
            with self.subTest(domain=domain):
                self.assertTrue(self.dispatch(operation='service', service=domain + '.' + service,
                    target=[domain + '.sample'])['approval_required'])
        self.assertEqual(self.backend.writes, [])
        for domain, service in [('scene', 'turn_on'), ('script', 'turn_on'), ('automation', 'trigger')]:
            with self.subTest(domain=domain):
                self.assertTrue(self.dispatch(operation='service', service=domain + '.' + service,
                    target=[domain + '.sample'])['ok'])

    def test_receipts_identify_targets_without_private_values_or_audit_targets(self):
        self.enable(bigger=True)
        cases = [('reload', 'automation', {}), ('update_entity', 'light.sample', {'name': 'PRIVATE_DISPLAY_CANARY'}),
                 ('rename_entity', 'light.sample', {'new_entity_id': 'light.renamed'}),
                 ('delete_entity', 'light.sample', {}), ('delete_script', 'script.sample', {}),
                 ('update_device', 'sample', {'name_by_user': 'PRIVATE_DISPLAY_CANARY'}),
                 ('save_dashboard', '', {'config': {'title': 'PRIVATE_DISPLAY_CANARY'}})]
        for operation, target, data in cases:
            with self.subTest(operation=operation):
                result = self.dispatch(operation=operation, target=target, data=data)
                self.assertTrue(result['ok'])
                self.assertIn(target or 'default', result['performed'])
                if operation == 'rename_entity': self.assertIn('light.renamed', result['performed'])
                self.assertNotIn('PRIVATE_DISPLAY_CANARY', json.dumps(result))
        self.assertNotIn('light.sample', json.dumps(self.audit))
        self.assertNotIn('PRIVATE_DISPLAY_CANARY', json.dumps(self.audit))


class PeerFlows(unittest.TestCase):
    setUp = runtime.CoreEndpoints.setUp

    def form(self, fields=None, **extra):
        return {'handler': 'sample', 'flow_id': 'synthetic', 'type': 'form', 'step_id': 'user',
                'data_schema': fields if fields is not None else [{'name': 'port', 'type': 'integer', 'required': True}], **extra}

    def test_form_and_rejected_nonsecret_submission_return_only_bounded_descriptors(self):
        value = self.form(errors={'port': 'invalid_port'}, description_placeholders={'value': 'PRIVATE_CANARY'})
        value['data_schema'][0].update(default='PRIVATE_CANARY', description='PRIVATE_CANARY')
        with patch.object(self.core, 'rest', return_value=value):
            result = self.core.execute({'operation': 'flow_start', 'target': 'sample'})
        self.assertEqual(result['fields'], [{'name': 'port', 'type': 'integer', 'required': True}])
        self.assertEqual(result['errors'], {'port': 'invalid_port'})
        rejected = self.form(errors={'port': 'invalid_port'}, data={'port': 'PRIVATE_CANARY'})
        with patch.object(self.core, 'rest', side_effect=[self.form(), rejected]) as rest:
            result = self.core.execute({'operation': 'flow_continue', 'target': 'synthetic', 'data': {'port': 'PRIVATE_CANARY'}})
        self.assertEqual([c.args[0] for c in rest.call_args_list], ['GET', 'POST'])
        self.assertEqual(result['errors'], {'port': 'invalid_port'})
        self.assertNotIn('PRIVATE_CANARY', json.dumps(result))

    def test_password_secret_and_unknown_fields_are_never_submitted(self):
        for field in [{'name': 'password', 'type': 'string', 'required': True},
                      {'name': 'value', 'selector': {'text': {'type': 'password'}}, 'required': True},
                      {'name': 'value', 'type': 'string', 'secret': True},
                      {'name': 'api_key', 'type': 'string'},
                      {'name': 'value', 'selector': {'object': {}}}]:
            with self.subTest(field=field), patch.object(self.core, 'rest', return_value=self.form([field])) as rest:
                result = self.core.execute({'operation': 'flow_continue', 'target': 'synthetic', 'data': {field['name']: 'PRIVATE_CANARY'}})
                rest.assert_called_once_with('GET', 'config/config_entries/flow/synthetic')
                self.assertIn('Home Assistant', result['handoff'])
                self.assertNotIn('PRIVATE_CANARY', json.dumps(result))
        for fields, data in [([], {'password': 'PRIVATE_CANARY'}),
                             ([{'name': 'port', 'type': 'integer'}], {'unknown': 'PRIVATE_CANARY'})]:
            with patch.object(self.core, 'rest', return_value=self.form(fields)) as rest:
                with self.assertRaises(ApprovalRequired):
                    self.core.execute({'operation': 'flow_continue', 'target': 'synthetic', 'data': data})
                self.assertEqual(rest.call_count, 1)

    def test_oversized_malformed_and_reflected_metadata_never_leak(self):
        for schema in (None, {}, [{}], [{'name': 'port', 'type': 'string'}] * 65):
            form = self.form(); form['data_schema'] = schema
            with patch.object(self.core, 'rest', return_value=form) as rest:
                with self.assertRaises(ApprovalRequired):
                    self.core.execute({'operation': 'flow_continue', 'target': 'synthetic', 'data': {}})
                self.assertEqual(rest.call_count, 1)
        rejected = self.form(errors={'base': 'private_canary', 'port': 'invalid_port'}, step_id='private_canary')
        with patch.object(self.core, 'rest', side_effect=[self.form(), rejected]):
            result = self.core.execute({'operation': 'flow_continue', 'target': 'synthetic', 'data': {'port': 'private_canary'}})
        self.assertNotIn('private_canary', json.dumps(result))
