"""A schedule made in a chat wakes that chat: its runs continue the chat's Codex thread instead of opening new chats."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_folders import isolated_app_folders
import test_scheduling as fixtures
from test_scheduling import NOW, proposal
from test_server import server
import schedule_api
import scheduling

CHAT = 'owner-chat'
NEW = '20270301T010000Z-abcdef12'
SESSION = '019fc242-910a-7c92-a17d-54c014e19fc4'
PROMPT = 'Reply with "Hello!"'


def setUpModule():
    """Scheduled runs may be switched off in a series; these tests need them on."""
    global _saved_switch
    _saved_switch = scheduling.SCHEDULED_RUNS
    scheduling.SCHEDULED_RUNS = True


def tearDownModule():
    scheduling.SCHEDULED_RUNS = _saved_switch


class ChatWorker(fixtures.FakeWorker):
    """Continues the named chat when it is in `chats`, as the real worker does for a chat that can go on."""

    def __init__(self, chats=()):
        super().__init__()
        self.chats = set(chats); self.turns = 0

    def submit(self, task_id, prompt, mode, name, schedule_id, model=None, chat_id=''):
        self.turns += 1
        turn_id = f'turn-{self.turns}'
        target = chat_id if chat_id in self.chats else task_id
        self.sent.append((target, prompt, mode, name, schedule_id))
        self.tasks[target] = {'status': 'queued', 'current_turn_id': turn_id,
                              'turns': [{'turn_id': turn_id, 'status': 'queued'}]}
        self.blocked = True
        return scheduling.Placed(target, turn_id)


class EngineTests(unittest.TestCase):
    """The run log and the schedule when the runs of one schedule share one chat."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'schedules.sqlite3'
        self.store = scheduling.Store(self.path)

    def engine(self, worker):
        return scheduling.Engine(self.store, worker)

    def test_every_run_of_a_repeating_schedule_uses_the_chat_it_was_made_in(self):
        worker = ChatWorker({CHAT}); engine = self.engine(worker)
        schedule = self.store.save(proposal(chat_id=CHAT), owner=True, now=NOW)
        for hour in (1, 2, 3):
            engine.tick(NOW + hour * 3600 + 1)
            worker.tasks[CHAT]['status'] = 'completed'; worker.blocked = False
        runs = self.store.runs()
        self.assertEqual([run['task_id'] for run in runs], [CHAT] * 3)
        self.assertEqual(sorted(run['turn_id'] for run in runs), ['turn-1', 'turn-2', 'turn-3'])
        self.assertEqual({run['schedule_id'] for run in runs}, {schedule['id']})

    def test_a_schedule_from_settings_still_opens_a_new_chat_for_each_run(self):
        worker = ChatWorker({CHAT}); engine = self.engine(worker)
        self.store.save(proposal(), owner=True, now=NOW)
        engine.tick(NOW + 3601)
        worker.tasks[self.store.runs()[0]['task_id']]['status'] = 'completed'; worker.blocked = False
        engine.tick(NOW + 7202)
        chats = [run['task_id'] for run in self.store.runs()]
        self.assertEqual(len(set(chats)), 2)
        self.assertNotIn(CHAT, chats)

    def test_the_schedule_follows_the_new_chat_when_its_own_chat_is_gone(self):
        worker = ChatWorker(); engine = self.engine(worker)
        schedule = self.store.save(proposal(chat_id=CHAT), owner=True, now=NOW)
        engine.tick(NOW + 3601)
        new_chat = self.store.runs()[0]['task_id']
        self.assertNotEqual(new_chat, CHAT)
        self.assertEqual(self.store.schedules()[0]['chat_id'], new_chat)
        worker.chats.add(new_chat); worker.tasks[new_chat]['status'] = 'completed'; worker.blocked = False
        engine.tick(NOW + 7202)
        self.assertEqual([run['task_id'] for run in self.store.runs()], [new_chat, new_chat])
        self.assertEqual(self.store.schedules()[0]['id'], schedule['id'])

    def test_following_a_new_chat_keeps_a_chat_the_owner_chose_meanwhile(self):
        schedule = self.store.save(proposal(chat_id=CHAT), owner=True, now=NOW)
        self.store.save(proposal(chat_id='chosen-chat'), owner=True, now=NOW, schedule_id=schedule['id'])
        self.store.follow_chat(schedule['id'], CHAT, NEW)
        self.assertEqual(self.store.schedules()[0]['chat_id'], 'chosen-chat')

    def test_a_busy_chat_holds_the_run_until_its_exchange_ends(self):
        worker = ChatWorker({CHAT}); engine = self.engine(worker)
        self.store.save(proposal(chat_id=CHAT), owner=True, now=NOW)
        worker.blocked = True  # The owner's exchange in the chat is still running.
        engine.tick(NOW + 3601)
        self.assertEqual((worker.sent, self.store.runs()), ([], []))
        self.assertLessEqual(self.store.schedules()[0]['next_due'], NOW + 3601)
        worker.blocked = False
        engine.tick(NOW + 3700)
        self.assertEqual([run['task_id'] for run in self.store.runs()], [CHAT])

    def test_a_run_followed_by_later_messages_keeps_its_own_outcome(self):
        task = {'status': 'running', 'current_turn_id': 'owner-turn', 'approvals': [{'status': 'pending'}],
                'turns': [{'turn_id': 'scheduled', 'status': 'failed'}, {'turn_id': 'owner-turn', 'status': 'running'}]}
        self.assertEqual(scheduling.run_status(task, 'scheduled'), 'failed')
        task['turns'][0]['status'] = 'waiting_for_input'  # The owner answered its question in the next message.
        self.assertEqual(scheduling.run_status(task, 'scheduled'), 'completed')
        self.assertEqual(scheduling.run_status(task, 'owner-turn'), 'waiting_for_approval')
        self.assertEqual(scheduling.run_status({'status': 'completed'}, ''), 'completed')


