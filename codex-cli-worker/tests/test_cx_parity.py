"""Parity: owner-confirmed device work is allowed; escalation protections stay on."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gate_rules
import remote_control
import server


class ParityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.rules = gate_rules.load_rules()
        options = self.root / 'options.json'
        options.write_text(json.dumps({'refuse_categories': True}))
        for obj, name, value in ((gate_rules, 'OPTIONS_PATH', options),
                                 (gate_rules, 'CONFIG_ROOT', self.config), (server, 'CONFIG_ROOT', self.config)):
            mock = patch.object(obj, name, value, create=True)
            mock.start()
            self.addCleanup(mock.stop)

    def patch_call(self, text):
        body = '*** Begin Patch\n*** Add File: sample.yaml\n' + ''.join('+' + l + '\n' for l in text.splitlines()) + '*** End Patch\n'
        return gate_rules.check_tool_call('PreToolUse', {'tool_input': {'patch': body}}, False, self.rules)

    def test_category_settings_removed(self):
        import yaml
        config = yaml.safe_load((ROOT / 'config.yaml').read_text())
        for options in (config['options'], config['schema'], server.DEFAULT_OPTIONS):
            self.assertNotIn('refuse_categories', options)
        self.assertNotIn('refuse_categories', (ROOT / 'translations/en.yaml').read_text())

    def test_security_list_has_no_device_or_message_categories(self):
        for key in ('refused_domains', 'device_actions', 'message_script_words', 'garage_cover_words'):
            self.assertNotIn(key, self.rules)
        self.assertTrue(self.rules['reaches_anything'])

    def test_legacy_option_cannot_reenable_device_refusals(self):
        for action in ('lock.unlock', 'alarm_control_panel.alarm_disarm', 'cover.open_cover',
                       'camera.turn_off', 'notify.sample', 'tts.speak', 'script.notify_sample'):
            with self.subTest(action=action):
                text = '- action: ' + action + '\n'
                self.assertEqual(gate_rules.find_refused(text, self.rules), [])
                self.assertEqual(self.patch_call(text), '')

    def test_device_edits_are_kept_and_watch_does_not_refuse_them(self):
        target = self.config / 'sample.yaml'
        target.write_text('- action: light.turn_on\n')
        watch = remote_control.ConfigWatch(self.config, self.root / 'baseline.json')
        watch.take_baseline()
        target.write_text('- action: lock.unlock\n')
        self.assertEqual(watch.new_findings(), [])
        run = self.root / 'run'
        run.mkdir()
        self.assertEqual(server.refuse_planted_edits(run, {'added': ['sample.yaml']}, {}), [])
        self.assertTrue(target.exists())

    def test_template_target_change_is_normal_in_scan_hook_exchange_and_watch(self):
        before = '- alias: sample\n  trigger: old\n  action: light.turn_on\n  target:\n    entity_id: "{{ trigger.entity_id }}"\n'
        after = before.replace('old', 'new')
        target = self.config / 'sample.yaml'
        target.write_text(before)
        manifest = {'sample.yaml': {'sha256': server.file_hash(target)}}
        run = self.root / 'run'
        backup = run / server.BACKUP_DIR / 'sample.yaml'
        backup.parent.mkdir(parents=True)
        backup.write_text(before)
        watch = remote_control.ConfigWatch(self.config, self.root / 'baseline.json')
        watch.take_baseline()
        target.write_text(after)
        self.assertEqual(gate_rules.new_refused(before, after, self.rules), [])
        self.assertEqual(self.patch_call(after), '')
        self.assertEqual(watch.new_findings(), [])
        self.assertEqual(server.refuse_planted_edits(run, {'changed': ['sample.yaml']}, manifest), [])
        self.assertEqual(target.read_text(), after)

    def test_templated_action_stays_checked(self):
        text = '- action: "{{ selected_action }}"\n'
        self.assertTrue(self.patch_call(text))
        self.assertTrue(gate_rules.new_refused('', text, self.rules))
        target = self.config / 'sample.yaml'
        target.write_text('- action: light.turn_on\n')
        watch = remote_control.ConfigWatch(self.config, self.root / 'baseline.json')
        watch.take_baseline()
        target.write_text(text)
        self.assertTrue(watch.new_findings())
        run = self.root / 'run'
        run.mkdir()
        self.assertEqual(server.refuse_planted_edits(run, {'added': ['sample.yaml']}, {})[0]['undone'], 'removed')

    def test_escalation_credentials_and_policy_checks_stay_on(self):
        for command in ('cat /proc/1/environ', 'echo $SUPERVISOR_TOKEN', 'cat secrets.yaml', 'cat .storage/auth'):
            with self.subTest(command=command):
                self.assertTrue(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'command': command}}, True, self.rules))
        self.assertTrue(gate_rules.check_tool_call('PermissionRequest', {}, True, self.rules))
        self.assertTrue(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'sandbox_permissions': 'require_escalated'}}, True, self.rules))
        patch_text = '*** Begin Patch\n*** Update File: secrets.yaml\n+{}\n*** End Patch\n'
        self.assertTrue(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'patch': patch_text}}, False, self.rules))
        import approvals
        self.assertTrue(approvals.touches_protected_path('/data/options.json'))
        self.assertTrue(approvals.command_names_protected_path('edit /usr/share/ha_codex/policy.json'))

    def test_no_house_practice_is_bundled(self):
        self.assertFalse((ROOT / 'policy/house-rules.md').exists())
        self.assertIn('/config/AGENTS.md', (ROOT / 'server.py').read_text())


class DeviceCommandSpeedTests(unittest.TestCase):
    """A plain device command finds its entity through the Home Assistant tools, not through files."""

    STATES = [
        {'entity_id': 'light.office_overhead', 'state': 'off', 'attributes': {'friendly_name': 'Office Overhead', 'brightness': None}},
        {'entity_id': 'light.office_overhead_left_bulb', 'state': 'off', 'attributes': {'friendly_name': 'Office Overhead Left Bulb'}},
        {'entity_id': 'switch.office_overhead_fan', 'state': 'on', 'attributes': {'friendly_name': 'Office Overhead Fan'}},
        {'entity_id': 'light.kitchen_ceiling', 'state': 'on', 'attributes': {'friendly_name': 'Kitchen Ceiling'}},
        {'entity_id': 'sensor.office_overhead_api_key', 'state': 'synthetic-value', 'attributes': {'friendly_name': 'Office Overhead Key'}},
        {'entity_id': 'input_text.office_overhead_note', 'state': 'synthetic-value', 'attributes': {'mode': 'password'}},
        {'entity_id': 'light.unnamed_office_overhead', 'state': 'on', 'attributes': None},
        'not a state row',
    ]

    def find(self, **arguments):
        import verification
        engine = verification.Verification(unittest.mock.Mock(ha_token=lambda: 'synthetic'))
        with patch.object(verification, 'fetch_states', return_value=json.loads(json.dumps(self.STATES))) as fetch:
            result = engine.find(arguments)
        fetch.assert_called_once_with('/states', 'synthetic')
        return result

    def test_rules_send_a_device_command_to_the_home_assistant_tools(self):
        rules = server.PAGE_INSTRUCTIONS
        line = next(line for line in rules.splitlines() if line.startswith('For a device command'))
        for step in ('home_assistant.verify find', 'home_assistant_actions.act', 'home_assistant.verify entity'):
            self.assertIn(step, line)
        self.assertLess(line.index('verify find'), line.index('actions.act'))
        self.assertIn('Do not search files under /config or .storage or run shell commands to find an entity', line)
        self.assertIn('{"operation":"find","name":"kitchen ceiling","domain":"light"}', rules)

    def test_rules_no_longer_ask_for_the_owner_file_in_each_message(self):
        rules = server.PAGE_INSTRUCTIONS
        self.assertNotIn('check for it in each message', rules)
        self.assertIn('Owner workspace instructions: if /config/AGENTS.md exists, follow it.', rules)
        self.assertIn('read the file again only when its owner_instructions text says to', rules)

    def test_exchange_info_says_when_the_owner_file_changed_or_that_it_is_gone(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'AGENTS.md'
            with patch.object(server, 'AGENTS_PATH', target):
                self.assertIn('There is no /config/AGENTS.md now', server.exchange_info('chat', None)['owner_instructions'])
                target.write_text('Synthetic owner rules')
                os.utime(target, (1_800_000_000, 1_800_000_000))
                first = server.exchange_info('chat', None)['owner_instructions']
                self.assertIn('last changed at 2027-01-15T08:00:00Z', first)
                self.assertEqual(first, server.exchange_info('chat', None)['owner_instructions'])
                os.utime(target, (1_800_000_060, 1_800_000_060))
                self.assertNotEqual(first, server.exchange_info('chat', None)['owner_instructions'])

    def test_find_returns_the_entity_for_the_words_a_person_uses(self):
        result = self.find(name='Office overhead', domain='light')
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['matches'][0], {'entity_id': 'light.office_overhead', 'name': 'Office Overhead', 'state': 'off'})
        self.assertEqual([row['entity_id'] for row in result['matches']],
                         ['light.office_overhead', 'light.unnamed_office_overhead', 'light.office_overhead_left_bulb'])
        self.assertEqual((result['total_matches'], result['truncated']), (3, False))

    def test_find_without_a_domain_covers_every_domain_and_hides_credentials(self):
        ids = [row['entity_id'] for row in self.find(name='overhead office')['matches']]
        # The device itself comes before the entities that add a word to its name.
        self.assertEqual(ids[0], 'light.office_overhead')
        self.assertIn('switch.office_overhead_fan', ids)
        self.assertNotIn('sensor.office_overhead_api_key', ids)
        self.assertNotIn('input_text.office_overhead_note', ids)
        self.assertNotIn('synthetic-value', json.dumps(self.find(name='overhead')))

    def test_find_caps_its_answer_and_reports_no_match(self):
        import verification
        many = [{'entity_id': f'light.sample_{n:02d}', 'state': 'on', 'attributes': {}} for n in range(25)]
        with patch.object(self, 'STATES', many):
            result = self.find(name='sample')
        self.assertEqual(len(result['matches']), verification.MAX_FIND_MATCHES)
        self.assertEqual((result['total_matches'], result['truncated']), (25, True))
        self.assertEqual(self.find(name='garage')['matches'], [])
        self.assertIn('No entity matches', self.find(name='garage')['message'])

    def test_find_refuses_bad_arguments_before_any_read(self):
        import verification
        engine = verification.Verification(unittest.mock.Mock(ha_token=lambda: 'synthetic'))
        with patch.object(verification, 'fetch_states') as fetch:
            for arguments in ({}, {'name': ''}, {'name': '...'}, {'name': 'x' * 121}, {'name': 7},
                              {'name': 'office', 'domain': 'Light.x'}, {'name': 'office', 'domain': 5}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    engine.find(arguments)
        fetch.assert_not_called()

    def test_find_is_offered_by_the_tool_and_stays_a_read(self):
        import verification_mcp
        schema = verification_mcp.TOOL['inputSchema']['properties']
        self.assertIn('find', schema['operation']['enum'])
        self.assertIn('name', schema)
        self.assertIn('No service calls', verification_mcp.TOOL['description'])
        self.assertIn('do not search files', verification_mcp.TOOL['description'])


class ApprovalParityTests(unittest.TestCase):
    def test_preserved_action_confirmation_describes_target_and_effect(self):
        import approvals
        record = {'kind': 'command', 'command': '/api/services/light/turn_on -d light.sample',
                  'cwd': '/config', 'can_allow': True, 'changes': []}
        text = approvals.confirmation(record)
        self.assertEqual(text['what'], 'Run the Home Assistant action light.turn_on.')
        self.assertEqual(text['where'], 'light.sample.')
        self.assertEqual(text['effect'], 'This turns on light.sample.')
        record['command'] = '/api/services/automation/reload'
        self.assertEqual(approvals.confirmation(record)['what'], 'Reload the automations.')

    def test_preserved_file_confirmation_explains_when_changes_apply(self):
        import approvals
        record = {'kind': 'file_change', 'can_allow': True, 'changes': [
            {'path': 'automations.yaml', 'kind': 'update'}, {'path': 'scenes.yaml', 'kind': 'add'}]}
        text = approvals.confirmation(record)
        self.assertEqual(text['what'], 'Create and change 2 files.')
        self.assertIn('automations change once automations are reloaded', text['effect'])
        self.assertIn('scenes change once scenes are reloaded', text['effect'])

    def test_protected_request_confirmation_does_not_offer_a_change(self):
        import approvals
        record = {'kind': 'command', 'command': 'edit protected policy', 'cwd': '/config',
                  'can_allow': False, 'changes': [], 'note': 'Protected policy cannot be changed.'}
        self.assertTrue(approvals.confirmation(record)['effect'].startswith('This request cannot be allowed here.'))

    def test_device_command_has_plain_words_and_requires_its_own_decision(self):
        import test_approvals as fixtures
        fixture = fixtures.ApprovalTests('runTest')
        fixture.setUp()
        try:
            approval_id = fixture.request(command='ha services call lock unlock', cwd='/config')
            record = fixture.records[-1]
            self.assertTrue(record['can_allow'])
            self.assertEqual(record['status'], 'pending')
            self.assertEqual(set(record['confirmation']), {'what', 'where', 'effect'})
            self.assertTrue(all(record['confirmation'].values()))
            self.assertNotEqual(fixture.writer.messages()[-1].get('result'), {'decision': 'accept'})
            fixture.session.decide(approval_id, 'allow')
            self.assertEqual(fixture.writer.messages()[-1]['result'], {'decision': 'accept'})
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
