"""A one-time wakeup that Codex proposes runs without approval; times reach Codex in the home's time zone."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_server import server
import scheduling
import scheduling_mcp
import test_scheduling
from test_scheduling import FakeWorker, proposal

NOW = datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()
ZONE = 'America/Chicago'


class ChatWorker(FakeWorker):
    """A fake worker that also records the chat a run continues."""
    def submit(self, task_id, prompt, mode, name, schedule_id, chat_id=''):
        self.chats = getattr(self, 'chats', []) + [chat_id]
        return super().submit(task_id, prompt, mode, name, schedule_id)


def wakeup(at=NOW + 600):
    return proposal(name='Sample wakeup', spec={'kind': 'once', 'at': at})


class WakeupStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = scheduling.Store(Path(self.temp.name) / 'schedules.sqlite3', zone=ZONE)

    def propose(self, item):
        return self.store.save(item, owner=False, author='codex', now=NOW, made_in='synthetic-chat')

    def test_a_codex_wakeup_is_saved_on(self):
        item = self.propose(wakeup())
        self.assertTrue(item['approved']); self.assertTrue(item['enabled'])
        self.assertEqual(item['author'], 'codex'); self.assertEqual(item['next_due'], NOW + 600)

    def test_a_repeating_codex_proposal_still_waits_for_the_owner(self):
        for spec in ({'kind': 'interval', 'seconds': 3600}, {'kind': 'cron', 'expression': '0 7 * * *'}):
            with self.subTest(kind=spec['kind']):
                item = self.propose(proposal(spec=spec))
                self.assertFalse(item['approved']); self.assertFalse(item['enabled'])

    def test_the_wakeup_runs_once_in_its_chat_with_no_approval(self):
        item = self.propose(wakeup())
        worker = ChatWorker()
        engine = scheduling.Engine(self.store, worker)
        engine.dispatch_due(NOW + 599)
        self.assertEqual(worker.sent, [])
        engine.dispatch_due(NOW + 600)
        self.assertEqual(worker.chats, ['synthetic-chat'])
        self.assertEqual(worker.sent[0][4], item['id'])
        saved = self.store.schedules()[0]
        self.assertFalse(saved['enabled']); self.assertIsNone(saved['next_due'])

    def test_wakeups_count_against_the_schedule_limit_not_the_proposal_limit(self):
        for index in range(scheduling.MAX_PROPOSALS):
            self.propose(proposal(name=f'Sample {index}'))
        with self.assertRaisesRegex(ValueError, 'Proposal limit reached'):
            self.propose(proposal(name='One more'))
        self.assertTrue(self.propose(wakeup())['approved'])
        with patch.object(scheduling, 'MAX_SCHEDULES', 1):
            with self.assertRaisesRegex(ValueError, 'Approved schedule limit reached'):
                self.propose(wakeup(NOW + 700))

    def test_the_owner_still_edits_and_deletes_a_codex_wakeup(self):
        item = self.propose(wakeup())
        with self.assertRaises(LookupError):
            self.store.save(wakeup(NOW + 900), owner=False, author='codex', now=NOW, schedule_id=item['id'])
        self.store.action(item['id'], 'pause', NOW)
        self.assertFalse(self.store.schedules()[0]['enabled'])
        self.store.action(item['id'], 'delete', NOW)
        self.assertEqual(self.store.schedules(), [])


class LocalTimeTextTests(unittest.TestCase):
    def test_times_are_written_in_the_named_zone(self):
        winter = datetime(2026, 11, 25, 0, 50, 58, tzinfo=timezone.utc).timestamp()
        self.assertEqual(scheduling.local_text(winter, ZONE), 'Tuesday, November 24, 2026 at 6:50 PM CST')
        summer = datetime(2027, 7, 4, 14, 5, tzinfo=timezone.utc).timestamp()
        self.assertEqual(scheduling.local_text(summer, ZONE), 'Sunday, July 4, 2027 at 9:05 AM CDT')
        self.assertEqual(scheduling.local_text(summer, 'UTC'), 'Sunday, July 4, 2027 at 2:05 PM UTC')

    def test_the_tool_text_does_not_call_cron_fields_utc(self):
        spec = scheduling_mcp.PROPOSAL['properties']['spec']['description']
        self.assertNotIn('UTC', spec)
        self.assertIn('Home Assistant time zone', spec)
        self.assertIn('one-time wakeup', scheduling_mcp.TOOLS[0]['description'])
        self.assertIn('never in UTC', server.SCHEDULING_PARAGRAPH)


class WakeupAnswerTests(unittest.TestCase):
    """The socket answer Codex reads after propose_schedule."""
    setUp_base = test_scheduling.SchedulingApiTests.setUp
    agent = test_scheduling.SchedulingApiTests.agent

    def setUp(self):
        self.setUp_base()
        zone = patch.object(server, 'schedule_timezone', return_value=ZONE)
        zone.start(); self.addCleanup(zone.stop)

    def test_a_wakeup_answer_says_it_is_on_and_gives_the_local_time(self):
        at = time.time() + 3600
        local = datetime.fromtimestamp(at, ZoneInfo(ZONE))
        answer = self.agent()('propose_schedule', wakeup(at))
        self.assertTrue(answer['ok']); self.assertTrue(answer['approved']); self.assertTrue(answer['enabled'])
        self.assertRegex(answer['next_run'], rf'^\w+, \w+ {local.day}, {local.year} at {local.hour % 12 or 12}:{local.minute:02d} [AP]M C[SD]T$')
        self.assertEqual(answer['timezone'], ZONE)
        self.assertIn('is on', answer['message'])

    def test_a_repeating_answer_waits_and_also_gives_the_local_time(self):
        answer = self.agent()('propose_schedule', proposal(spec={'kind': 'cron', 'expression': '0 7 * * *'}))
        self.assertTrue(answer['ok']); self.assertFalse(answer['approved']); self.assertFalse(answer['enabled'])
        self.assertRegex(answer['next_run'], r' at 7:00 AM C[SD]T$')
        self.assertIn('Waiting for owner approval', answer['message'])


if __name__ == '__main__':
    unittest.main()