class UpgradeTests(unittest.TestCase):
    """A run log from version 3, where each run had a chat of its own, moves to the shared-chat shape."""

    def test_a_version_3_run_log_is_kept_and_accepts_runs_that_share_a_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'schedules.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE runs (id TEXT PRIMARY KEY, schedule_id TEXT, task_id TEXT UNIQUE NOT NULL, '
                       "due REAL NOT NULL, created REAL NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')")
            db.execute("INSERT INTO runs VALUES('old-run','s1',?,1.0,2.0,'completed','')", (CHAT,))
            db.execute('PRAGMA user_version=3'); db.commit(); db.close()
            store = scheduling.Store(path)
            self.assertEqual([(run['id'], run['task_id'], run['turn_id']) for run in store.runs()],
                             [('old-run', CHAT, '')])
            with store.connect() as db:
                db.execute("INSERT INTO runs(id,schedule_id,task_id,due,created,status) VALUES('new-run','s1',?,3.0,4.0,'queued')",
                           (CHAT,))
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(len(scheduling.Store(path).runs()), 2)


class WorkerTests(unittest.TestCase):
    """schedule_api.Worker against the real worker module, with Codex itself left out."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.addCleanup(isolated_app_folders(server, Path(temp.name)).close)
        saved = dict(server.tasks); server.tasks.clear()
        self.addCleanup(lambda: (server.tasks.clear(), server.tasks.update(saved)))
        self.addCleanup(server.active_task_runners.clear)
        self.started = []
        for name, value in (('save_task_index', None), ('read_options', {}),
                            ('session_available', True)):
            self.enter(patch.object(server, name, return_value=value))
        self.enter(patch.object(server, 'start_background_task',
                                side_effect=lambda *args, **kwargs: self.started.append((args, kwargs))))
        self.enter(patch.object(server, 'resolve_chat_settings',
                                side_effect=lambda settings, options: {'model': settings.get('model') or 'default'}))
        self.enter(patch.object(server.protected_policy, 'policy_problem', return_value=None))
        self.worker = schedule_api.Worker(server)

    def enter(self, context):
        context.start(); self.addCleanup(context.stop)

    def owner_chat(self, **changes):
        server.tasks[CHAT] = {
            'task_id': CHAT, 'title': 'Lights', 'status': 'completed', 'session_id': SESSION, 'prompt': 'First message',
            'chat_settings': {'model': 'owner-model'}, 'permission_mode': 'default', 'current_turn_id': 'owner-turn',
            'turns': [{'turn_id': 'owner-turn', 'message': 'First message', 'status': 'completed'}], **changes}

    def submit(self, mode='default', chat_id=CHAT, model=None):
        return self.worker.submit(NEW, PROMPT, mode, 'Hello', 'schedule-1', model=model, chat_id=chat_id)

    def test_the_run_continues_the_chat_thread_and_marks_it_unread(self):
        self.owner_chat()
        placed = self.submit(model='schedule-model')
        task = server.tasks[CHAT]
        self.assertEqual(placed, scheduling.Placed(CHAT, task['current_turn_id']))
        self.assertNotIn(NEW, server.tasks)
        self.assertEqual(self.started, [((CHAT, 'First message'), {'session_id': SESSION, 'reply': PROMPT})])
        turn = task['turns'][-1]
        self.assertEqual((turn['message'], turn['task_source'], turn['background_mode']), (PROMPT, 'schedule', 'default'))
        self.assertEqual(turn['execution_settings'], {'model': 'schedule-model'})
        self.assertEqual(task['chat_settings'], {'model': 'owner-model'})  # The owner's next message keeps it.
        self.assertTrue(task['unread'])
        self.assertTrue(server.background_task(CHAT))

    def test_the_run_never_takes_the_chat_wider_mode(self):
        self.owner_chat(permission_mode=server.protected_policy.FULL_ACCESS_MODE)
        with patch.object(server.full_access, 'is_on', return_value=True):
            self.assertEqual(server.task_mode_settings(CHAT, {}),
                             server.protected_policy.mode_settings(server.protected_policy.FULL_ACCESS_MODE, 'workspace-write'))
            self.submit()
            self.assertEqual(server.task_mode_settings(CHAT, {}),
                             server.protected_policy.mode_settings(server.protected_policy.DEFAULT_MODE, 'workspace-write'))

    def test_a_read_only_schedule_narrows_only_its_own_exchange(self):
        self.owner_chat()
        self.submit(mode='read-only')
        self.assertEqual(server.task_options(CHAT)['codex_sandbox'], 'read-only')
        task = server.tasks[CHAT]
        task['turns'].append({'turn_id': 'owner-next', 'message': 'Thanks', 'status': 'running'})
        task['current_turn_id'] = 'owner-next'
        self.assertIsNone(server.unattended_mode(task))
        self.assertNotIn('codex_sandbox', server.task_options(CHAT))
        self.assertFalse(server.background_task(CHAT))

    def test_a_chat_that_cannot_go_on_gets_a_new_chat_named_for_the_schedule(self):
        cases = {'deleted': None, 'archived': {'archived': True}, 'without a session': {'session_id': ''},
                 'made in Settings': {}}
        for label, changes in cases.items():
            with self.subTest(label):
                server.tasks.clear(); self.started.clear(); server.active_task_runners.clear()
                if changes is not None:
                    self.owner_chat(**changes)
                placed = self.submit(mode='read-only', chat_id='' if label == 'made in Settings' else CHAT)
                self.assertEqual(placed.task_id, NEW)
                new = server.tasks[NEW]
                self.assertEqual((new['title'], new['task_source'], new['background_mode'], new['unread']),
                                 ('Hello', 'schedule', 'read-only', True))
                self.assertEqual(self.started, [((NEW, PROMPT), {})])
                if changes is not None:
                    self.assertEqual(len(server.tasks[CHAT]['turns']), 1)

    def test_a_busy_worker_refuses_the_run_so_it_stays_due(self):
        self.owner_chat()
        server.active_task_runners.add('other-chat')
        with self.assertRaises(scheduling.Busy):
            self.submit()
        self.assertEqual(len(server.tasks[CHAT]['turns']), 1)


class UnreadTests(unittest.TestCase):
    """The chat list shows the unread mark, and opening the chat clears it without moving the chat."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.addCleanup(isolated_app_folders(server, Path(temp.name)).close)
        saved = dict(server.tasks); server.tasks.clear()
        self.addCleanup(lambda: (server.tasks.clear(), server.tasks.update(saved)))
        for name, value in (('save_task_index', None), ('api_token', 'test-token')):
            context = patch.object(server, name, return_value=value); context.start(); self.addCleanup(context.stop)
        server.tasks[CHAT] = {'task_id': CHAT, 'status': 'completed', 'unread': True,
                              'updated_at': '2027-03-01T01:00:00+00:00'}

    def test_reading_the_chat_clears_the_mark_and_keeps_its_place(self):
        self.assertTrue(server.task_payload(server.tasks[CHAT], summary=True)['unread'])
        response = server.app.test_client().post(f'/tasks/{CHAT}/read', headers={'Authorization': 'Bearer test-token'},
                                                 json={})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(server.task_payload(server.tasks[CHAT], summary=True)['unread'])
        self.assertEqual(server.tasks[CHAT]['updated_at'], '2027-03-01T01:00:00+00:00')
        missing = server.app.test_client().post('/tasks/no-such-chat/read', headers={'Authorization': 'Bearer test-token'},
                                                json={})
        self.assertEqual(missing.status_code, 404)


if __name__ == '__main__':
    unittest.main()
