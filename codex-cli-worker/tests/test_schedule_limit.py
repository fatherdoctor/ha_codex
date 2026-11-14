"""Page sizes and refusal reasons for the codex_scheduling tools.

A model asked read_mailbox for 50 messages and got only the generic
"Invalid, refused or expired scheduling request.", so it could not correct
itself. A limit outside 1-20 is now clamped, and every refusal on this path
says its reason.
"""
import json
import unittest
from unittest.mock import patch

from test_server import server
import schedule_api
import scheduling_mcp
import test_scheduling

GENERIC = 'Invalid, refused or expired scheduling request.'
ROWS = 25          # more than one full page of 20


class ScheduleLimitTests(unittest.TestCase):
    setUp = test_scheduling.SchedulingApiTests.setUp

    def begin_turn(self):
        task_id = 'synthetic-task'
        server.tasks[task_id] = {'status': 'running', 'current_turn_id': 'synthetic-turn'}
        cap = server.ha_data.begin(task_id)
        self.addCleanup(lambda: server.ha_data.end(task_id))
        return task_id, cap

    def fill(self):
        store = self.service.engine().store
        for i in range(ROWS):
            store.mail('Sample text %d' % i, 'owner', test_scheduling.NOW + i)
            store.save(test_scheduling.proposal(name='Sample %d' % i), owner=True, now=test_scheduling.NOW)

    def ask(self, cap, operation, arguments):
        """One request through the socket handler's code path, as the MCP tool sends it."""
        raw = json.dumps({'operation': operation, 'arguments': arguments, 'capability': cap}).encode() + b'\n'
        return self.service.answer(raw)

    def test_limit_is_clamped_for_each_tool(self):
        _, cap = self.begin_turn()
        self.fill()
        for operation in ('list_schedules', 'read_mailbox'):
            for limit, size, next_offset in ((0, 1, 1), (1, 1, 1), (20, 20, 20), (21, 20, 20), (50, 20, 20)):
                with self.subTest(operation=operation, limit=limit):
                    result = self.ask(cap, operation, {'limit': limit})
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(len(result['items']), size)
                    self.assertEqual(result['next_offset'], next_offset)
            # the next page after a clamped one holds the rest
            rest = self.ask(cap, operation, {'offset': 20, 'limit': 50})
            self.assertEqual(len(rest['items']), ROWS - 20)
            self.assertIsNone(rest['next_offset'])

    def test_limit_that_is_not_a_whole_number_is_refused_with_its_reason(self):
        _, cap = self.begin_turn()
        for operation in ('list_schedules', 'read_mailbox'):
            for limit in ('20', 2.5, True, None, [5]):
                with self.subTest(operation=operation, limit=limit):
                    result = self.ask(cap, operation, {'limit': limit})
                    self.assertFalse(result['ok'])
                    self.assertIn('limit must be a whole number', result['error'])

    def test_each_refusal_names_its_reason(self):
        task_id, cap = self.begin_turn()
        cases = [
            ('list_schedules', {'offset': -1}, 'offset must be a whole number from 0 to 120.'),
            ('read_mailbox', {'offset': 501}, 'offset must be a whole number from 0 to 500.'),
            ('read_mailbox', {'unread': True}, 'read_mailbox accepts only offset and limit.'),
            ('post_mailbox', {'body': 'Sample.', 'to': 'x'}, 'post_mailbox accepts only body and parent.'),
            ('post_mailbox', {'body': 'Sample.', 'parent': 'short'}, 'parent must be the 32-character id'),
            ('post_mailbox', {'body': 'Sample.', 'parent': 'f' * 32}, 'Mailbox thread not found.'),
            ('propose_schedule', {'name': 'Sample'}, 'prompt must be nonempty text'),
            ('approve_schedule', {}, 'approvals are owner-only'),
        ]
        for operation, arguments, reason in cases:
            with self.subTest(operation=operation, arguments=arguments):
                result = self.ask(cap, operation, arguments)
                self.assertFalse(result['ok'])
                self.assertIn(reason, result['error'])
        self.assertIn('needs exactly operation, arguments and capability', self.service.answer(b'{}\n')['error'])
        self.assertEqual(self.service.answer(b'not json\n')['error'], 'The request is not valid JSON.')
        self.assertEqual(self.service.answer(b'x' * 65537)['error'], 'The request is larger than 64 KiB.')
        server.ha_data.end(task_id)
        self.assertEqual(self.ask(cap, 'read_mailbox', {})['error'], 'This exchange has ended.')

    def test_an_unexpected_error_stays_generic(self):
        _, cap = self.begin_turn()
        for error in (KeyError('synthetic-task'), RuntimeError('internal detail')):
            with self.subTest(error=error), patch.object(self.service, 'dispatch', side_effect=error):
                self.assertEqual(self.ask(cap, 'read_mailbox', {})['error'], GENERIC)


class SchedulingToolTests(unittest.TestCase):
    def test_tool_schema_does_not_cap_the_limit(self):
        # A client that enforces the schema would otherwise refuse 50 before the service could clamp it.
        for name in ('list_schedules', 'read_mailbox'):
            tool = next(t for t in scheduling_mcp.TOOLS if t['name'] == name)
            limit = tool['inputSchema']['properties']['limit']
            self.assertEqual(limit['type'], 'integer')
            self.assertNotIn('maximum', limit)
            self.assertNotIn('minimum', limit)
            self.assertIn('1-20', limit['description'])

    def call(self, name, arguments):
        request = {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
        return json.loads(scheduling_mcp.respond(request)['content'][0]['text'])

    def test_tool_refusals_name_their_reason(self):
        self.assertIn('Unknown scheduling tool', self.call('approve_schedule', {})['error'])
        self.assertEqual(self.call('read_mailbox', {'unread': True})['error'],
                         'read_mailbox accepts only these arguments: offset, limit.')
        with patch.object(scheduling_mcp.socket, 'socket', side_effect=OSError('no socket')):
            self.assertEqual(self.call('read_mailbox', {})['error'],
                             'The scheduling service is not reachable; the add-on may be restarting.')
        self.assertEqual(scheduling_mcp.respond({'method': 'tools/call', 'params': []})['content'][0]['text'],
                         json.dumps({'ok': False, 'error': 'A tool call needs params with a name and an arguments object.'}))

    def test_service_reason_reaches_the_model(self):
        refused = {'ok': False, 'error': 'offset must be a whole number from 0 to 500.'}
        with patch.object(scheduling_mcp, 'fetch', return_value=refused):
            result = scheduling_mcp.respond({'method': 'tools/call', 'params': {'name': 'read_mailbox', 'arguments': {}}})
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text']), refused)


if __name__ == '__main__':
    unittest.main()
