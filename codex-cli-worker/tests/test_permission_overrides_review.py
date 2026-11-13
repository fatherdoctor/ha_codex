"""Full-review regressions use synthetic rules, Core replies and configuration only."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_permission_overrides as policy_tests
from test_permission_overrides import Backend
import test_permission_overrides_runtime as runtime_tests
from permission_overrides import ApprovalRequired, Authority, DEFAULTS, Engine, MARKERS, bounded_json, guard_rules
import permission_overrides_api as api

# Verbatim public kit starting sets, not private owner instructions.
CAREFUL = '''Before each change, say what you will change and wait for my yes. Back up before editing. After a configuration
change, run the Home Assistant configuration check and report the result. If a check cannot run or fails, say so.'''
DIRECT = '''My direct request is the go-ahead for the change I asked for. Make it, then say in one line what changed. Ask first
when my request is unclear, a change goes beyond what I asked, or before deleting, restarting, firmware or version
changes. Back up before editing. After a configuration change, run the Home Assistant configuration check and report
the result. If a check cannot run or fails, say so.'''
LOCK_RULE = MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1]


class ConfirmedRules(unittest.TestCase):
    setUp = runtime_tests.RuntimeOverrides.setUp
    dispatch = runtime_tests.RuntimeOverrides.dispatch

    def prepare(self):
        self.path = self.config / 'AGENTS.md'
        self.path.write_text(LOCK_RULE)
        self.service.settings().save({**DEFAULTS, 'security': True})
        self.service.confirm_rules()
        self.service.begin('task', 'synthetic-cap', self.options)

    def test_confirmed_copy_and_digest_are_private_and_guard_the_turn(self):
        self.prepare()
        value = json.loads(self.service.rules_path().read_text())
        self.assertEqual(value['text'], LOCK_RULE)
        self.assertEqual(len(value['sha256']), 64)
        self.assertEqual(self.service.rules_path().stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['approval_required'])
        self.assertEqual(self.backend.writes, [])

    def test_edit_delete_and_symlink_refuse_and_taint_the_rest_of_the_exchange(self):
        for change in ('edit', 'delete', 'symlink'):
            with self.subTest(change=change):
                self.prepare()
                self.assertTrue(self.dispatch(operation='check_config')['ok'])
                self.backend.writes.clear()
                if change == 'edit':
                    self.path.write_text('')
                else:
                    self.path.unlink()
                    if change == 'symlink':
                        target = self.root / 'synthetic-rules'
                        target.write_text('')
                        self.path.symlink_to(target)
                self.assertFalse(self.dispatch(operation='check_config')['ok'])
                if self.path.is_symlink():
                    self.path.unlink()
                self.path.write_text(LOCK_RULE)
                self.assertFalse(self.dispatch(operation='check_config')['ok'])
                self.assertEqual(self.backend.writes, [])
                self.service.begin('task', 'synthetic-cap', self.options)
                self.assertTrue(self.dispatch(operation='check_config')['ok'])
                self.backend.writes.clear()

    def test_changed_rules_across_exchanges_need_confirmation(self):
        for change in ('edit', 'delete', 'symlink'):
            with self.subTest(change=change):
                self.prepare()
                if change == 'edit':
                    self.path.write_text('')
                else:
                    self.path.unlink()
                    if change == 'symlink':
                        self.path.symlink_to(self.root / 'absent-synthetic-rules')
                self.service.begin('task', 'synthetic-cap', self.options)
                self.assertFalse(self.dispatch(operation='check_config')['ok'])
                self.assertTrue(self.service.guard_note())
                if self.path.is_symlink():
                    with self.assertRaises(ApprovalRequired):
                        self.service.confirm_rules()
                    self.path.unlink()
                self.service.confirm_rules()
                self.service.begin('task', 'synthetic-cap', self.options)
                self.assertTrue(self.dispatch(operation='check_config')['ok'])
                self.backend.writes.clear()

    def test_confirmation_does_not_authorize_an_already_unconfirmed_exchange(self):
        self.prepare()
        self.path.write_text('new synthetic instructions')
        self.service.begin('task', 'synthetic-cap', self.options)
        self.service.confirm_rules()
        self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])
        self.service.begin('task', 'synthetic-cap', self.options)
        self.assertTrue(self.dispatch(operation='check_config')['ok'])

    def test_rule_change_during_audit_refuses_before_the_write(self):
        path = self.config / 'AGENTS.md'
        path.write_text('')
        self.service.confirm_rules()
        self.service.begin('task', 'synthetic-cap', self.options)
        audit = self.service.audit
        def change_after_audit(task_id, event):
            audit(task_id, event)
            if event['outcome'] == 'requested':
                path.write_text(LOCK_RULE)
        with patch.object(self.service, 'audit', side_effect=change_after_audit):
            self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_stale_confirmation_digest_does_not_save(self):
        self.prepare()
        import os
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(ApprovalRequired):
            self.service.rules_snapshot()
        self.path.unlink()
        self.path.write_text(LOCK_RULE)
        old = self.service.rules_snapshot()['sha256']
        self.path.write_text('new synthetic instructions')
        with self.assertRaises(ApprovalRequired):
            self.service.confirm_rules(old)
        self.assertEqual(json.loads(self.service.rules_path().read_text())['text'], LOCK_RULE)

    def test_selected_block_note_identifies_the_limit(self):
        self.prepare()
        self.assertIn('lock', self.service.guard_note())
        self.assertIn('- Do not lock or unlock locks.', self.service.guard_note())

    def test_multiple_selected_limits_name_their_affected_groups(self):
        self.prepare()
        self.path.write_text(MARKERS[0] + '\n- Do not lock or unlock locks.\n'
                             '- Do not publish content or changes externally.\n' + MARKERS[1])
        self.service.confirm_rules()
        note = self.service.guard_note()
        self.assertIn('locks. Suspended groups/actions: Security and people; Organize for matching entities', note)
        self.assertIn('externally. Suspended groups/actions: Security and people; Organize; Bigger changes', note)
        self.assertIn('Everyday controls', note)
        self.assertIn('Apply checked changes', note)

    def test_unattended_and_unverified_remote_origins_require_normal_approval(self):
        self.worker.tasks = {'task': {}}
        # Codex 0.166 does not supply a trusted person-started origin for remote turns.
        # A model/client source label cannot grant groups; only worker-owned chat turns qualify.
        for source in ('chat', 'schedule', 'lead', 'unknown', 'remote'):
            with self.subTest(source=source):
                self.worker.tasks['task']['task_source'] = source
                self.service.begin('task', 'synthetic-cap', self.options)
                self.assertEqual(self.dispatch(operation='check_config')['ok'], source == 'chat')
        self.assertEqual(len(self.backend.writes), 1)

    def test_permission_mode_read_only_reaches_the_service_without_writes(self):
        from test_server import server
        with patch.object(server, 'read_options', return_value=dict(server.DEFAULT_OPTIONS)), \
                patch.dict(server.tasks, {'review-readonly': {'permission_mode': 'read-only'}}):
            self.worker.task_options = lambda _: server.task_options('review-readonly')
            options = self.worker.task_options('task')
            self.assertEqual(options['codex_sandbox'], 'read-only')
            self.service.begin('task', 'synthetic-cap', options)
            self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])


class GuardAndMutationRegressions(unittest.TestCase):
    setUp = policy_tests.OverridePolicyTests.setUp
    dispatch = policy_tests.OverridePolicyTests.dispatch
    enable = policy_tests.OverridePolicyTests.enable

    def test_both_kit_sets_only_apply_the_marked_block(self):
        self.enable(security=True)
        for text in (CAREFUL, DIRECT):
            with self.subTest(text=text[:20]):
                self.rules = text
                self.assertTrue(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['ok'])
                self.rules = text + '\n' + LOCK_RULE
                self.assertFalse(self.dispatch(operation='service', service='lock.unlock', target=['lock.sample'])['ok'])
                self.assertTrue(self.dispatch(operation='service', service='light.turn_on', target=['light.sample'])['ok'])

    def test_external_limit_refuses_nonservice_publication(self):
        self.enable(bigger=True)
        self.rules = MARKERS[0] + '\n- Do not publish content or changes externally.\n' + MARKERS[1]
        self.assertFalse(self.dispatch(operation='save_dashboard', target='', data={'config': {}})['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_restricted_security_entities_cannot_be_renamed_updated_or_deleted(self):
        for operation, data in [('rename_entity', {'new_entity_id': 'lock.renamed'}), ('update_entity', {'name': 'Synthetic'}), ('delete_entity', {})]:
            for restricted in (True, False):
                with self.subTest(operation=operation, restricted=restricted):
                    self.enable(bigger=True, security=restricted)
                    self.rules = LOCK_RULE if restricted else ''
                    self.assertFalse(self.dispatch(operation=operation, target='lock.sample', data=data)['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_template_guard_itself_rejects_templates(self):
        with self.assertRaisesRegex(ApprovalRequired, 'Dynamic templates'):
            bounded_json({'value': '{{ synthetic }}'})

    def test_indirect_depth_budget_refuses_before_reading_the_next_definition(self):
        self.enable(bigger=True)
        for n in range(18):
            self.backend.definitions[f'script.s{n}'] = {'sequence': [{'action': f'script.s{n+1}'}]}
        with patch.object(self.backend, 'definition', wraps=self.backend.definition) as read:
            result = self.dispatch(operation='service', service='script.turn_on', target=['script.s0'])
        self.assertFalse(result['ok'])
        self.assertNotIn(unittest.mock.call('script.s16'), read.call_args_list)
        self.assertEqual(self.backend.writes, [])

    def test_mixed_domain_selectors_keep_only_entities_of_the_service_domain(self):
        for selector in ('area_id', 'device_id', 'label_id'):
            with self.subTest(selector=selector):
                self.backend.expansion[selector] = ['light.sample', 'switch.sample', 'lock.sample', 'sensor.api_key']
                result = self.dispatch(operation='service', service='light.turn_on', target={selector: 'sample'})
                self.assertTrue(result['ok'])
                self.assertIn('light.sample', result['performed'])
                self.assertNotIn('lock.sample', result['performed'])
                self.assertEqual(self.backend.writes[-1]['target'], ['light.sample'])

    def test_definition_change_during_audit_refuses_loaded_action_and_reload(self):
        safe = {'actions': [{'action': 'light.turn_on', 'target': {'entity_id': 'light.sample'}}]}
        unsafe = {'actions': [{'action': 'lock.unlock', 'target': {'entity_id': 'lock.sample'}}]}
        for reload in (False, True):
            with self.subTest(reload=reload):
                self.backend.writes.clear()
                self.backend.definitions['automation.sample'] = deepcopy(safe)
                changes = {'new': deepcopy(safe)}
                self.backend.reload_changes = lambda domain: deepcopy(changes)
                def audit(event):
                    if event['outcome'] == 'requested':
                        self.backend.definitions['automation.sample'] = deepcopy(unsafe)
                        changes['new'] = deepcopy(unsafe)
                self.engine.observer = audit
                request = ({'operation': 'reload', 'target': 'automation'} if reload else
                           {'operation': 'service', 'service': 'automation.trigger', 'target': ['automation.sample']})
                result = self.dispatch(**request)
                self.assertFalse(result['ok'])
                self.assertTrue(result['approval_required'])
                self.assertEqual(self.backend.writes, [])

    def test_reload_checks_only_new_or_changed_definitions_and_rechecks_them(self):
        self.backend.reload_changes = Mock(return_value={'new': {'actions': [{'action': 'lock.unlock', 'target': {'entity_id': 'lock.sample'}}]}})
        self.assertFalse(self.dispatch(operation='reload', target='automation')['ok'])
        self.assertEqual(self.backend.writes, [])
        self.backend.reload_changes.return_value = {'new': {'actions': [{'action': 'light.turn_on', 'target': {'entity_id': 'light.sample'}}]}}
        self.assertTrue(self.dispatch(operation='reload', target='automation')['ok'])
        self.backend.writes.clear()
        self.backend.reload_changes.side_effect = [{}, {'new': {'actions': []}}]
        self.assertFalse(self.dispatch(operation='reload', target='automation')['ok'])
        self.assertEqual(self.backend.writes, [])


class LoadedCore(unittest.TestCase):
    setUp = runtime_tests.CoreEndpoints.setUp

    def test_loaded_automation_and_script_win_over_opposite_disk_definitions(self):
        for domain in ('automation', 'script'):
            for loaded_service, allowed in [('lock.unlock', False), ('light.turn_on', True)]:
                with self.subTest(domain=domain, loaded=loaded_service):
                    key = 'sequence' if domain == 'script' else 'actions'
                    target = loaded_service.split('.')[0] + '.sample'
                    loaded = {key: [{'action': loaded_service, 'target': {'entity_id': target}}]}
                    self.core.resolve = lambda selector, **_: selector if isinstance(selector, list) else [selector['entity_id']]
                    writes = []
                    self.core.execute = lambda plan: writes.append(plan)
                    with patch.object(self.core, 'require_entity', return_value={}), patch.object(self.core, 'ws', return_value={'config': loaded}) as ws, patch.object(self.core, 'rest', return_value={key: [{'action': 'light.turn_on' if not allowed else 'lock.unlock'}]}) as disk:
                        engine = Engine(SimpleNamespace(read=lambda: {**DEFAULTS, 'bigger': True}), self.core, lambda: '', lambda _: None)
                        result = engine.dispatch({'operation': 'service', 'service': domain + '.turn_on', 'target': [domain + '.sample']}, Authority('workspace-write', 'on-request', 'user', 'worker'))
                    self.assertEqual(result['ok'], allowed)
                    self.assertEqual(len(writes), int(allowed))
                    ws.assert_called_with(domain + '/config', entity_id=domain + '.sample')
                    disk.assert_not_called()

    def test_registry_expansion_filters_before_target_checks_and_limits(self):
        rows = [{'entity_id': 'light.sample', 'area_id': 'sample'}]
        rows += [{'entity_id': 'sensor.api_key' + str(n), 'area_id': 'sample'} for n in range(100)]
        def ws(kind, **_):
            return rows if kind == 'config/entity_registry/list' else []
        with patch.object(self.core, 'ws', side_effect=ws), patch.object(self.core, 'require_entity') as verify:
            self.assertEqual(self.core.resolve({'area_id': 'sample'}, service_domain='light'), ['light.sample'])
        verify.assert_not_called()  # Engine verifies only the final filtered targets.

    def test_scene_members_come_from_loaded_state(self):
        with patch.object(self.core, 'require_entity', return_value={'attributes': {'entity_id': ['lock.sample']}}), patch.object(self.core, 'rest') as disk:
            self.assertEqual(self.core.definition('scene.sample'), {'entities': {'lock.sample': None}})
        disk.assert_not_called()

    def test_password_mode_helper_refuses_before_registry_lookup(self):
        for domain in ('input_text', 'text'):
            with patch.object(self.core, 'rest', return_value={'entity_id': domain + '.sample', 'attributes': {'mode': 'password'}}), patch.object(self.core, 'ws') as ws:
                with self.assertRaises(ApprovalRequired):
                    self.core.require_entity(domain + '.sample')
                ws.assert_not_called()

    def test_unknown_setup_names_and_urls_are_handed_off(self):
        # Synthetic corpus covers joined spellings, PSKs and URL-bearing fields.
        names = ('noise_psk psk webhook_id cookie authorization authtoken sharedsecret url rtsp_url stream_source '
                 'password passwd passphrase password1 pin pincode api_key apikey access_token token refresh_token '
                 'client_secret secret credential credentials bearer bearer_token auth auth_token session session_id '
                 'session_cookie cert certificate private_key seed signing_key signature sign shared_secret encryption_key '
                 'encryptionkey encryption_noise_psk webhook webhook_url endpoint base_url server_url mqtt_url '
                 'connection_string oauth_code clientkey key authentication').split()
        self.assertEqual(len(names), 54)
        for name in names:
            with self.subTest(name=name):
                form = {'flow_id': 'synthetic', 'type': 'form', 'step_id': 'user', 'data_schema': [{'name': name, 'type': 'string'}]}
                with patch.object(self.core, 'rest', return_value=form) as rest:
                    result = self.core.execute({'operation': 'flow_continue', 'target': 'synthetic', 'data': {name: 'SYNTHETIC_PRIVATE'}})
                self.assertIn('handoff', result)
                self.assertEqual(rest.call_count, 1)
                self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))

    def test_reload_special_files_are_refused_without_waiting_for_a_writer(self):
        import os
        path = self.root / 'synthetic-pipe.yaml'
        os.mkfifo(path)
        with self.assertRaises(ApprovalRequired):
            self.core.read_reload_yaml(path)

    def test_reload_comparison_keeps_unchanged_owner_definitions_and_finds_new_edits(self):
        self.worker.CONFIG_ROOT = self.root
        (self.root / 'configuration.yaml').write_text('script: !include scripts.yaml\n')
        original = {'sequence': [{'action': 'lock.unlock', 'target': {'entity_id': 'lock.sample'}}]}
        import yaml
        (self.root / 'scripts.yaml').write_text(yaml.safe_dump({'old': original, 'new': {'sequence': []}}))
        with patch.object(self.core, 'rest', return_value=[{'entity_id': 'script.old'}]), patch.object(self.core, 'definition', return_value=original):
            self.assertEqual(self.core.reload_changes('script'), {'new': {'sequence': []}})
            (self.root / 'scripts.yaml').write_text(yaml.safe_dump({'old': {'sequence': []}}))
            self.assertEqual(self.core.reload_changes('script'), {'old': {'sequence': []}})
        (self.root / 'configuration.yaml').write_text('script: !include_dir_merge_named scripts\n')
        with self.assertRaises(ApprovalRequired):
            self.core.reload_changes('script')


if __name__ == '__main__':
    unittest.main()
