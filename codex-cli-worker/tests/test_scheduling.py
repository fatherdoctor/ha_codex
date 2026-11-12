"""Schedule time edges, durable reservations, owner authority and agent boundaries."""
import concurrent.futures
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_server import server
import scheduling
import schedule_api
import scheduling_mcp

NOW = datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()


def proposal(**changes):
    return {'name': 'Sample inspection', 'prompt': 'Inspect the sample configuration.', 'mode': 'default',
            'spec': {'kind': 'interval', 'seconds': 3600}, **changes}


class FakeWorker:
    def __init__(self):
        self.tasks = {}; self.sent = []; self.blocked = False
    def busy(self):
        return self.blocked
    def snapshot(self, task_id):
        return self.tasks.get(task_id)
    def submit(self, task_id, prompt, mode, name, schedule_id):
        self.sent.append((task_id, prompt, mode, name, schedule_id))
        self.tasks[task_id] = {'status': 'queued'}
        self.blocked = True


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'schedules.sqlite3'
        self.store = scheduling.Store(self.path)
        self.worker = FakeWorker(); self.engine = scheduling.Engine(self.store, self.worker)

    def save(self, **kwargs):
        return self.store.save(proposal(**kwargs), owner=True, now=NOW)

    def test_interval_has_minimum_and_rejects_nonfinite_and_bool(self):
        for value in (0, 59, True, 60.5, float('nan'), 366 * 86400 + 1):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                self.save(spec={'kind': 'interval', 'seconds': value})
        self.assertEqual(self.save(spec={'kind': 'interval', 'seconds': 60})['next_due'], NOW + 60)

    def test_unsupported_modes_fields_and_oversized_prompts_are_refused(self):
        for payload in (proposal(mode='danger-full-access'), proposal(approved=True), proposal(prompt='x' * 16001), proposal(spec={'kind': 'shell', 'command': 'false'})):
            with self.assertRaises(ValueError):
                self.store.save(payload, owner=True, now=NOW)

    def test_once_requires_future_time_with_timezone(self):
        for at in (NOW, 'not-a-date', float('inf'), True):
            with self.assertRaises(ValueError):
                self.save(spec={'kind': 'once', 'at': at})
        value = self.save(spec={'kind': 'once', 'at': '2027-03-02T01:00:00+01:00'})
        self.assertEqual(value['next_due'], NOW + 86400)

    def test_cron_steps_ranges_lists_and_sunday_alias(self):
        value = self.save(spec={'kind': 'cron', 'expression': '*/15 0-2,23 * * 0,7'})
        self.assertEqual(datetime.fromtimestamp(value['next_due'], timezone.utc).isoformat(), '2027-03-07T00:00:00+00:00')
        spec = {'kind': 'cron', 'expression': '*/15 * * * *'}
        self.assertEqual(scheduling.next_due(spec, NOW + 59), NOW + 900)
        self.assertEqual(scheduling.next_due(spec, NOW + 900), NOW + 1800)

    def test_cron_day_fields_use_or_and_wildcard_uses_other_field(self):
        spec = {'kind': 'cron', 'expression': '0 0 2 * 3'}
        self.assertEqual(scheduling.next_due(spec, NOW), NOW + 86400)
        self.assertEqual(scheduling.next_due({'kind': 'cron', 'expression': '0 0 * * 3'}, NOW), NOW + 2 * 86400)
        self.assertEqual(scheduling.next_due({'kind': 'cron', 'expression': '0 0 */2 * 1'}, NOW), NOW + 14 * 86400)

    def test_cron_midnight_month_boundary_and_impossible_day(self):
        date = datetime(2027, 3, 31, 23, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(scheduling.next_due({'kind': 'cron', 'expression': '0 0 1 * *'}, date), date + 60)
        for expression in ('0 0 31 2 *', '60 * * * *', '*/0 * * * *', '* * * *', '0 0 * JAN *', '0 0 * * 8', '* * * * * extra'):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.save(spec={'kind': 'cron', 'expression': expression})

    def test_agent_proposal_and_agent_edits_cannot_self_approve(self):
        value = self.store.save(proposal(), owner=False, now=NOW)
        self.assertFalse(value['approved']); self.assertFalse(value['enabled'])
        self.engine.tick(NOW + 3601); self.assertEqual(self.worker.sent, [])
        edited = self.store.save(proposal(prompt='Inspect another sample.'), owner=False, now=NOW, schedule_id=value['id'])
        with self.assertRaises(ValueError):
            self.store.action(value['id'], 'approve', NOW, value['revision'])
        self.store.action(value['id'], 'approve', NOW, edited['revision'])
        with self.assertRaises(LookupError):
            self.store.save(proposal(), owner=False, now=NOW, schedule_id=value['id'])
        self.assertTrue(self.store.schedules()[0]['enabled'])

    def test_pause_resume_delete_and_expired_once(self):
        value = self.save(); self.store.action(value['id'], 'pause', NOW)
        self.engine.tick(NOW + 4000); self.assertFalse(self.worker.sent)
        self.store.action(value['id'], 'resume', NOW + 4000)
        self.assertEqual(self.store.schedules()[0]['next_due'], NOW + 7600)
        self.store.action(value['id'], 'delete', NOW)
        self.assertEqual(self.store.schedules(), [])
        once = self.save(spec={'kind': 'once', 'at': NOW + 60})
        with self.assertRaises(ValueError):
            self.store.action(once['id'], 'resume', NOW + 61)

    def test_restart_persists_schedule_and_coalesces_missed_intervals(self):
        self.save(); self.store = scheduling.Store(self.path); self.engine = scheduling.Engine(self.store, self.worker)
        self.engine.tick(NOW + 5 * 3600)
        self.assertEqual(len(self.worker.sent), 1)
        self.assertEqual(self.store.schedules()[0]['next_due'], NOW + 6 * 3600)
        self.engine.tick(NOW + 5 * 3600); self.assertEqual(len(self.worker.sent), 1)

    def test_crash_between_reservation_and_task_save_is_not_retried(self):
        value = self.save(); claim = self.store.reserve(NOW + 3600, value['id'])
        self.engine = scheduling.Engine(scheduling.Store(self.path), self.worker)
        self.engine.tick(NOW + 3601)
        self.assertEqual(self.store.runs()[0]['status'], 'interrupted')
        self.assertFalse(self.worker.sent)
        self.assertIn('not retried', self.store.runs()[0]['error'])

    def test_concurrent_reservations_only_claim_one_occurrence(self):
        value = self.save()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: scheduling.Store(self.path).reserve(NOW + 3600, value['id']), range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_reservation_uses_current_approved_payload_after_edit_race(self):
        value = self.save()
        original = self.store.reserve
        def edited(now, schedule_id=None):
            self.store.save(proposal(prompt='Current approved sample.', spec={'kind': 'once', 'at': NOW + 1}), owner=True, now=NOW, schedule_id=value['id'])
            return original(now, schedule_id)
        with patch.object(self.store, 'reserve', side_effect=edited):
            self.engine.tick(NOW + 3600)
        self.assertEqual(self.worker.sent[0][1], 'Current approved sample.')

    def test_native_request_is_parked_and_never_auto_decided(self):
        self.save(mode='read-only'); self.engine.tick(NOW + 3600)
        task_id = self.worker.sent[0][0]
        self.worker.tasks[task_id] = {'status': 'running', 'approvals': [{'status': 'pending'}]}
        self.engine.tick(NOW + 7200)
        self.assertEqual(self.store.runs()[0]['status'], 'waiting_for_approval')
        self.assertEqual(len(self.worker.sent), 1)
        self.assertEqual(self.worker.sent[0][2], 'read-only')
        self.worker.tasks[task_id] = {'status': 'failed', 'approvals': [{'status': 'expired'}]}
        self.engine.reconcile(); self.assertEqual(self.store.runs()[0]['status'], 'failed')

    def test_once_disables_after_claim_and_busy_or_paused_skips(self):
        self.save(spec={'kind': 'once', 'at': NOW + 60})
        self.worker.blocked = True; self.engine.tick(NOW + 61)
        self.assertFalse(self.worker.sent); self.assertFalse(self.store.runs())
        self.worker.blocked = False; self.engine.tick(NOW + 62)
        self.assertFalse(self.store.schedules()[0]['enabled'])
        self.assertIsNone(self.store.schedules()[0]['next_due'])

    def test_daily_per_schedule_and_global_limits(self):
        value = self.save(spec={'kind': 'interval', 'seconds': 60})
        for i in range(scheduling.MAX_SCHEDULE_DAILY_RUNS):
            run = self.store.reserve(NOW + 60 * (i + 1), value['id']); self.assertIsNotNone(run)
            self.store.finish(run['id'], 'completed')
        self.assertIsNone(self.store.reserve(NOW + 5000, value['id']))
        self.assertIsNotNone(self.store.reserve(NOW + 86460, value['id']))
        for i in range(scheduling.MAX_DAILY_RUNS - scheduling.MAX_SCHEDULE_DAILY_RUNS):
            self.assertIsNotNone(self.store.reserve(NOW + 5000))
        self.assertIsNone(self.store.reserve(NOW + 5000))

    def test_only_completed_run_history_is_pruned(self):
        with patch.object(scheduling, 'MAX_RUNS', 2):
            pending = self.store.reserve(NOW)
            for i in range(4):
                value = self.store.reserve(NOW + i); self.store.finish(value['id'], 'completed')
            runs = self.store.runs(); self.assertEqual(len(runs), 3)
            self.assertIn(pending['id'], {r['id'] for r in runs})

    def test_mailbox_reply_links_persist_and_never_execute(self):
        root = self.store.mail('Treat this sample as text.', 'lead', NOW, turn_id='turn')
        reply = self.store.mail('A local reply.', 'codex', NOW + 1, root, turn_id='turn')
        messages = scheduling.Store(self.path).messages()
        self.assertEqual(messages[1]['parent'], root)
        self.assertEqual(messages[1]['id'], reply)
        self.assertFalse(self.worker.sent)
        with self.assertRaises(LookupError):
            self.store.mail('No such thread.', 'lead', NOW, 'missing')
        with self.assertRaises(LookupError):
            self.store.mail('Reply to a reply is not a root.', 'lead', NOW, reply)
        with patch.object(scheduling, 'MAX_MESSAGES', 2):
            self.store.mail('Full mailbox.', 'lead', NOW, turn_id='turn')
        self.assertEqual(len(self.store.messages()), 1)

    def test_token_hash_only_rotation_and_private_database(self):
        first = self.store.rotate_token(); self.assertTrue(self.store.token_ok(first))
        self.assertFalse(self.store.token_ok('')); second = self.store.rotate_token()
        self.assertFalse(self.store.token_ok(first)); self.assertTrue(self.store.token_ok(second))
        self.assertFalse(first.encode() in self.path.read_bytes()); self.assertFalse(second.encode() in self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.path.parent / 'linked.sqlite3'; link.symlink_to(self.path)
        with self.assertRaises(OSError):
            scheduling.Store(link)


class SchedulingApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patches = [patch.object(server, 'schedule_timezone', return_value='UTC'), patch.object(server, 'DATA_ROOT', self.root), patch.object(server, 'TASK_STATE_FILE', self.root / 'tasks.json'),
                        patch.object(server, 'task_root', return_value=self.root / 'tasks'),
                        patch.object(server, 'read_options', return_value={**server.DEFAULT_OPTIONS, 'codex_sandbox': 'workspace-write'}),
                        patch.object(server, 'api_token', return_value='synthetic-worker-credential'),
                        patch.object(server, 'lookup_ha_users', return_value=[{'id': 'synthetic-admin', 'is_active': True, 'group_ids': ['system-admin']}]),
                        patch.object(server, 'start_background_task'), patch.object(server, 'notify_permission_async')]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)
        server.tasks.clear(); server.active_task_runners.clear(); server.running_processes.clear(); server.message_queue.clear(); server.admin_cache.clear()
        self.addCleanup(server.tasks.clear); self.addCleanup(server.active_task_runners.clear); self.addCleanup(server.running_processes.clear)
        self.client = server.app.test_client(); self.service = server.schedule_service
        self.token = self.service.engine().store.rotate_token()
        self.lead = {'Authorization': 'Bearer ' + self.token}
        self.owner = {'X-Ingress-Path': '/sample', 'X-Remote-User-Id': 'synthetic-admin'}
        self.owner_env = {'REMOTE_ADDR': server.INGRESS_PROXY_IP}

    def request_owner(self, method, path, data=None):
        return self.client.open(path, method=method, json=data, headers=self.owner, environ_overrides=self.owner_env)

    def test_lead_requires_separate_token_and_ingress_alone_is_not_a_token(self):
        self.assertEqual(self.client.get('/lead/mailbox').status_code, 401)
        self.assertEqual(self.client.get('/lead/mailbox', headers={'X-Codex-Worker-Token': 'synthetic-worker-credential'}).status_code, 401)
        self.assertEqual(self.request_owner('GET', '/lead/mailbox').status_code, 401)
        self.assertEqual(self.client.get('/lead/mailbox', headers=self.lead).status_code, 200)
        self.assertEqual(self.client.get('/schedules', headers=self.lead).status_code, 401)
        self.assertEqual(self.client.get('/schedules', headers={'X-Codex-Worker-Token': 'synthetic-worker-credential'}).status_code, 403)

    def test_management_requires_real_ingress_administrator(self):
        with patch.object(server, 'lookup_ha_users', return_value=[]):
            self.assertEqual(self.request_owner('POST', '/schedules', proposal()).status_code, 403)
        server.admin_cache.clear()
        result = self.request_owner('POST', '/schedules', proposal())
        self.assertEqual(result.status_code, 201)
        self.assertTrue(result.json['schedule']['approved'])
        self.assertEqual(result.headers['Cache-Control'], 'no-store')

    def test_lead_proposal_cannot_activate_and_edit_invalidates_owner_approval(self):
        result = self.client.post('/lead/schedules', json=proposal(), headers=self.lead)
        self.assertEqual(result.status_code, 201); schedule = result.json['schedule']
        self.assertFalse(schedule['enabled']); self.assertFalse(schedule['approved'])
        endpoint = '/schedules/' + schedule['id'] + '/approve'
        self.assertEqual(self.client.post(endpoint, json={'revision': schedule['revision']}, headers=self.lead).status_code, 401)
        self.assertEqual(self.request_owner('POST', endpoint, {'revision': schedule['revision']}).status_code, 200)
        result = self.client.patch('/lead/schedules/' + schedule['id'], json=proposal(prompt='Changed sample.'), headers=self.lead)
        self.assertEqual(result.status_code, 404)
        self.assertTrue(self.service.engine().store.schedules()[0]['approved'])
        self.assertEqual(self.client.post('/lead/schedules', json=proposal(approved=True), headers=self.lead).status_code, 400)

    def test_redacted_agent_prompt_cannot_be_blindly_approved(self):
        result = self.client.post('/lead/schedules', json=proposal(prompt='A sample that must be hidden.'), headers=self.lead)
        value = result.json['schedule']
        with patch.object(server, 'redact', side_effect=lambda text: '[redacted]' if 'must be hidden' in text else text):
            result = self.request_owner('POST', '/schedules/' + value['id'] + '/approve', {'revision': value['revision']})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.service.engine().store.schedules()[0]['approved'])

    def test_owner_can_edit_pause_resume_and_delete(self):
        value = self.request_owner('POST', '/schedules', proposal()).json['schedule']
        route = '/schedules/' + value['id']
        self.assertEqual(self.request_owner('PATCH', route, proposal(mode='read-only')).status_code, 200)
        self.assertEqual(self.request_owner('POST', route + '/pause', {}).status_code, 200)
        self.assertFalse(self.request_owner('GET', '/schedules').json['schedules'][0]['enabled'])
        self.assertEqual(self.request_owner('POST', route + '/resume', {}).status_code, 200)
        self.assertEqual(self.request_owner('POST', route + '/delete', {}).status_code, 200)
        self.assertEqual(self.request_owner('GET', '/schedules').json['schedules'], [])

    def test_lead_task_uses_ordinary_worker_and_readonly_cannot_expand_policy(self):
        response = self.client.post('/lead/tasks', json={'prompt': 'Inspect sample.', 'mode': 'read-only'}, headers=self.lead)
        self.assertEqual(response.status_code, 202)
        task_id = response.json['task_id']; task = server.tasks[task_id]
        self.assertEqual(task['task_source'], 'lead'); self.assertEqual(task['background_mode'], 'read-only')
        self.assertEqual(server.task_options(task_id)['codex_sandbox'], 'read-only')
        self.assertEqual(server.read_options()['codex_sandbox'], 'workspace-write')
        server.start_background_task.assert_called_once_with(task_id, 'Inspect sample.')
        self.assertEqual(self.client.post('/lead/tasks', json={'prompt': 'No parallel slot.'}, headers=self.lead).status_code, 409)
        server.tasks[task_id]['status'] = 'running'; server.tasks[task_id]['approvals'] = [{'status': 'pending'}]
        result = self.client.get('/lead/tasks/' + task_id, headers=self.lead)
        self.assertEqual(result.json['task']['run_status'], 'waiting_for_approval')
        self.assertEqual(self.client.get('/lead/tasks/missing', headers=self.lead).status_code, 404)

    def test_lead_result_route_cannot_read_unrelated_page_chats(self):
        server.tasks['private-page-chat'] = {'task_id': 'private-page-chat', 'status': 'completed', 'summary': 'Synthetic page-only text.'}
        response = self.client.get('/lead/tasks/private-page-chat', headers=self.lead)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('Synthetic page-only text.', response.get_data(as_text=True))
        server.tasks['scheduled-chat'] = {'task_id': 'scheduled-chat', 'task_source': 'schedule', 'status': 'completed'}
        self.assertEqual(self.client.get('/lead/tasks/scheduled-chat', headers=self.lead).status_code, 200)

    def test_lead_cannot_answer_native_approval_or_create_bypass_fields(self):
        self.assertEqual(self.client.post('/tasks/sample/approvals/sample', json={'decision': 'allow'}, headers=self.lead).status_code, 401)
        for data in ({'prompt': 'Sample', 'mode': 'danger-full-access'}, {'prompt': 'Sample', 'approval_policy': 'never'}):
            self.assertEqual(self.client.post('/lead/tasks', json=data, headers=self.lead).status_code, 400)
        server.start_background_task.assert_not_called()

    def test_saved_readonly_task_still_refuses_an_invalid_global_policy(self):
        task_id = 'synthetic-task'
        server.tasks[task_id] = {'status': 'queued', 'task_source': 'schedule', 'background_mode': 'read-only'}
        with patch.object(server, 'read_options', return_value={**server.DEFAULT_OPTIONS, 'codex_sandbox': 'danger-full-access'}), \
             patch.object(server, 'fail_task_launch') as refused, \
             patch.object(server, 'record_baseline') as baseline:
            server.run_task(task_id, 'Inspect the sample configuration.')
        refused.assert_called_once(); baseline.assert_not_called()

    def test_policy_refusal_is_not_bypassed_by_lead_submission(self):
        with patch.object(server.protected_policy, 'policy_problem', return_value='Synthetic policy refusal.'):
            result = self.client.post('/lead/tasks', json={'prompt': 'Inspect sample.'}, headers=self.lead)
        self.assertEqual(result.status_code, 400); server.start_background_task.assert_not_called()
        self.assertEqual(self.service.engine().store.runs()[0]['status'], 'failed')

    def test_paused_worker_and_oversized_or_malformed_json_do_not_launch(self):
        with patch.object(server, 'assistant_paused', return_value=True):
            self.assertEqual(self.client.post('/lead/tasks', json={'prompt': 'Sample'}, headers=self.lead).status_code, 409)
        self.assertEqual(self.client.post('/lead/tasks', data='not json', content_type='application/json', headers=self.lead).status_code, 400)
        self.assertEqual(self.client.post('/lead/tasks', json={'prompt': 'x' * 70000}, headers=self.lead).status_code, 400)
        server.start_background_task.assert_not_called()

    def test_local_mailbox_roundtrip_and_token_rotation_never_launch_tasks(self):
        result = self.client.post('/lead/mailbox', json={'turn_id': 'sample-turn', 'body': 'A sample request.'}, headers=self.lead)
        message_id = result.json['message_id']
        self.assertEqual(self.request_owner('POST', '/schedule-mailbox/' + message_id + '/reply', {'body': 'A local answer.'}).status_code, 201)
        self.assertEqual(len(self.client.get('/lead/mailbox', headers=self.lead).json['mailbox']), 2)
        result = self.request_owner('POST', '/lead-token', {'confirm': True})
        self.assertEqual(result.status_code, 200); self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get('/lead/mailbox', headers=self.lead).status_code, 401)
        self.assertTrue(self.service.engine().store.token_ok(result.json['token']))
        server.start_background_task.assert_not_called()
        self.assertEqual(server.redact(result.json['token']), '[redacted]')

    def test_proposal_tool_requires_active_turn_and_has_no_approval_operation(self):
        task_id = 'synthetic-task'; server.tasks[task_id] = {'status': 'running', 'current_turn_id': 'synthetic-turn'}
        cap = server.ha_data.begin(task_id); self.addCleanup(lambda: server.ha_data.end(task_id))
        def call(op, args):
            return self.service.dispatch({'operation': op, 'arguments': args, 'capability': cap})
        value = call('propose_schedule', proposal()); self.assertFalse(value['approved']); self.assertFalse(value['enabled'])
        self.assertEqual(len(call('list_schedules', {})['items']), 1)
        first = call('post_mailbox', {'body': 'A sample tool message.'})
        call('post_mailbox', {'body': 'A sample reply.', 'parent': first['message_id']})
        self.assertEqual(len(call('read_mailbox', {})['items']), 2)
        with self.assertRaises(ValueError):
            call('approve_schedule', {'id': value['schedule_id']})
        server.tasks[task_id]['current_turn_id'] = 'different-turn'
        with self.assertRaises(ValueError):
            call('propose_schedule', proposal())
        server.start_background_task.assert_not_called()

    def test_tool_pages_are_bounded_and_reject_invalid_limits(self):
        task_id = 'synthetic-task'; server.tasks[task_id] = {'status': 'running', 'current_turn_id': 'synthetic-turn'}
        cap = server.ha_data.begin(task_id); self.addCleanup(lambda: server.ha_data.end(task_id))
        for i in range(3):
            self.service.engine().store.mail('Sample text ' + str(i), 'owner', NOW + i)
        result = self.service.dispatch({'operation': 'read_mailbox', 'arguments': {'offset': 1, 'limit': 1}, 'capability': cap})
        self.assertEqual(len(result['items']), 1); self.assertEqual(result['next_offset'], 2)
        for limit in (0, 21, True, '1'):
            with self.assertRaises(ValueError):
                self.service.dispatch({'operation': 'read_mailbox', 'arguments': {'limit': limit}, 'capability': cap})

    def test_tool_transport_roundtrip_and_expired_capability(self):
        # The HA pytest harness blocks sockets; allow only this local Unix
        # transport, keeping all IP networking blocked for this test.
        if os.environ.get('PYTEST_CURRENT_TEST'):
            import pytest_socket
            pytest_socket.socket_allow_hosts([], allow_unix_socket=True)
        task_id = 'synthetic-task'; server.tasks[task_id] = {'status': 'running', 'current_turn_id': 'synthetic-turn'}
        cap = server.ha_data.begin(task_id); self.addCleanup(lambda: server.ha_data.end(task_id))
        path = self.root / 'schedules.sock'; listener = self.service.serve(path)
        self.addCleanup(listener.server_close); self.addCleanup(listener.shutdown)
        with patch.dict('os.environ', {'HA_DATA_CAPABILITY': cap, 'CODEX_SCHEDULE_SOCKET': str(path)}):
            response = scheduling_mcp.fetch('propose_schedule', proposal())
            self.assertTrue(response['ok']); self.assertFalse(response['enabled'])
            server.ha_data.end(task_id)
            self.assertFalse(scheduling_mcp.fetch('read_mailbox', {})['ok'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_native_session_gets_actual_readonly_sandbox_and_on_request_approvals(self):
        task_id = 'synthetic-task'; server.tasks[task_id] = {'status': 'running', 'task_source': 'schedule', 'background_mode': 'read-only', 'current_turn_id': 'synthetic-turn'}
        proc = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
        with patch.object(server, 'AppServerSession') as constructor:
            server.create_approval_session(task_id, proc, 'Sample prompt', self.root / 'final.json', None, {}, server.task_options(task_id))
        kwargs = constructor.call_args.kwargs
        self.assertEqual(kwargs['thread_params']['sandbox'], 'read-only')
        self.assertEqual(kwargs['turn_params']['sandboxPolicy'], {'type': 'readOnly', 'networkAccess': False})
        self.assertEqual(kwargs['turn_params']['approvalPolicy'], 'on-request')
        args = server.build_app_server_args()
        self.assertTrue(any('scheduling_mcp.py' in a for a in args))
