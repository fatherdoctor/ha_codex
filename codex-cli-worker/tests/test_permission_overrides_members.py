"""Scene and wrapper effects use real policy/Core code with synthetic transport only."""
import unittest
from types import SimpleNamespace
from copy import deepcopy
from permission_overrides import DEFAULTS, Engine, Authority
import permission_overrides_api as api
from test_permission_overrides_review import LOCK_RULE


class MemberEffects(unittest.TestCase):
    def setUp(self):
        self.states = {}
        self.registry = {}
        self.writes = []
        self.core = api.Core(SimpleNamespace())
        self.core.rest = lambda method, path: deepcopy(self.states.get(path.removeprefix('states/')))
        self.core.ws = lambda kind, **args: deepcopy(self.registry.get(args.get('entity_id')))
        self.core.execute = lambda plan: self.writes.append(plan)
        self.engine = Engine(SimpleNamespace(read=lambda: DEFAULTS), self.core,
                             lambda: LOCK_RULE, lambda event: None)
        self.authority = Authority('workspace-write', 'on-request', 'user', 'worker')

    def add(self, target, platform='hue', members=None):
        self.states[target] = {'entity_id': target, 'attributes': {}}
        self.registry[target] = {'platform': platform}
        if members is not None:
            self.states[target]['attributes']['entity_id'] = members

    def act(self, target, allowed=False):
        self.writes.clear()
        result = self.engine.dispatch({'operation': 'service', 'service': target.split('.')[0] + '.turn_on',
                                      'target': [target]}, self.authority)
        self.assertEqual(result['ok'], allowed, result)
        self.assertEqual(len(self.writes), int(allowed))
        return result

    def test_scene_and_nested_scene_refuse_template_switch_and_light(self):
        for domain in ('switch', 'light'):
            for nested in (False, True):
                with self.subTest(domain=domain, nested=nested):
                    member = domain + '.sample'
                    self.add(member, 'template')
                    self.add('scene.inner', 'scene', [member])
                    self.add('scene.outer', 'scene', ['scene.inner'] if nested else [member])
                    self.act('scene.outer')

    def test_safe_scene_members_are_verified_again_after_audit(self):
        self.add('light.sample')
        self.add('scene.outer', 'scene', ['light.sample'])
        self.act('scene.outer', allowed=True)
        def change(event):
            if event['outcome'] == 'requested':
                self.registry['light.sample']['platform'] = 'template'
        self.engine.observer = change
        self.act('scene.outer')

    def test_common_direct_device_platforms_remain_preapproved(self):
        for platform in ('zwave_js', 'zha', 'mqtt', 'matter', 'esphome', 'hue', 'wled',
                         'shelly', 'tplink', 'lutron_caseta', 'homekit_controller'):
            with self.subTest(platform=platform):
                self.add('light.sample', platform)
                self.act('light.sample', allowed=True)

    def test_groups_and_switch_wrappers_judge_their_actual_members(self):
        for wrapper in ('group', 'switch_as_x'):
            for platform, allowed in (('hue', True), ('template', False), ('unknown', False)):
                with self.subTest(wrapper=wrapper, platform=platform):
                    member = 'switch.sample' if wrapper == 'switch_as_x' else 'light.sample'
                    self.add(member, platform)
                    self.add('light.outer', wrapper, [member])
                    if wrapper == 'switch_as_x':
                        self.registry['light.outer']['options'] = {'switch_as_x': {
                            'entity_id': 'switch.sample', 'invert': False}}
                    self.act('light.outer', allowed=allowed)

    def test_wrapper_membership_change_after_audit_requires_approval(self):
        self.add('light.first')
        self.add('light.second')
        self.add('light.outer', 'group', ['light.first'])
        def change(event):
            if event['outcome'] == 'requested':
                self.states['light.outer']['attributes']['entity_id'] = ['light.second']
        self.engine.observer = change
        self.act('light.outer')

    def test_direct_entity_becoming_a_wrapper_after_audit_requires_approval(self):
        self.add('light.outer')
        self.add('light.member')
        def change(event):
            if event['outcome'] == 'requested':
                self.registry['light.outer']['platform'] = 'group'
                self.states['light.outer']['attributes']['entity_id'] = ['light.member']
        self.engine.observer = change
        self.act('light.outer')

    def test_cycles_empty_missing_and_unrecognized_wrapper_members_require_approval(self):
        for members in ([], None, ['light.missing'], ['light.outer']):
            with self.subTest(members=members):
                self.add('light.outer', 'group', members)
                self.act('light.outer')
        self.add('light.outer', 'switch_as_x')
        self.act('light.outer')
        self.registry['light.outer']['options'] = {'switch_as_x': {'entity_id': 'switch.sample', 'invert': 'yes'}}
        self.act('light.outer')
        self.add('scene.outer', 'scene', ['scene.inner'])
        self.add('scene.inner', 'scene', ['scene.outer'])
        self.act('scene.outer')

    def test_nested_safe_scenes_work_but_changed_inner_membership_asks(self):
        self.add('light.first')
        self.add('light.second')
        self.add('scene.inner', 'scene', ['light.first'])
        self.add('scene.outer', 'scene', ['scene.inner'])
        self.act('scene.outer', allowed=True)
        def change(event):
            if event['outcome'] == 'requested':
                self.states['scene.inner']['attributes']['entity_id'] = ['light.second']
        self.engine.observer = change
        self.act('scene.outer')

    def test_wrapped_and_scene_lock_effects_keep_the_selected_limit(self):
        self.add('lock.sample', 'zwave_js')
        self.add('scene.outer', 'scene', ['lock.sample'])
        self.act('scene.outer')
        self.add('switch.sample', 'template')
        self.add('light.wrapper', 'switch_as_x')
        self.registry['light.wrapper']['options'] = {'switch_as_x': {'entity_id': 'switch.sample', 'invert': True}}
        self.add('scene.outer', 'scene', ['light.wrapper'])
        self.act('scene.outer')
