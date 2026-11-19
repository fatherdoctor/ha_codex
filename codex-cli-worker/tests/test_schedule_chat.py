"""A schedule made from a chat: the owner's chat and model fields, and who may set or read them."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_scheduling as fixtures
from test_scheduling import NOW, proposal, scheduling
from test_server import server
import scheduling


def setUpModule():
    """These tests cover the scheduling code, which is switched off in this series; turn it on for them."""
    global _saved_switch
    _saved_switch = scheduling.SCHEDULED_RUNS
    scheduling.SCHEDULED_RUNS = True


def tearDownModule():
    scheduling.SCHEDULED_RUNS = _saved_switch


MODEL = server.CHAT_MODELS[0][0]
OTHER_MODEL = server.CHAT_MODELS[1][0]
CHAT = 'sample-chat'


class RecordingWorker(fixtures.FakeWorker):
    """Records the extra arguments a run was started with."""

    def submit(self, task_id, prompt, mode, name, schedule_id, **extra):
        super().submit(task_id, prompt, mode, name, schedule_id)
        self.extra = extra


class OwnerFieldStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'schedules.sqlite3'
        self.store = scheduling.Store(self.path)

    def test_the_owner_saves_a_chat_and_a_model_with_a_schedule(self):
        saved = self.store.save(proposal(chat_id=CHAT, model=MODEL), owner=True, now=NOW)
        self.assertEqual((saved['chat_id'], saved['model']), (CHAT, MODEL))
        self.assertEqual(self.store.schedules()[0]['chat_id'], CHAT)

    def test_a_schedule_saved_without_them_has_no_chat_and_follows_the_addon_model(self):
        saved = self.store.save(proposal(), owner=True, now=NOW)
        self.assertEqual((saved['chat_id'], saved['model']), ('', None))

    def test_an_edit_that_leaves_them_out_keeps_them_and_an_edit_that_names_them_replaces_them(self):
        saved = self.store.save(proposal(chat_id=CHAT, model=MODEL), owner=True, now=NOW)
        kept = self.store.save(proposal(name='Renamed'), owner=True, now=NOW, schedule_id=saved['id'])
        self.assertEqual((kept['name'], kept['chat_id'], kept['model']), ('Renamed', CHAT, MODEL))
        cleared = self.store.save(proposal(chat_id='', model=None), owner=True, now=NOW, schedule_id=saved['id'])
        self.assertEqual((cleared['chat_id'], cleared['model']), ('', None))

    def test_an_agent_proposal_cannot_name_a_chat_or_a_model(self):
        for author in ('codex',):
            for field in ({'chat_id': CHAT}, {'model': MODEL}):
                with self.subTest(author=author, field=field), self.assertRaisesRegex(
                        ValueError, 'accepts only name, prompt, mode and spec'):
                    self.store.save(proposal(**field), owner=False, author=author, now=NOW)
        self.assertEqual(self.store.schedules(), [])

    def test_a_chat_or_model_that_is_not_a_plain_id_is_refused(self):
        bad_chats = (None, 7, ['x'], '../tasks', 'a b', '-leading', 'x' * 129, 'chat\n')
        bad_models = (7, ['x'], '', 'a b', '../model', 'x' * 81, 'model\n')
        for field, values in (('chat_id', bad_chats), ('model', bad_models)):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.store.save(proposal(**{field: value}), owner=True, now=NOW)
        self.assertEqual(self.store.schedules(), [])

    def test_a_schedule_from_an_older_version_opens_with_no_chat_and_still_runs(self):
        old = Path(self.temp.name) / 'old.sqlite3'
        with sqlite3.connect(old) as db:
            db.execute('''CREATE TABLE schedules (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
                mode TEXT NOT NULL, spec TEXT NOT NULL, approved INTEGER NOT NULL,
                enabled INTEGER NOT NULL, next_due REAL, updated REAL NOT NULL, revision TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'owner')''')
            db.execute("INSERT INTO schedules VALUES('old','Older schedule','Inspect the sample.','read-only',"
                       "'{\"kind\": \"interval\", \"seconds\": 3600}',1,1,?,?,'revision','owner')", (NOW + 3600, NOW))
        store = scheduling.Store(old)
        schedule = store.schedules()[0]
        self.assertEqual((schedule['name'], schedule['chat_id'], schedule['model']), ('Older schedule', '', None))
        worker = RecordingWorker()
        scheduling.Engine(store, worker).tick(NOW + 3600)
        self.assertEqual(worker.sent[0][1:], ('Inspect the sample.', 'read-only', 'Older schedule', 'old'))
        self.assertEqual(worker.extra, {}, 'a schedule with no model starts as it did before')

    def test_a_run_is_started_with_the_model_of_its_schedule(self):
        self.store.save(proposal(model=MODEL), owner=True, now=NOW)
        worker = RecordingWorker()
        scheduling.Engine(self.store, worker).tick(NOW + 3600)
        self.assertEqual(worker.extra, {'model': MODEL})


class OwnerFieldApiTests(unittest.TestCase):
    setUp = fixtures.SchedulingApiTests.setUp
    request_owner = fixtures.SchedulingApiTests.request_owner

    def add_chat(self):
        server.tasks[CHAT] = {'task_id': CHAT, 'status': 'completed', 'title': 'Sample chat'}

    def test_a_schedule_names_only_a_chat_that_exists(self):
        refused = self.request_owner('POST', '/schedules', proposal(chat_id=CHAT))
        self.assertEqual(refused.status_code, 400)
        self.assertIn('no longer exists', refused.json['error'])
        self.add_chat()
        saved = self.request_owner('POST', '/schedules', proposal(chat_id=CHAT))
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(self.request_owner('GET', '/schedules').json['schedules'][0]['chat_id'], CHAT)

    def test_a_schedule_can_still_be_edited_after_its_chat_is_deleted(self):
        self.add_chat()
        saved = self.request_owner('POST', '/schedules', proposal(chat_id=CHAT)).json['schedule']
        del server.tasks[CHAT]
        edited = self.request_owner('PATCH', '/schedules/' + saved['id'], proposal(name='Renamed', chat_id=CHAT))
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json['schedule']['chat_id'], CHAT)
        moved = self.request_owner('PATCH', '/schedules/' + saved['id'], proposal(chat_id='another-missing-chat'))
        self.assertEqual(moved.status_code, 400)

    def test_a_schedule_names_only_a_model_the_chat_page_offers(self):
        refused = self.request_owner('POST', '/schedules', proposal(model='not-a-model'))
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.service.engine().store.schedules(), [])
        saved = self.request_owner('POST', '/schedules', proposal(model=MODEL))
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(saved.json['schedule']['model'], MODEL)

    def test_a_due_schedule_starts_its_chat_on_its_own_model(self):
        self.request_owner('POST', '/schedules', proposal(model=MODEL))
        self.service.engine().tick(NOW + 10 ** 9)
        run = self.service.engine().store.runs()[0]
        task = server.tasks[run['task_id']]
        self.assertEqual(task['chat_settings']['model'], MODEL)
        self.assertEqual(task['turns'][0]['execution_settings']['model'], MODEL)
        self.assertEqual(task['task_source'], 'schedule')

    def test_a_run_says_why_when_a_later_model_pin_refuses_the_model_of_its_schedule(self):
        self.request_owner('POST', '/schedules', proposal(model=MODEL))
        pinned = {**server.read_options(), 'model_pin': OTHER_MODEL}
        with patch.object(server, 'read_options', return_value=pinned), self.assertRaises(ValueError):
            self.service.engine().tick(NOW + 10 ** 9)
        run = self.service.engine().store.runs()[0]
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['error'], server.model_pin_message(MODEL, OTHER_MODEL) + ' Not retried.')
        self.assertNotIn(run['task_id'], server.tasks)

    def test_codex_cannot_read_the_chat_and_the_model(self):
        self.add_chat()
        self.request_owner('POST', '/schedules', proposal(chat_id=CHAT, model=MODEL))
        task = 'listing-turn'
        server.tasks[task] = {'status': 'running', 'current_turn_id': 'synthetic-turn'}
        thread = fixtures.open_exchange(self, task)
        for field in ({'chat_id': CHAT}, {'model': MODEL}):
            with self.assertRaisesRegex(ValueError, 'accepts only name, prompt, mode and spec'):
                self.service.dispatch({'operation': 'propose_schedule', 'arguments': proposal(**field), 'thread_id': thread})
        self.service.dispatch({'operation': 'propose_schedule', 'arguments': proposal(), 'thread_id': thread})
        answer = self.service.dispatch({'operation': 'list_schedules', 'arguments': {}, 'thread_id': thread})
        self.assertEqual(len(answer['items']), 2)
        for row in answer['items']:
            self.assertFalse(scheduling.OWNER_FIELDS & set(row), row)


if __name__ == '__main__':
    unittest.main()
