"""Exercise the last direct-target read with real Core and capability checks."""
import unittest
from unittest.mock import patch

from ha_data import HomeAssistantData
from permission_overrides import DEFAULTS, MARKERS
import permission_overrides_api as api
import test_permission_overrides_runtime as runtime_tests


class FinalAuthorization(unittest.TestCase):
    setUp = runtime_tests.RuntimeOverrides.setUp

    def prepare(self):
        self.rules_path = self.config / 'AGENTS.md'
        self.rules_path.write_text('')
        self.service.settings().save({**DEFAULTS, 'security': True, 'bigger': True})
        self.service.confirm_rules()
        self.worker.tasks = {'task': {'task_source': 'chat', 'current_turn_id': 'synthetic-turn',
                                      'status': 'running'}}
        self.worker.ha_data = HomeAssistantData(self.worker, self.root / 'exports')
        self.capability = self.worker.ha_data.begin('task')
        self.service.begin('task', self.capability, self.options)

    def dispatch_core(self, arguments, last_read=lambda: None):
        core = api.Core(self.worker)
        self.reads = 0
        self.writes = []

        def rest(method, path, data=None):
            if method == 'GET':
                self.reads += 1
                return {'entity_id': arguments['target'][0] if isinstance(arguments['target'], list)
                        else arguments['target'], 'attributes': {}}
            self.writes.append((method, path, data))
            return {}

        def ws(kind, **data):
            if kind == 'config/entity_registry/get':
                # Planning reads once; this is the final post-audit registry reply.
                if self.reads == 2:
                    last_read()
                return {'platform': 'synthetic'}
            self.writes.append((kind, data))
            return {}

        core.rest, core.ws = rest, ws
        with patch.object(api, 'Core', return_value=core):
            return self.service.dispatch({'arguments': arguments, 'capability': self.capability})

    def check_last_read(self, change):
        self.prepare()
        result = self.dispatch_core({'operation': 'service', 'service': 'lock.unlock',
                                     'target': ['lock.sample']}, change)
        self.assertEqual(self.reads, 2)
        self.assertFalse(result['ok'], result)
        self.assertTrue(result.get('approval_required', False), result)
        self.assertEqual(self.writes, [])

    def test_rules_changed_during_last_target_read_make_zero_writes(self):
        def change():
            self.rules_path.write_text(MARKERS[0] + '\n- Do not lock or unlock locks.\n' + MARKERS[1])
        self.check_last_read(change)

    def test_cancellation_during_last_target_read_makes_zero_writes(self):
        self.check_last_read(lambda: self.worker.tasks['task'].update(cancellation_requested=True))

    def test_capability_revoked_during_last_target_read_makes_zero_writes(self):
        self.check_last_read(lambda: self.worker.ha_data.end('task'))

    def test_group_disabled_during_last_target_read_makes_zero_writes(self):
        self.check_last_read(lambda: self.service.settings().save({**DEFAULTS, 'bigger': True}))

    def test_unchanged_authorization_executes_one_direct_write(self):
        self.prepare()
        result = self.dispatch_core({'operation': 'service', 'service': 'lock.unlock',
                                     'target': ['lock.sample']})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.reads, 2)
        self.assertEqual(len(self.writes), 1)

    def test_unknown_service_field_requires_approval_and_makes_zero_writes(self):
        self.prepare()
        result = self.dispatch_core({'operation': 'service', 'service': 'light.turn_on',
                                     'target': ['light.sample'], 'data': {'unexpected': 1}})
        self.assertFalse(result['ok'], result)
        self.assertTrue(result.get('approval_required', False), result)
        self.assertEqual(self.writes, [])

    def test_cross_domain_rename_requires_approval_and_makes_zero_writes(self):
        self.prepare()
        result = self.dispatch_core({'operation': 'rename_entity', 'target': 'light.sample',
                                     'data': {'new_entity_id': 'switch.sample'}})
        self.assertFalse(result['ok'], result)
        self.assertTrue(result.get('approval_required', False), result)
        self.assertEqual(self.writes, [])


class Session:
    """A chat that answers the act tool's Allow once request with a fixed answer."""

    def __init__(self, answer):
        self.answer = answer
        self.questions = []

    def ask_owner_action(self, question):
        self.questions.append(question)
        return self.answer


class OwnerQuestion(FinalAuthorization):
    """An action that only the groups or the data allow-list refused goes to the owner in the chat."""

    request = {'operation': 'service', 'service': 'light.turn_on', 'target': ['light.sample'],
               'data': {'effect': 'colorloop'}}

    def ask(self, answer):
        self.prepare()
        self.worker.approval_sessions = {'task': Session(answer)}
        return self.dispatch_core(self.request), self.worker.approval_sessions['task']

    def test_allow_once_completes_the_action_after_a_full_second_check(self):
        result, session = self.ask(True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(session.questions), 1)
        self.assertIn('"effect": "colorloop"', session.questions[0]['detail'])
        self.assertEqual(len(self.writes), 1)
        # The first run reads its target in planning before it asks the owner; the allowed run
        # reads it in planning and again after the audit, as any pre-approved action does.
        self.assertEqual(self.reads, 3)
        decisions = [event.get('decision') for event in self.recorder.active['task']['record']['events']
                     if event.get('kind') == 'override']
        self.assertEqual(decisions, ['refused', 'owner_allowed', 'owner_allowed'])

    def test_deny_stops_the_action(self):
        result, session = self.ask(False)
        self.assertFalse(result['ok'])
        self.assertTrue(result['denied_by_owner'])
        self.assertFalse(result['approval_required'])
        self.assertEqual(self.writes, [])

    def test_no_answer_stops_the_action(self):
        result, session = self.ask(None)
        self.assertFalse(result['ok'])
        self.assertNotIn('denied_by_owner', result)
        self.assertEqual(self.writes, [])

    def test_allow_once_does_not_lift_a_guard_rail_added_while_waiting(self):
        self.prepare()
        session = Session(True)
        def answer(question):
            self.rules_path.write_text(MARKERS[0] + '\n- Do not access or control cameras.\n' + MARKERS[1])
            return True
        session.ask_owner_action = answer
        self.worker.approval_sessions = {'task': session}
        result = self.dispatch_core(self.request)
        self.assertFalse(result['ok'])
        self.assertEqual(self.writes, [])

    def test_without_a_live_chat_the_refusal_stands(self):
        self.prepare()
        self.worker.approval_sessions = {}
        result = self.dispatch_core(self.request)
        self.assertTrue(result['approval_required'])
        self.assertNotIn('owner_question', result)
        self.assertEqual(self.writes, [])
