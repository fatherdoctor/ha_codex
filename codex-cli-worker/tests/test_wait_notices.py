"""Task wait clocks and mailbox reply signals use invented local data only."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
import scheduling
W = server
import test_scheduling

START = '2026-11-16T00:00:00+00:00'


def task(**fields):
    return {'task_id': 'a' * 32, 'current_turn_id': 'b' * 32, 'status': 'waiting_for_input',
            'question': 'Which example should I use?', 'input_requested_at': START, **fields}


class Waits(unittest.TestCase):
    def test_an_unrelated_root_thread_update_does_not_answer_a_nested_question(self):
        with tempfile.TemporaryDirectory() as directory:
            store = scheduling.Store(Path(directory) / 'mail.sqlite3')
            root = store.mail('Thread context', 'lead', 10, turn_id='lead-turn')
            question = store.mail('An owner question', 'codex', 11, parent=root, turn_id='task:turn', to='owner')
            store.mail('Unrelated progress update', 'lead', 12, parent=root, turn_id='lead-turn')
            self.assertFalse(store.mail_replied(question))
            with self.assertRaises(LookupError):
                store.mail('A child is not a reply target', 'owner', 13, parent=question, to='codex')
            direct_question = store.mail('A separate question', 'codex', 14, turn_id='task:turn', to='owner')
            store.mail('The answer', 'owner', 15, parent=direct_question, to='codex')
            restarted = scheduling.Store(store.path)
            self.assertFalse(restarted.mail_replied(question))
            self.assertTrue(restarted.mail_replied(direct_question))

    def test_a_live_thread_can_still_receive_replies_after_its_root_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            store = scheduling.Store(Path(directory) / 'mail.sqlite3')
            root = store.mail('Thread context', 'lead', 10, turn_id='lead-turn')
            store.mail('Unread response', 'codex', 11, parent=root, turn_id='task:turn')
            with store.connect() as db:
                db.execute('DELETE FROM mailbox WHERE id=?', (root,))
                db.execute('DELETE FROM mailbox_deliveries WHERE message_id=?', (root,))
            reply = store.mail('Another response', 'owner', 12, parent=root)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT parent FROM mailbox WHERE id=?', (reply,)).fetchone()[0], root)

    def test_all_wait_kinds_have_ids_and_explicit_times_without_bodies(self):
        rows = W.task_waits([task(), task(task_id='c' * 32, status='running', approvals=[
            {'approval_id': 'd' * 32, 'status': 'pending', 'requested_at': START,
             'command': 'private command', 'reason': 'private reason'}]),
            task(task_id='e' * 32, question='', mailbox_wait={
                'message_id': 'f' * 32, 'turn_id': 'b' * 32, 'requested_at': START})])
        self.assertEqual([r['kind'] for r in rows], ['question', 'approval', 'mailbox'])
        self.assertEqual(len({r['wait_id'] for r in rows}), 3)
        self.assertNotIn('private', str(rows))
        self.assertNotIn('Which example', str(rows))
        self.assertTrue(all(set(r) == {'wait_id', 'task_id', 'kind', 'waiting_since', 'notice_at'} for r in rows))

    def test_resolution_new_turn_and_cancellation_remove_waits(self):
        for status in ('queued', 'completed', 'failed', 'cancelled'):
            with self.subTest(status=status):
                self.assertEqual(W.task_waits([task(status=status)]), [])
        self.assertEqual(W.task_waits([task(cancellation_requested=True)]), [])
        self.assertNotEqual(W.task_waits([task()])[0]['wait_id'],
                            W.task_waits([task(current_turn_id='new-turn')])[0]['wait_id'])
        self.assertEqual(W.task_waits([task(status='running', approvals=[
            {'status': 'accepted', 'requested_at': START}])]), [])

    def test_mail_reply_and_old_turn_remove_only_that_mail_wait(self):
        mail = {'message_id': 'f' * 32, 'turn_id': 'b' * 32, 'requested_at': START}
        current = task(question='', mailbox_wait=mail)
        self.assertEqual(len(W.task_waits([current])), 1)
        self.assertEqual(W.task_waits([current], {'f' * 32}), [])
        current['current_turn_id'] = 'next-turn'
        self.assertEqual(W.task_waits([current]), [])

    def test_bad_times_are_not_used_to_invent_wait_duration(self):
        for stamp in (None, '', 'bad', '2026-11-16T00:00:00', -1):
            with self.subTest(stamp=stamp):
                self.assertEqual(W.task_waits([task(input_requested_at=stamp)]), [])

    def test_delay_default_and_bounds(self):
        self.assertEqual(W.wait_seconds(60, 3600), 1800)
        self.assertEqual(W.wait_seconds(5, 3600), 300)
        self.assertEqual(W.wait_seconds(60, 40), 20)
        self.assertEqual(W.wait_seconds(10080, 86400), 43200)
        self.assertEqual(W.wait_minutes(5), 5)
        self.assertEqual(W.wait_minutes(10080), 10080)
        for value in (None, '5', True, 0, -1, 10081, float('nan')):
            self.assertEqual(W.wait_minutes(value), 60)

    def test_question_timestamp_is_durable_and_not_touched_by_other_updates(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'tasks', {}), \
                patch.object(server, 'get_task_dir', return_value=Path(directory)), \
                patch.object(server, 'save_task_index'), patch.object(server, 'utc_now', return_value=START):
            server.update_task('t', status='waiting_for_input', question='Which example?')
            saved = copy.deepcopy(server.tasks['t'])
            with patch.object(server, 'utc_now', return_value='2026-11-16T01:00:00+00:00'):
                server.update_task('t', details='More context')
                self.assertEqual(server.tasks['t']['input_requested_at'], START)
                server.update_task('t', question='A different question?')
                self.assertNotEqual(server.tasks['t']['input_requested_at'], START)
                server.tasks['t'] = saved  # Model a restart from the durable task file.
                self.assertEqual(W.task_waits([{'task_id': 't', **saved}])[0]['waiting_since'],
                                 W.requested_time(START))
                server.update_task('t', status='running')
                self.assertEqual(server.tasks['t']['input_requested_at'], '')

    def test_mail_reply_observation_is_thread_scoped_and_does_not_acknowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            store = scheduling.Store(Path(directory) / 'mail.sqlite3')
            question = store.mail('Question', 'codex', 10, turn_id='task:turn', to='owner')
            other = store.mail('Another question', 'codex', 11, turn_id='task:turn', to='owner')
            store.mail('Unrelated answer', 'owner', 12, parent=other, to='codex')
            self.assertFalse(store.mail_replied(question))
            answer = store.mail('Answer', 'owner', 13, parent=question, to='codex')
            self.assertTrue(store.mail_replied(question))
            with store.connect() as db:
                self.assertIsNone(db.execute('SELECT 1 FROM mailbox_reads WHERE message_id=? AND reader=?',
                                             (answer, 'codex')).fetchone())

    def test_mail_reply_after_clock_correction_still_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            store = scheduling.Store(Path(directory) / 'mail.sqlite3')
            question = store.mail('Question', 'codex', 20, turn_id='task:turn', to='owner')
            store.mail('Answer after a clock correction', 'owner', 19, parent=question, to='codex')
            self.assertTrue(store.mail_replied(question))


class WaitApi(unittest.TestCase):
    setUp = test_scheduling.SchedulingApiTests.setUp
    request_owner = test_scheduling.SchedulingApiTests.request_owner

    def begin(self):
        task_id = 'synthetic-task'
        server.tasks[task_id] = {'task_id': task_id, 'status': 'running', 'current_turn_id': 'synthetic-turn'}
        cap = server.ha_data.begin(task_id)
        self.addCleanup(lambda: server.ha_data.end(task_id))
        return task_id, cap

    def post(self, cap, **arguments):
        return self.service.dispatch({'operation': 'post_mailbox', 'capability': cap,
                                      'arguments': {'body': 'Synthetic question', 'to': 'owner', **arguments}})

    def test_explicit_mail_wait_is_saved_replied_and_not_replaced(self):
        task_id, cap = self.begin()
        self.post(cap)
        self.assertNotIn('mailbox_wait', server.tasks[task_id])
        result = self.post(cap, await_reply=True)
        wait = server.tasks[task_id]['mailbox_wait']
        self.assertEqual(wait['message_id'], result['message_id'])
        self.assertEqual(wait['turn_id'], 'synthetic-turn')
        self.assertEqual(len(W.task_waits([server.tasks[task_id]])), 1)
        with self.assertRaisesRegex(ValueError, 'already waits'):
            self.post(cap, await_reply=True)
        response = self.request_owner('POST', '/schedule-mailbox/' + result['message_id'] + '/reply',
                                      {'message': 'An owner answer', 'to': 'codex'})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(self.service.mailbox_replied(result['message_id']))
        self.assertEqual(W.task_waits([server.tasks[task_id]], {result['message_id']}), [])
        self.post(cap, await_reply=True)  # A later, separate wait is permitted after the answer.

    def test_invalid_mail_wait_flags_and_ended_turn_refuse_without_mail(self):
        _, cap = self.begin()
        for arguments in ({'await_reply': 'yes'}, {'await_reply': True, 'to': 'lead'},
                          {'await_reply': True, 'to': 'codex'}, {'await_reply': True, 'parent': 'd' * 32}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.post(cap, **arguments)
        self.assertEqual(self.service.engine().store.mail_status()['messages'], [])
        server.ha_data.end('synthetic-task')
        with self.assertRaisesRegex(ValueError, 'ended'):
            self.post(cap, await_reply=True)

    def test_status_reports_all_waits_and_uses_the_owner_option(self):
        task_id, _ = self.begin()
        server.tasks[task_id].update(status='waiting_for_input', question='Example question',
                                     input_requested_at=START)
        with patch.object(server, 'refresh_usage_status_async'), \
                patch.object(server, 'codex_login_status', return_value={'status_ok': True}), \
                patch.object(server, 'usage_status_payload', return_value={}), \
                patch.object(server, 'auth_status_payload', return_value={}), \
                patch.object(server, 'remote_control_summary', return_value={}), \
                patch.object(server, 'read_options', return_value={'unanswered_wait_minutes': 5}):
            response = self.client.get('/status', headers={'X-Codex-Worker-Token': 'synthetic-worker-credential'})
        self.assertEqual(response.status_code, 200)
        waits = response.get_json()['task_waits']
        self.assertEqual(waits['minutes'], 5)
        self.assertTrue(waits['available'])
        self.assertEqual(waits['items'][0]['kind'], 'question')
        self.assertNotIn('Example question', str(waits))


if __name__ == '__main__':
    unittest.main()
