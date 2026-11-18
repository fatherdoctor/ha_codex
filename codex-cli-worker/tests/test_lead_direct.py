"""Lead initiative, durable handoff, and mailbox delivery invariants (synthetic only)."""
import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
import scheduling
import test_scheduling

NOW = test_scheduling.NOW


class LeadStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = scheduling.Store(self.path)
        self.worker = test_scheduling.FakeWorker()
        self.engine = scheduling.Engine(self.store, self.worker)

    def test_one_busy_task_waits_durably_and_dispatches_once_without_timezone(self):
        self.worker.blocked = True
        task = self.engine.lead_task('Inspect synthetic data', 'read-only', NOW)
        self.assertFalse(self.worker.sent)
        with self.assertRaisesRegex(scheduling.Busy, 'already waiting'):
            self.engine.lead_task('Second', 'default', NOW)
        engine = scheduling.Engine(scheduling.Store(self.path, zone=None), self.worker)
        self.assertEqual(engine.store.pending_lead()['task_id'], task)
        self.worker.blocked = False
        engine.tick(NOW + 1); engine.tick(NOW + 2)
        self.assertEqual(len(self.worker.sent), 1)
        self.assertEqual(self.worker.sent[0][:3], (task, 'Inspect synthetic data', 'read-only'))
        self.assertIsNone(engine.store.pending_lead())

    def test_pending_waits_through_pause_and_daily_cap(self):
        self.worker.blocked = True
        task = self.engine.lead_task('Wait', 'default', NOW)
        self.worker.blocked = False; self.worker.paused = lambda: True
        self.engine.tick(NOW + 1); self.assertFalse(self.worker.sent)
        with self.assertRaisesRegex(scheduling.Busy, 'paused'):
            self.engine.lead_task('Other', 'default', NOW)
        self.worker.paused = lambda: False
        with patch.object(scheduling, 'MAX_DAILY_RUNS', 1):
            claim = self.store.reserve(NOW); self.store.finish(claim['id'], 'completed')
            self.engine.tick(NOW + 2); self.assertFalse(self.worker.sent)
            self.engine.tick(NOW + 86400)
        self.assertEqual(self.worker.sent[0][0], task)

    def test_crash_after_reservation_never_reexecutes_waiting_task(self):
        self.worker.blocked = True
        task = self.engine.lead_task('Wait', 'default', NOW)
        self.store.reserve(NOW, task_id=task)
        self.worker.blocked = False
        engine = scheduling.Engine(scheduling.Store(self.path), self.worker)
        engine.tick(NOW + 1); engine.tick(NOW + 2)
        self.assertFalse(self.worker.sent)
        self.assertIsNone(self.store.pending_lead())
        self.assertEqual(self.store.runs()[0]['status'], 'interrupted')

    def test_busy_launch_race_restores_waiting_task_without_unique_id_failure(self):
        with patch.object(self.worker, 'submit', side_effect=scheduling.Busy('Race')):
            task = self.engine.lead_task('Wait', 'default', NOW)
        self.assertEqual(self.store.pending_lead()['task_id'], task)
        self.engine.tick(NOW + 1)
        self.assertEqual(self.worker.sent[0][0], task)
        self.assertIsNone(self.store.pending_lead())

    def test_legacy_over_cap_keeps_unread_and_blocks_new_delivery(self):
        for i in range(4): self.store.mail(str(i), 'owner', NOW + i, to='lead')
        with patch.object(scheduling, 'MAX_MESSAGES', 2):
            self.assertEqual(len(self.store.messages(reader='lead', now=NOW + 5)), 4)
            self.assertTrue(self.store.mail_status(NOW + 5)['full']['lead'])
            with self.assertRaises(scheduling.MailboxFull): self.store.mail('No', 'owner', NOW + 5, to='lead')
            ids = [r['id'] for r in self.store.messages(reader='lead', now=NOW + 5)]
            self.store.mark_mail_read('lead', ids[:2], now=NOW + 5)
            self.store.mark_mail_read('lead', ids[2:3], now=NOW + 5)
            self.assertEqual(len(self.store.messages(reader='lead', now=NOW + 5)), 4)
            self.assertEqual(self.store.messages(reader='lead', unread_only=True, now=NOW + 5)[0]['id'], ids[-1])
            newest = self.store.mail('New arrival', 'owner', NOW + 6, to='lead')
            remaining = self.store.messages(reader='lead', now=NOW + 6)
            self.assertEqual([r['id'] for r in remaining], [ids[-1], newest])

    def test_concurrent_submitters_share_single_waiting_slot(self):
        self.worker.blocked = True
        def submit(_):
            try:
                return scheduling.Engine(scheduling.Store(self.path), self.worker).lead_task('Wait', 'default', NOW)
            except scheduling.Busy:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(x is not None for x in pool.map(submit, range(8))), 1)

    def test_unread_survives_days_full_refuses_atomically_read_evicts_first(self):
        self.assertEqual(scheduling.MAX_MESSAGES, 128)
        first = self.store.mail('Old unread', 'owner', NOW, to='lead')
        second = self.store.mail('Read later', 'owner', NOW + 1, to='lead')
        self.store.mark_mail_read('lead', [second], now=NOW + 1)
        with patch.object(scheduling, 'MAX_MESSAGES', 2):
            third = self.store.mail('New', 'owner', NOW + 2, to='lead')
            self.assertEqual([r['id'] for r in self.store.messages(reader='lead', now=NOW + 3)], [first, third])
            self.assertTrue(self.store.mail_status(NOW + 3)['full']['lead'])
            with self.assertRaisesRegex(scheduling.MailboxFull, 'lead mailbox is full of unread'):
                self.store.mail('Refused', 'owner', NOW + 3, to='lead')
        self.assertEqual(len(self.store.messages(reader='lead', now=NOW + 7 * 86400)), 2)
        self.assertFalse(self.store.messages(reader='codex', now=NOW + 7 * 86400))

    def test_read_retention_is_per_reader_and_owner_has_no_age_limit(self):
        mid = self.store.mail('Keep owner and unread Codex', 'owner', NOW)
        self.store.mark_mail_read('lead', [mid], now=NOW)
        future = NOW + 48 * 3600 + 1
        self.assertEqual(self.store.messages(reader='lead', now=future), [])
        self.assertEqual(self.store.messages(reader='owner', now=future)[0]['id'], mid)
        self.assertEqual(self.store.messages(reader='codex', now=future)[0]['id'], mid)
        self.store.clear_mail(reader='owner')
        self.assertEqual(self.store.messages(reader='codex', now=future)[0]['id'], mid)

    def test_legacy_migration_preserves_unread_and_initializes_times(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE mailbox(id TEXT PRIMARY KEY,parent TEXT,author TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL)')
            db.execute('INSERT INTO mailbox VALUES(?,?,?,?,?)', ('a'*32, None, 'owner', 'Legacy', NOW))
        store = scheduling.Store(path)
        row = store.messages(reader='lead', now=NOW)[0]
        self.assertEqual(row['last_modified'], row['sent'])
        self.assertEqual(row['sent'], '2027-03-01T00:00:00.000Z')
        self.assertEqual(store.mail_status(NOW)['unread']['lead'], 1)
        self.assertEqual(scheduling.Store(path).mail_status(NOW)['counts']['owner'], 1)

    def test_envelope_normalizes_dates_reply_edit_and_plain_timezone(self):
        mid = self.store.mail('Body', 'lead', NOW, turn_id='turn', to='codex', due='2027-03-01T02:00:00+01:00')
        row = self.store.messages(now=NOW)[0]
        envelope = scheduling.mail_envelope(row)
        self.assertEqual(list(envelope), ['to', 'from', 'sent', 'last_modified', 'due', 'message'])
        self.assertEqual(envelope['due'], '2027-03-01T01:00:00.000Z')
        self.assertEqual(scheduling.mail_plain(row, 'America/Chicago'), 'To: codex\nFrom: lead\nSent: 2027-02-28 18:00:00 (America/Chicago)\nLast modified: 2027-02-28 18:00:00 (America/Chicago)\nDue: 2027-02-28 19:00:00 (America/Chicago)\n\nBody')
        self.assertIn('(UTC)', scheduling.mail_plain(row))
        self.store.mail('Reply', 'codex', NOW + 1, mid, 'turn', to='lead')
        self.assertEqual(self.store.messages(now=NOW + 1)[0]['last_modified'], scheduling.mail_time(NOW + 1))
        self.store.edit_mail(mid, 'lead', 'Edited', envelope['due'], NOW + 2)
        self.assertEqual(self.store.messages(now=NOW + 2)[0]['last_modified'], scheduling.mail_time(NOW + 2))
        self.assertEqual(self.store.mail_status(NOW + 3601)['due']['codex'][0]['message_id'], mid)
        for invalid in ('tomorrow', '2027-03-01T00:00:00', 1):
            with self.assertRaises(ValueError):
                self.store.mail('Bad due', 'owner', NOW, due=invalid)

    def test_same_clock_edit_and_reply_still_change_last_modified(self):
        mid = self.store.mail('Original', 'owner', NOW)
        initial = self.store.messages(now=NOW)[0]['last_modified']
        self.store.edit_mail(mid, 'owner', 'Edited', None, NOW)
        edited = self.store.messages(now=NOW)[0]['last_modified']
        self.assertGreater(edited, initial)
        self.store.mail('Reply', 'owner', NOW, mid)
        self.assertGreater(self.store.messages(now=NOW)[0]['last_modified'], edited)

    def test_task_mail_and_queue_are_atomic_and_report_once_in_same_thread(self):
        self.worker.blocked = True
        fields = {'message': 'Inspect this', 'turn_id': 'turn', 'to': 'codex'}
        tid = self.engine.lead_task('Inspect', 'default', NOW, mail=fields)
        root = self.store.pending_lead()['thread_id']
        with self.assertRaises(scheduling.Busy):
            self.engine.lead_task('No', 'default', NOW, mail=fields)
        self.assertEqual(len(self.store.messages(now=NOW)), 1)
        self.worker.blocked = False; self.engine.tick(NOW + 1)
        self.assertEqual(self.store.messages(now=NOW + 1)[1]['parent'], root)
        self.worker.tasks[tid] = {'status': 'completed', 'summary': 'Synthetic result'}
        self.worker.blocked = False
        self.engine.tick(NOW + 2); self.engine.tick(NOW + 3)
        rows = self.store.messages(now=NOW + 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['parent'], root)
        self.assertIn('Synthetic result', rows[-1]['body'])
        self.assertEqual(rows[-1]['origin'], 'lead')

    def test_full_mailbox_rolls_back_attached_note_and_waiting_task(self):
        self.store.mail('Unread', 'owner', NOW, to='codex')
        with patch.object(scheduling, 'MAX_MESSAGES', 1), self.assertRaises(scheduling.MailboxFull):
            self.engine.lead_task('Inspect', 'default', NOW, mail={'message': 'No room', 'turn_id': 'turn'})
        self.assertIsNone(self.store.pending_lead()); self.assertFalse(self.worker.sent)
        self.assertEqual(len(self.store.messages(now=NOW)), 1)

    def test_full_result_recipient_retries_after_clear_without_reexecuting(self):
        self.store.mail('Unread for lead', 'owner', NOW, to='lead')
        with patch.object(scheduling, 'MAX_MESSAGES', 2):
            tid = self.engine.lead_task('Inspect', 'default', NOW, mail={'message': 'Inspect', 'turn_id': 'turn'})
            self.worker.tasks[tid] = {'status': 'completed', 'summary': 'Saved result'}; self.worker.blocked = False
            self.engine.tick(NOW + 1)
            self.assertEqual(self.store.mail_status(NOW + 1)['pending_task_replies'], 1)
            self.store.clear_mail(reader='lead'); self.engine.tick(NOW + 2)
            self.assertEqual(self.store.mail_status(NOW + 2)['pending_task_replies'], 0)
        self.assertEqual(len(self.worker.sent), 1)
        self.assertIn('Saved result', self.store.messages(reader='lead', now=NOW + 2)[-1]['body'])


class LeadApiTests(unittest.TestCase):
    setUp = test_scheduling.SchedulingApiTests.setUp
    request_owner = test_scheduling.SchedulingApiTests.request_owner

    def test_attached_task_status_permission_boundary_and_plain_envelope(self):
        payload = {'message': 'Local note', 'turn_id': 'turn', 'task': {'prompt': 'Inspect', 'mode': 'read-only'}}
        self.assertEqual(self.client.post('/lead/mailbox', json=payload).status_code, 401)
        response = self.client.post('/lead/mailbox', json=payload, headers=self.lead)
        self.assertEqual(response.status_code, 202, response.json)
        tid = response.json['task_id']
        self.assertEqual(server.tasks[tid]['turns'][0]['task_source'], 'lead')
        self.assertEqual(server.task_options(tid)['codex_sandbox'], 'read-only')
        self.assertEqual(self.client.post('/tasks/x/approvals/x', json={'decision': 'allow'}, headers=self.lead).status_code, 401)
        row = self.client.get('/lead/mailbox', headers=self.lead).json['mailbox'][0]
        self.assertEqual(row['envelope']['message'], 'Local note')
        self.assertIn('Last modified:', row['text'])
        bad = {**payload, 'task': {'prompt': 'Inspect', 'approval_policy': 'never'}}
        self.assertEqual(self.client.post('/lead/mailbox', json=bad, headers=self.lead).status_code, 400)

    def test_busy_returns_waiting_handle_second_refused_and_no_note_left(self):
        with patch.object(server, '_active_task_ids_locked', return_value=['busy']):
            first = self.client.post('/lead/tasks', json={'prompt': 'Inspect'}, headers=self.lead)
            self.assertEqual(first.status_code, 202); self.assertEqual(first.json['status'], 'waiting')
            status = self.client.get('/lead/tasks/'+first.json['task_id'], headers=self.lead)
            self.assertEqual(status.json['task']['status'], 'waiting')
            second = self.client.post('/lead/mailbox', json={'message': 'No', 'turn_id': 'turn', 'task': {'prompt': 'Second'}}, headers=self.lead)
            self.assertEqual(second.status_code, 409); self.assertIn('already waiting', second.json['error'])
            self.assertFalse(self.service.engine().store.messages())
        server.start_background_task.assert_not_called()

    def test_lead_paging_clamps_marks_only_page_and_consumes_unread_without_skip(self):
        store = self.service.engine().store
        for i in range(25): store.mail('Note '+str(i), 'owner', NOW + i)
        first = self.client.get('/lead/mailbox?limit=50&unread_only=true', headers=self.lead)
        self.assertEqual(len(first.json['mailbox']), 20); self.assertEqual(first.json['next_offset'], 0)
        self.assertEqual(store.mail_status()['unread']['lead'], 5)
        second = self.client.get('/lead/mailbox?limit=20&unread_only=true', headers=self.lead)
        self.assertEqual(len(second.json['mailbox']), 5); self.assertIsNone(second.json['next_offset'])
        self.assertEqual(len(self.client.get('/lead/mailbox?limit=0', headers=self.lead).json['mailbox']), 1)
        self.assertEqual(self.client.get('/lead/mailbox?offset=501', headers=self.lead).status_code, 400)
        self.assertEqual(self.client.get('/lead/mailbox?limit=bad', headers=self.lead).status_code, 400)

    def test_legacy_page_acknowledgement_fits_new_cap_and_reaches_remaining_unread(self):
        store = self.service.engine().store
        for i in range(4): store.mail('Legacy '+str(i), 'lead', NOW + i, turn_id='turn', to='owner')
        with patch.object(scheduling, 'MAX_MESSAGES', 2):
            rows = self.request_owner('GET', '/schedules').json['mailbox']
            self.assertEqual(len(rows), 2)
            self.assertEqual(self.request_owner('POST', '/schedule-mailbox/read', {'ids': [r['id'] for r in rows]}).status_code, 200)
            following = self.request_owner('GET', '/schedules').json['mailbox']
            self.assertEqual(len(following), 2)
            self.assertEqual(following[-1]['envelope']['message'], 'Legacy 3')
            self.assertEqual(len(store.messages(reader='owner')), 4)
            self.assertEqual(self.request_owner('POST', '/schedule-mailbox/read',
                                               {'ids': [r['id'] for r in following]}).status_code, 200)
            self.assertEqual(store.mail_status()['unread']['owner'], 0)

    def test_worker_mail_can_notify_lead_but_cannot_dispatch_lead_work(self):
        tid = 'synthetic-worker'
        server.tasks[tid] = {'status': 'running', 'current_turn_id': 'synthetic-turn', 'task_source': 'schedule',
                             'turns': [{'turn_id': 'synthetic-turn', 'task_source': 'schedule'}]}
        cap = server.ha_data.begin(tid); self.addCleanup(lambda: server.ha_data.end(tid))
        request = {'operation': 'post_mailbox', 'capability': cap, 'arguments': {'body': 'Synthetic note', 'to': 'lead'}}
        result = self.service.dispatch(request)
        self.assertTrue(result['ok'])
        row = self.service.engine().store.messages(reader='lead')[0]
        self.assertEqual(row['origin'], 'schedule')
        for operation, arguments in [('lead_task', {'prompt': 'Start lead'}), ('post_mailbox', {'body': 'No', 'task': {'prompt': 'Start lead'}})]:
            with self.assertRaises(ValueError):
                self.service.dispatch({**request, 'operation': operation, 'arguments': arguments})
        server.start_background_task.assert_not_called()

    def test_clear_confirmation_and_reader_privileges(self):
        store = self.service.engine().store; store.mail('Keep other copies', 'owner', NOW)
        self.assertEqual(self.client.delete('/lead/mailbox', headers=self.lead).status_code, 400)
        self.assertEqual(self.client.delete('/lead/mailbox?mailbox=owner', json={'confirm': True}, headers=self.lead).status_code, 400)
        self.assertEqual(self.client.delete('/lead/mailbox', json={'confirm': True}, headers=self.lead).status_code, 200)
        self.assertEqual(store.messages(reader='lead'), []); self.assertEqual(len(store.messages(reader='owner')), 1)
        response = self.request_owner('DELETE', '/schedule-mailbox?mailbox=codex', {'confirm': True})
        self.assertEqual(response.status_code, 200); self.assertFalse(store.messages(reader='codex'))
        self.assertEqual(len(store.messages(reader='owner')), 1)

    def test_edit_due_scope_and_status_never_contains_body(self):
        store = self.service.engine().store
        lead_id = store.mail('Private body', 'lead', NOW, turn_id='turn', due='2027-03-01T00:00:01Z')
        owner_id = store.mail('Owner body', 'owner', NOW + 1)
        self.assertEqual(self.client.patch('/lead/mailbox/'+owner_id, json={'message': 'Bad'}, headers=self.lead).status_code, 404)
        response = self.client.patch('/lead/mailbox/'+lead_id, json={'message': 'Edited', 'due': '2027-03-01T02:00:00+01:00'}, headers=self.lead)
        self.assertEqual(response.status_code, 200)
        status = store.mail_status(NOW + 3601)
        self.assertNotIn('Edited', str(status)); self.assertTrue(status['due']['owner'])


if __name__ == '__main__': unittest.main()
