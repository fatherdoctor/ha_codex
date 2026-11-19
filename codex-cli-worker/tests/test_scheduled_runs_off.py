"""Scheduled runs are switched off in this series: no page, tool or timer shows or runs them, and the switch restores all."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_server import server
import scheduling
import test_scheduling

NOW = datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()
SCHEDULE_IDS = ('schedule-dialog', 'schedule-form', 'schedule-list', 'schedule-runs', 'schedule-timezone', 'data-action="schedule"')


class SwitchTests(unittest.TestCase):
    def test_the_switch_is_off_in_this_series(self):
        self.assertIs(scheduling.SCHEDULED_RUNS, False)

    def test_no_page_carries_a_scheduling_surface_while_off(self):
        page = server.render_index()
        for marker in SCHEDULE_IDS + ('Schedule this chat', 'Scheduled runs', 'scheduling:begin'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, page)

    def test_the_page_served_at_the_root_is_the_same(self):
        body = server.app.test_client().get('/').get_data(as_text=True)
        self.assertNotIn('schedule-dialog', body)
        self.assertNotIn('data-action="schedule"', body)

    def test_codex_is_offered_no_scheduling_tool_while_off(self):
        self.assertEqual(server.scheduling_mcp_args(), [])
        self.assertNotIn('codex_scheduling', ' '.join(server.build_app_server_args()))
        self.assertNotIn('codex_scheduling', server.PAGE_INSTRUCTIONS)
        self.assertNotIn('codex_scheduling', server.build_page_instructions())

    def test_turning_the_switch_on_brings_back_the_page_the_tool_and_the_text(self):
        with patch.object(scheduling, 'SCHEDULED_RUNS', True):
            page = server.render_index()
            for marker in SCHEDULE_IDS:
                with self.subTest(marker=marker):
                    self.assertIn(marker, page)
            self.assertNotIn('scheduling:begin', page)
            self.assertIn('Use the codex_scheduling tools', server.build_page_instructions())
            args = ' '.join(server.scheduling_mcp_args())
            for tool in server.SCHEDULING_TOOLS:
                self.assertIn(f'codex_scheduling.tools.{tool}.approval_mode', args)

    def test_the_instruction_paragraph_is_kept_in_the_code(self):
        source = Path(server.__file__).read_text(encoding='utf-8')
        self.assertIn('@@SCHEDULING_PARAGRAPH@@', source)  # The paragraph is kept in the code, not deleted.


class DocsTests(unittest.TestCase):
    """DOCS.md and README.md say nothing about scheduled runs in this series."""

    ROOT = Path(server.__file__).resolve().parent.parent
    # The lead channel section is CX-04-LEAD-RM.a's to reword; it still names the lead schedule routes.
    LEAD_HEADING = '## The lead agent channel and mailbox'
    # Plain uses of the word that are not about scheduled runs.
    ALLOWED = ('not refreshed on a schedule', 'instead schedules')

    def docs_without_lead_section(self):
        text = (self.ROOT / 'codex-cli-worker' / 'DOCS.md').read_text(encoding='utf-8')
        start = text.index(self.LEAD_HEADING)
        end = text.find('\n## ', start + 1)
        return text[:start] + (text[end:] if end != -1 else '')

    def hits(self, text):
        return [line for line in text.splitlines()
                if 'schedul' in line.lower() and not any(phrase in line for phrase in self.ALLOWED)]

    def test_docs_outside_the_lead_section_do_not_mention_scheduling(self):
        self.assertEqual(self.hits(self.docs_without_lead_section()), [])

    def test_the_readme_does_not_mention_scheduling(self):
        self.assertEqual(self.hits((self.ROOT / 'README.md').read_text(encoding='utf-8')), [])

    def test_the_lead_section_title_and_the_readme_link_agree(self):
        docs = (self.ROOT / 'codex-cli-worker' / 'DOCS.md').read_text(encoding='utf-8')
        self.assertIn(self.LEAD_HEADING + '\n', docs)
        self.assertNotIn('scheduled-runs', docs)
        readme = (self.ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertIn('DOCS.md#the-lead-agent-channel-and-mailbox', readme)


class ScheduleEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = scheduling.Store(Path(self.temp.name) / 'schedules.sqlite3')
        self.worker = test_scheduling.FakeWorker()
        self.engine = scheduling.Engine(self.store, self.worker)
        self.store.save(test_scheduling.proposal(), owner=True, now=NOW)

    def test_a_due_schedule_does_not_fire_while_off_and_is_kept(self):
        self.engine.tick(NOW + 7200)
        self.assertEqual(self.worker.sent, [])
        self.assertEqual(len(self.store.schedules()), 1)
        self.assertTrue(self.store.schedules()[0]['enabled'])

    def test_the_same_schedule_fires_once_the_switch_is_on(self):
        with patch.object(scheduling, 'SCHEDULED_RUNS', True):
            self.engine.tick(NOW + 7200)
        self.assertEqual(len(self.worker.sent), 1)


class ScheduleApiTests(test_scheduling.SchedulingApiTests):
    """The owner routes while off, on the same fixture the scheduling tests use."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(scheduling, 'SCHEDULED_RUNS', False); patcher.start(); self.addCleanup(patcher.stop)

    def test_the_list_is_empty_but_the_saved_schedule_stays(self):
        self.service.engine().store.save(test_scheduling.proposal(), owner=True, now=NOW)
        result = self.request_owner('GET', '/schedules')
        self.assertEqual((result.json['schedules'], result.json['runs'], result.json['scheduled_runs']), ([], [], False))
        self.assertEqual(len(self.service.engine().store.schedules()), 1)

    def test_a_schedule_cannot_be_saved_changed_or_acted_on(self):
        saved = self.service.engine().store.save(test_scheduling.proposal(), owner=True, now=NOW)
        body = {'name': 'Sample', 'prompt': 'Inspect the sample configuration.', 'mode': 'default',
                'spec': {'kind': 'interval', 'seconds': 3600}}
        self.assertEqual(self.request_owner('POST', '/schedules', body).status_code, 404)
        self.assertEqual(self.request_owner('PATCH', f"/schedules/{saved['id']}", body).status_code, 404)
        self.assertEqual(self.request_owner('POST', f"/schedules/{saved['id']}/pause", {}).status_code, 404)

    def test_the_held_count_is_logged_once(self):
        self.service.engine().store.save(test_scheduling.proposal(), owner=True, now=NOW)
        with patch('builtins.print') as shown:
            self.service.log_held_schedules()
        shown.assert_called_once()
        self.assertIn('1 saved schedule', shown.call_args.args[0])


# Only the tests written here run in this class; the parent's own tests run in test_scheduling.
for _name in [n for n in dir(test_scheduling.SchedulingApiTests) if n.startswith('test_')]:
    setattr(ScheduleApiTests, _name, None)
