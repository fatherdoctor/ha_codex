"""Direct-target rules use synthetic backends and Core replies, never a live system."""
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
import permission_overrides_api as api
from permission_overrides import DEFAULTS, Authority, Engine, MARKERS
import test_permission_overrides as policy_tests


class DirectTargets(unittest.TestCase):
    setUp = policy_tests.OverridePolicyTests.setUp
    enable = policy_tests.OverridePolicyTests.enable
    dispatch = policy_tests.OverridePolicyTests.dispatch

    def test_scene_script_and_automation_do_not_inspect_indirect_definitions(self):
        self.enable(bigger=True)
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        self.backend.definition = Mock(side_effect=AssertionError('Indirect definition must not be read'))
        for service, target in (('scene.turn_on', 'scene.sample'), ('script.turn_on', 'script.sample'),
                                ('automation.trigger', 'automation.sample'), ('automation.turn_on', 'automation.sample')):
            with self.subTest(service=service):
                self.assertTrue(self.dispatch(operation='service', service=service, target=[target])['ok'])
        self.backend.definition.assert_not_called()
        self.assertFalse(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['ok'])

    def test_apply_reloads_do_not_read_or_judge_configured_actions(self):
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        self.backend.reload_changes = Mock(side_effect=AssertionError('Reload definitions must not be read'))
        for domain in ('automation', 'script', 'scene', 'template', 'group'):
            with self.subTest(domain=domain):
                self.assertTrue(self.dispatch(operation='reload', target=domain)['ok'])
        self.backend.reload_changes.assert_not_called()
        self.enable(apply=False)
        self.assertFalse(self.dispatch(operation='reload', target='template')['ok'])

    def test_scripted_wrapper_and_unknown_platforms_use_only_the_direct_target(self):
        core = api.Core(SimpleNamespace())
        writes = []
        core.execute = lambda plan: writes.append(plan)
        core.rest = lambda method, path: {'entity_id': 'light.sample', 'attributes': {'entity_id': ['lock.sample']}}
        engine = Engine(self.settings, core, lambda: self.rules, self.audit.append)
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        for platform in ('template', 'script', 'group', 'switch_as_x', 'unknown', None):
            with self.subTest(platform=platform):
                core.ws = Mock(return_value={'platform': platform})
                result = engine.dispatch({'operation': 'service', 'service': 'light.turn_on', 'target': ['light.sample']}, self.authority)
                self.assertTrue(result['ok'], result)
                self.assertTrue(all(call.args[0] == 'config/entity_registry/get' for call in core.ws.call_args_list))
        self.assertEqual(len(writes), 6)

    def test_core_reload_posts_directly_without_reading_files_or_definitions(self):
        core = api.Core(SimpleNamespace())
        core.rest = Mock(return_value={})
        core.ws = Mock(side_effect=AssertionError('A reload has no registry target'))
        engine = Engine(self.settings, core, lambda: self.rules, self.audit.append)
        for domain in ('automation', 'script', 'scene', 'template', 'group'):
            with self.subTest(domain=domain):
                result = engine.dispatch({'operation': 'reload', 'target': domain}, self.authority)
                self.assertTrue(result['ok'], result)
                core.rest.assert_called_with('POST', 'services/' + domain + '/reload', {})
        core.ws.assert_not_called()

    def test_direct_domain_mismatch_and_selected_lock_limit_still_ask(self):
        self.enable(security=True)
        self.assertFalse(self.dispatch(operation='service', service='light.turn_on', target=['lock.sample'])['ok'])
        self.rules = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]
        self.assertFalse(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_direct_targets_are_checked_after_the_audit(self):
        available = {'yes': True}
        def require(target):
            if not available['yes']:
                raise api.ApprovalRequired('Direct entity became unavailable')
            return {'entity_id': target}
        self.backend.require_entity = require
        def audit(event):
            if event['outcome'] == 'requested':
                available['yes'] = False
        self.engine.observer = audit
        self.assertFalse(self.dispatch(operation='service', service='light.turn_on', target=['light.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])


class AuditFlush(unittest.TestCase):
    import test_permission_overrides_runtime as runtime_tests
    setUp = runtime_tests.RuntimeOverrides.setUp
    dispatch = runtime_tests.RuntimeOverrides.dispatch

    def test_failure_reported_by_flush_prevents_the_write(self):
        from unittest.mock import patch
        def fail_flush():
            self.recorder.failures += 1
        with patch.object(self.recorder, 'flush', side_effect=fail_flush):
            self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])
