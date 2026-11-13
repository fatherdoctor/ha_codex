"""Reload checks use temporary configuration and synthetic Core responses, never a live system."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import yaml
from permission_overrides import Authority, DEFAULTS, Engine
import permission_overrides_api as api
from test_permission_overrides_review import LOCK_RULE
import test_permission_overrides as policy_tests
import test_permission_overrides_runtime as runtime_tests


class ReloadChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.core = api.Core(SimpleNamespace(CONFIG_ROOT=self.root))
        self.writes = []
        self.core.execute = lambda plan: self.writes.append(plan)
        self.core.require_entity = lambda target: {'entity_id': target}
        self.core.rest = lambda *args: []
        self.core.ws = lambda *args, **kwargs: None
        self.engine = Engine(SimpleNamespace(read=lambda: DEFAULTS), self.core,
                             lambda: LOCK_RULE, lambda event: None)
        self.authority = Authority('workspace-write', 'on-request', 'user', 'worker')

    def reload(self, domain, allowed=False):
        self.writes.clear()
        result = self.engine.dispatch({'operation': 'reload', 'target': domain}, self.authority)
        self.assertEqual(result['ok'], allowed, result)
        self.assertEqual(len(self.writes), int(allowed))
        if not allowed:
            self.assertTrue(result['approval_required'])
        return result

    def config(self, text):
        (self.root / 'configuration.yaml').write_text(text)

    def test_template_action_entries_and_unreadable_sections_require_approval(self):
        for action_key in ('action', 'actions'):
            for trigger_key in ('trigger', 'triggers'):
                with self.subTest(action=action_key, trigger=trigger_key):
                    self.config(yaml.safe_dump({'template': [{trigger_key: [{'platform': 'time_pattern', 'minutes': '/1'}],
                        action_key: [{'action': 'lock.unlock', 'target': {'entity_id': 'lock.sample'}}]}]}))
                    self.reload('template')
        for section in ('!include templates.yaml', '[!include templates.yaml]', '{}', 'null', 'invalid'):
            with self.subTest(section=section):
                self.config('template: ' + section + '\n')
                self.reload('template')
        self.config('template:\n  - sensor:\n      - name: Sample\n        state: 1\n')
        self.reload('template', allowed=True)

    def test_template_reload_rechecks_after_audit(self):
        self.config('template: []\n')
        def change(event):
            if event['outcome'] == 'requested':
                self.config('template:\n  - trigger: []\n    action:\n      - action: lock.unlock\n')
        self.engine.observer = change
        self.reload('template')

    def test_packages_inline_or_behind_homeassistant_include_require_approval(self):
        (self.root / 'automations.yaml').write_text('[]\n')
        (self.root / 'core.yaml').write_text('packages:\n  sample:\n    automation:\n      action: lock.unlock\n')
        for section in ('!include core.yaml', '{packages: {sample: {}}}', 'null', '[]', 'plain'):
            with self.subTest(section=section):
                self.config('automation: !include automations.yaml\nhomeassistant: ' + section + '\n')
                self.reload('automation')
        for section in ('', 'homeassistant: {}\n'):
            self.config('automation: !include automations.yaml\n' + section)
            self.reload('automation', allowed=True)

    def test_split_domain_keys_require_approval(self):
        for domain, filename, empty in (('automation', 'automations.yaml', '[]'), ('script', 'scripts.yaml', '{}'),
                                        ('template', '', '[]')):
            with self.subTest(domain=domain):
                if filename:
                    (self.root / filename).write_text(empty)
                value = '!include ' + filename if filename else '[]'
                self.config(f'{domain}: {value}\n{domain} extra: []\n')
                self.reload(domain)

    def test_blueprints_on_disk_or_in_loaded_definitions_require_approval(self):
        # Changing a blueprint can change its effects while the referring row stays identical.
        for domain, filename in (('automation', 'automations.yaml'), ('script', 'scripts.yaml')):
            for source in ('disk', 'loaded', 'both'):
                with self.subTest(domain=domain, source=source):
                    self.config(f'{domain}: !include {filename}\n')
                    direct = {'id': 'sample', 'actions': []} if domain == 'automation' else {'sequence': []}
                    blueprint = {'id': 'sample', 'use_blueprint': {'path': 'sample.yaml', 'input': {}}}
                    disk = blueprint if source in ('disk', 'both') else direct
                    loaded = blueprint if source in ('loaded', 'both') else direct
                    value = [disk] if domain == 'automation' else {'sample': disk}
                    (self.root / filename).write_text(yaml.safe_dump(value))
                    (self.root / 'sample.yaml').write_text('action: lock.unlock\n')
                    self.core.rest = lambda *args: [{'entity_id': domain + '.sample'}]
                    self.core.ws = lambda *args, **kwargs: {'config': deepcopy(loaded)}
                    self.reload(domain)

    def test_scene_reload_requires_approval_even_for_standard_layout(self):
        self.config('scene: !include scenes.yaml\n')
        for value in ('[]\n', '{}\n'):
            (self.root / 'scenes.yaml').write_text(value)
            self.reload('scene')


class RemainingGuards(unittest.TestCase):
    setUp = policy_tests.OverridePolicyTests.setUp
    dispatch = policy_tests.OverridePolicyTests.dispatch
    enable = policy_tests.OverridePolicyTests.enable

    def test_top_level_service_entity_id_in_sequence_requires_approval(self):
        self.enable(bigger=True)
        self.backend.definitions['script.sample'] = {'sequence': [
            {'service': 'light.turn_on', 'entity_id': 'lock.sample', 'target': {'entity_id': 'light.sample'}}]}
        self.assertFalse(self.dispatch(operation='service', service='script.turn_on', target=['script.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_service_target_domain_mismatch_requires_approval(self):
        self.enable(security=True)
        self.assertFalse(self.dispatch(operation='service', service='light.turn_on', target=['lock.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])

    def test_empty_scene_members_require_approval(self):
        self.backend.definitions['scene.sample'] = {'entities': {}}
        self.assertFalse(self.dispatch(operation='service', service='scene.turn_on', target=['scene.sample'])['ok'])
        self.assertEqual(self.backend.writes, [])


class AuditFlush(unittest.TestCase):
    setUp = runtime_tests.RuntimeOverrides.setUp
    dispatch = runtime_tests.RuntimeOverrides.dispatch

    def test_failure_reported_by_flush_prevents_the_write(self):
        def fail_flush():
            self.recorder.failures += 1
        with patch.object(self.recorder, 'flush', side_effect=fail_flush):
            self.assertFalse(self.dispatch(operation='check_config')['ok'])
        self.assertEqual(self.backend.writes, [])
