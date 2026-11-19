"""The lead agent channel and the mailbox are gone (0.4.0), and an old schedule file still opens.

Each test here fails on version 0.3, where the channel and the mailbox existed.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
import scheduling
import scheduling_mcp


# Every route of the removed channel and mailbox, with a method each one answered to in version 0.3.
REMOVED_ROUTES = (
    ('POST', '/lead-token'),
    ('GET', '/lead/schedules'), ('POST', '/lead/schedules'),
    ('PATCH', '/lead/schedules/sample-id'), ('DELETE', '/lead/schedules/sample-id'),
    ('POST', '/lead/tasks'), ('GET', '/lead/tasks/sample-id'),
    ('GET', '/lead/mailbox'), ('POST', '/lead/mailbox'), ('DELETE', '/lead/mailbox'),
    ('POST', '/lead/mailbox/sample-id/reply'), ('PATCH', '/lead/mailbox/sample-id'),
    ('DELETE', '/lead/mailbox/sample-id'),
    ('GET', '/schedule-mailbox'), ('POST', '/schedule-mailbox'), ('DELETE', '/schedule-mailbox'),
    ('POST', '/schedule-mailbox/read'), ('POST', '/schedule-mailbox/sample-id/reply'),
    ('PATCH', '/schedule-mailbox/sample-id'), ('DELETE', '/schedule-mailbox/sample-id'),
)

# The tables version 0.3 kept for the channel and the mailbox.
OLD_TABLES = {'mailbox', 'mailbox_reads', 'mailbox_deliveries', 'mailbox_threads', 'mail_usage',
              'lead_pending', 'lead_threads'}


def table_names(path):
    """Return the names of the tables in a schedule file."""
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class RemovedRouteTests(unittest.TestCase):
    """No old route answers, for an administrator on the page or for a bearer token."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        patches = [
            patch.object(server, 'schedule_timezone', return_value='UTC'),
            patch.object(server, 'DATA_ROOT', root),
            patch.object(server, 'TASK_STATE_FILE', root / 'tasks.json'),
            patch.object(server, 'api_token', return_value='synthetic-worker-credential'),
            patch.object(server, 'lookup_ha_users', return_value=[
                {'id': 'synthetic-admin', 'is_active': True, 'group_ids': ['system-admin']}]),
            patch.object(server, 'start_background_task'),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        server.admin_cache.clear()
        self.client = server.app.test_client()

    def test_no_lead_or_mailbox_route_answers(self):
        owner = {'X-Ingress-Path': '/sample', 'X-Remote-User-Id': 'synthetic-admin'}
        bearer = {'Authorization': 'Bearer cxlead_' + 'a' * 64}
        for method, path in REMOVED_ROUTES:
            for headers, environ in ((owner, {'REMOTE_ADDR': server.INGRESS_PROXY_IP}), (bearer, {})):
                with self.subTest(method=method, path=path, caller='owner' if headers is owner else 'bearer'):
                    response = self.client.open(path, method=method, json={'confirm': True},
                                                headers=headers, environ_overrides=environ)
                    self.assertEqual(response.status_code, 404)
        server.start_background_task.assert_not_called()

    def test_status_reports_the_mailbox_as_not_available(self):
        response = self.client.get('/status', headers={'Authorization': 'Bearer synthetic-worker-credential'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['mailbox'], {'available': False, 'unread': {'lead': None, 'owner': None}})


class CodexToolTests(unittest.TestCase):
    """Codex is told of, and offered, only the two schedule tools."""

    def test_codex_is_offered_only_the_two_schedule_tools(self):
        self.assertEqual(server.SCHEDULING_TOOLS, ('propose_schedule', 'list_schedules'))
        self.assertEqual([tool['name'] for tool in scheduling_mcp.TOOLS], ['propose_schedule', 'list_schedules'])
        self.assertNotIn('mailbox', server.SCHEDULING_PARAGRAPH.lower())
        with patch.object(scheduling, 'SCHEDULED_RUNS', True):
            arguments = ' '.join(server.scheduling_mcp_args())
            rules = server.build_page_instructions()
        self.assertIn('tools.list_schedules.approval_mode', arguments)
        self.assertNotIn('mailbox', arguments)
        self.assertNotIn('mailbox', rules.lower())

    def test_the_socket_refuses_the_mailbox_operations(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        task_id = 'synthetic-task'
        with patch.object(server, 'DATA_ROOT', Path(temp.name)), \
                patch.object(server, 'schedule_timezone', return_value='UTC'), \
                patch.dict(server.tasks, {task_id: {'status': 'running', 'current_turn_id': 'synthetic-turn'}}):
            capability = server.ha_data.begin(task_id)
            self.addCleanup(lambda: server.ha_data.end(task_id))
            for operation, arguments in (('read_mailbox', {}), ('post_mailbox', {'body': 'A sample message.'})):
                with self.subTest(operation=operation):
                    answer = server.schedule_service.answer(
                        json.dumps({'operation': operation, 'arguments': arguments, 'capability': capability}))
                    self.assertEqual(answer, {'ok': False,
                                              'error': 'Unsupported scheduling operation; approvals are owner-only.'})


class ScheduleFileTests(unittest.TestCase):
    """The schedule file: new ones get no old tables, old ones keep theirs unread."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'schedules.sqlite3'

    def test_a_new_file_has_no_mailbox_or_lead_tables(self):
        scheduling.Store(self.path)
        self.assertEqual(table_names(self.path) & OLD_TABLES, set())
        self.assertEqual(table_names(self.path), {'schedules', 'runs', 'settings'})

    def test_an_old_file_keeps_its_mailbox_tables_unread(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE mailbox (id TEXT PRIMARY KEY, parent TEXT, author TEXT NOT NULL, '
                       'body TEXT NOT NULL, created REAL NOT NULL)')
            db.execute("INSERT INTO mailbox VALUES ('sample-id', NULL, 'codex', 'A sample message.', 1)")
            db.execute('CREATE TABLE lead_threads (task_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, '
                       'started INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0)')
            db.execute("INSERT INTO lead_threads VALUES ('sample-task', 'sample-id', 0, 0)")
        store = scheduling.Store(self.path)
        run = store.reserve(1000.0)
        store.finish(run['id'], 'completed')
        self.assertEqual(store.runs()[0]['status'], 'completed')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT body FROM mailbox').fetchall(), [('A sample message.',)])
            self.assertEqual(db.execute('SELECT count(*) FROM lead_threads').fetchone(), (1,))

    def test_a_proposal_cannot_be_saved_as_the_lead(self):
        store = scheduling.Store(self.path, zone='UTC')
        proposal = {'name': 'Sample', 'prompt': 'Inspect the sample.', 'spec': {'kind': 'interval', 'seconds': 3600}}
        with self.assertRaises(ValueError):
            store.save(proposal, owner=False, author='lead', now=1000.0)
        with self.assertRaises(ValueError):
            store.save(proposal, owner=False, now=1000.0)
        self.assertEqual(store.save(proposal, owner=False, author='codex', now=1000.0)['author'], 'codex')


class PageTests(unittest.TestCase):
    """The page has no lead token or mailbox markup, whether scheduled runs are on or off."""

    def test_the_page_has_no_lead_or_mailbox_markup(self):
        for switch in (False, True):
            with self.subTest(scheduled_runs=switch), patch.object(scheduling, 'SCHEDULED_RUNS', switch):
                page = server.render_index()
                for marker in ('lead-token', 'schedule-mail', 'Lead agent channel', 'Local mailbox'):
                    self.assertNotIn(marker, page)
                self.assertEqual('id="scheduled-work"' in page, switch)


if __name__ == '__main__':
    unittest.main()
