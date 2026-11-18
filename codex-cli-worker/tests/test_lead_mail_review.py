"""Mailbox readers preserve their own delivery, retention, and task boundaries."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
import scheduling
import test_scheduling

NOW = test_scheduling.NOW


class MailRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = scheduling.Store(Path(self.temp.name) / 'mail.sqlite3')

    def test_old_unread_mail_gets_48_hours_from_its_first_read(self):
        message = self.store.mail('Old unread note', 'owner', NOW)
        read_at = NOW + 3 * 86400
        page = self.store.messages(reader='lead', mark_read=True, now=read_at)
        self.assertEqual([row['id'] for row in page], [message])
        self.assertEqual(len(self.store.messages(reader='lead', now=read_at + 1)), 1)
        self.store.mark_mail_read('lead', [message], now=read_at + 12 * 3600)
        expired = read_at + 48 * 3600 + 1
        self.assertEqual(self.store.messages(reader='lead', now=expired), [])
        self.assertEqual(len(self.store.messages(reader='owner', now=expired)), 1)
        self.assertEqual(len(self.store.messages(reader='codex', now=expired)), 1)

    def test_legacy_read_marks_get_one_upgrade_grace_period(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        message = 'a' * 32
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE mailbox(id TEXT PRIMARY KEY,parent TEXT,author TEXT,body TEXT,created REAL)')
            db.execute('INSERT INTO mailbox VALUES(?,?,?,?,?)', (message, None, 'owner', 'Legacy note', NOW))
            db.execute('CREATE TABLE mailbox_reads(message_id TEXT,reader TEXT,PRIMARY KEY(message_id,reader))')
            db.execute('INSERT INTO mailbox_reads VALUES(?,?)', (message, 'lead'))
        upgraded = NOW + 3 * 86400
        with patch.object(scheduling.time, 'time', return_value=upgraded):
            store = scheduling.Store(path)
        self.assertEqual(len(store.messages(reader='lead', now=upgraded)), 1)
        with patch.object(scheduling.time, 'time', return_value=upgraded + 86400):
            store = scheduling.Store(path)
        self.assertEqual(store.messages(reader='lead', now=upgraded + 48 * 3600 + 1), [])

    def test_reply_preserves_other_replies_last_modified(self):
        root = self.store.mail('Root', 'owner', NOW)
        reply = self.store.mail('First reply', 'lead', NOW + 1, root, 'turn')
        self.store.mail('Second reply', 'codex', NOW + 2, root, 'turn')
        rows = {row['id']: row for row in self.store.messages(now=NOW + 3)}
        self.assertEqual(rows[reply]['last_modified'], scheduling.mail_time(NOW + 1))
        self.assertEqual(rows[root]['last_modified'], scheduling.mail_time(NOW + 2))

    def test_reply_reaches_earlier_thread_participants(self):
        root = self.store.mail('Owner to lead', 'owner', NOW, to='lead')
        reply = self.store.mail('Lead to Codex', 'lead', NOW + 1, root, 'turn', to='codex')
        for reader in scheduling.READERS:
            with self.subTest(reader=reader):
                self.assertIn(reply, [row['id'] for row in self.store.messages(reader=reader, now=NOW + 2)])

    def test_completed_task_links_expire_only_after_the_thread_is_gone(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                store = scheduling.Store(Path(self.temp.name) / (str(finished) + '.sqlite3'))
                task, _ = store.hold_lead('Inspect', 'default', NOW,
                                          {'message': 'Task note', 'turn_id': 'turn'})
                if finished:
                    store.report_lead(task, 'finished', 'Completed', NOW + 1)
                store.clear_mail()
                with store.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM lead_threads').fetchone()[0],
                                     0 if finished else 1)


class MailReaderApiTests(unittest.TestCase):
    setUp = test_scheduling.SchedulingApiTests.setUp
    request_owner = test_scheduling.SchedulingApiTests.request_owner

    def exchange(self, *, origin='lead', turn_origin='lead'):
        """Create one synthetic active exchange with independent task and turn labels."""
        task = 'mail-reader-' + str(len(server.tasks))
        turn = {'turn_id': 'current'}
        if turn_origin is not None:
            turn['task_source'] = turn_origin
        server.tasks[task] = {'status': 'running', 'current_turn_id': 'current',
                              'task_source': origin, 'turns': [turn]}
        capability = server.ha_data.begin(task)
        self.addCleanup(lambda: server.ha_data.end(task))
        return capability

    def test_both_readers_walk_every_three_day_old_message(self):
        store = self.service.engine().store
        capability = self.exchange()
        for reader in ('lead', 'codex'):
            expected = [store.mail('Note ' + str(i), 'owner', NOW + i, to=reader) for i in range(30)]
            offset, seen = 0, []
            with self.subTest(reader=reader), patch.object(scheduling.time, 'time', return_value=NOW + 3 * 86400):
                for _ in range(5):
                    if reader == 'lead':
                        response = self.client.get('/lead/mailbox?offset=' + str(offset) + '&limit=10',
                                                   headers=self.lead)
                        self.assertEqual(response.status_code, 200)
                        result, key = response.json, 'mailbox'
                    else:
                        result = self.service.dispatch({'operation': 'read_mailbox', 'capability': capability,
                                                        'arguments': {'offset': offset, 'limit': 10}})
                        key = 'items'
                    seen.extend(row['id'] for row in result[key])
                    offset = result['next_offset']
                    if offset is None:
                        break
                self.assertIsNone(offset)
                self.assertEqual(seen, expected)
                self.assertEqual(store.mail_status()['unread'][reader], 0)

    def test_owner_cannot_acknowledge_another_readers_mailbox(self):
        store = self.service.engine().store
        for reader in ('lead', 'codex'):
            with self.subTest(reader=reader):
                message = store.mail('Unread for ' + reader, 'owner', NOW, to=reader)
                path = '/schedule-mailbox?mailbox=' + reader
                self.assertEqual(self.request_owner('GET', path).status_code, 200)
                response = self.request_owner('POST', '/schedule-mailbox/read?mailbox=' + reader,
                                               {'ids': [message]})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(store.mail_status()['unread'][reader], 1)
        self.assertEqual(self.request_owner('POST', '/schedule-mailbox/read', {'ids': [message]}).status_code, 200)

    def test_codex_walks_every_legacy_message_above_capacity(self):
        engine = self.service.engine()
        capability = self.exchange()
        read_at = NOW + 3 * 86400
        expected = [f'{i:032x}' for i in range(150)]
        for limit in (10, 20):
            with self.subTest(limit=limit):
                path = self.root / f'legacy-{limit}.sqlite3'
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE mailbox(id TEXT PRIMARY KEY,parent TEXT,author TEXT,body TEXT,created REAL)')
                    db.executemany('INSERT INTO mailbox VALUES(?,?,?,?,?)',
                                   [(mid, None, 'owner', f'Legacy note {i}', NOW + i)
                                    for i, mid in enumerate(expected)])
                with patch.object(scheduling.time, 'time', return_value=read_at):
                    engine.store = scheduling.Store(path)
                    offset, seen = 0, []
                    for _ in range(16):
                        result = self.service.dispatch({'operation': 'read_mailbox', 'capability': capability,
                                                        'arguments': {'offset': offset, 'limit': limit}})
                        seen.extend(row['id'] for row in result['items'])
                        offset = result['next_offset']
                        if offset is None:
                            break
                    self.assertIsNone(offset)
                    self.assertEqual(seen, expected)
                    self.assertEqual(engine.store.mail_status()['unread']['codex'], 0)
                    self.assertEqual(len(engine.store.messages(reader='codex')), 150)
                    newest = engine.store.mail('After the walk', 'owner', read_at + 1, to='codex')
                    rows = engine.store.messages(reader='codex')
                    self.assertEqual(len(rows), scheduling.MAX_MESSAGES)
                    self.assertEqual(rows[-1]['id'], newest)
                    self.assertEqual(engine.store.mail_status()['unread']['codex'], 1)
                    self.assertEqual(engine.store.mail_status()['unread']['lead'], 150)

    def test_mail_origin_comes_from_the_current_turn(self):
        store = self.service.engine().store
        for turn_origin in ('lead', None, 'schedule'):
            with self.subTest(turn_origin=turn_origin):
                cap = self.exchange(origin='lead', turn_origin=turn_origin)
                result = self.service.dispatch({'operation': 'post_mailbox', 'capability': cap,
                                                'arguments': {'body': 'Current turn note', 'to': 'lead'}})
                rows = {row['id']: row for row in store.messages()}
                self.assertEqual(rows[result['message_id']]['origin'], turn_origin or '')

    def test_owner_messages_and_replies_never_accept_tasks(self):
        store = self.service.engine().store
        root = store.mail('Thread root', 'owner', NOW)
        task = {'prompt': 'Must not run'}
        for path in ('/schedule-mailbox', '/schedule-mailbox/' + root + '/reply'):
            with self.subTest(path=path):
                response = self.request_owner('POST', path, {'message': 'No task', 'to': 'codex', 'task': task})
                self.assertEqual(response.status_code, 400)
        response = self.client.post('/lead/mailbox/' + root + '/reply', headers=self.lead,
                                    json={'message': 'No task', 'to': 'codex', 'task': task, 'turn_id': 'turn'})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(store.pending_lead())
        self.assertEqual(len(store.messages()), 1)
        server.start_background_task.assert_not_called()

    def test_task_note_cannot_target_owner_or_lead(self):
        for recipient in ('owner', 'lead'):
            with self.subTest(recipient=recipient):
                response = self.client.post('/lead/mailbox', headers=self.lead,
                                            json={'message': 'Invalid recipient', 'to': recipient,
                                                  'turn_id': 'turn', 'task': {'prompt': 'Must not run'}})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.service.engine().store.messages(), [])
        self.assertIsNone(self.service.engine().store.pending_lead())
        server.start_background_task.assert_not_called()

    def test_codex_read_marks_only_its_own_returned_mail_read(self):
        store = self.service.engine().store
        message = store.mail('Unread note', 'owner', NOW)
        cap = self.exchange()
        result = self.service.dispatch({'operation': 'read_mailbox', 'capability': cap, 'arguments': {}})
        self.assertEqual([row['id'] for row in result['items']], [message])
        status = store.mail_status()['unread']
        self.assertEqual(status['codex'], 0)
        self.assertEqual(status['lead'], 1)


if __name__ == '__main__':
    unittest.main()
