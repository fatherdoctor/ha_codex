"""Scheduled runs are on in this series. The build-time switch still hides them all when it is turned off."""
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
    def test_the_switch_is_on_in_this_series(self):
        self.assertIs(scheduling.SCHEDULED_RUNS, True)

    def test_the_page_carries_the_scheduling_surfaces(self):
        page = server.render_index()
        for marker in SCHEDULE_IDS + ('Schedule this chat', 'Scheduled runs'):
            with self.subTest(marker=marker):
                self.assertIn(marker, page)
        self.assertNotIn('scheduling:begin', page)
        self.assertNotIn('scheduling:end', page)

    def test_codex_is_offered_the_scheduling_tools_and_the_text(self):
        args = ' '.join(server.scheduling_mcp_args())
        for tool in server.SCHEDULING_TOOLS:
            self.assertIn(f'codex_scheduling.tools.{tool}.approval_mode', args)
        self.assertIn('codex_scheduling', ' '.join(server.build_app_server_args()))
        self.assertIn('Use the codex_scheduling tools', server.PAGE_INSTRUCTIONS)
        self.assertIn('Use the codex_scheduling tools', server.build_page_instructions())

    def test_turning_the_switch_off_hides_the_page_the_tool_and_the_text(self):
        with patch.object(scheduling, 'SCHEDULED_RUNS', False):
            page = server.render_index()
            for marker in SCHEDULE_IDS + ('Schedule this chat', 'Scheduled runs', 'scheduling:begin'):
                with self.subTest(marker=marker):
                    self.assertNotIn(marker, page)
            self.assertEqual(server.scheduling_mcp_args(), [])
            self.assertNotIn('codex_scheduling', server.build_page_instructions())

    def test_the_instruction_paragraph_is_kept_in_the_code(self):
        source = Path(server.__file__).read_text(encoding='utf-8')
        self.assertIn('@@SCHEDULING_PARAGRAPH@@', source)


class DocsTests(unittest.TestCase):
    """DOCS.md and README.md describe scheduled runs as available."""

    ROOT = Path(server.__file__).resolve().parent.parent

    def test_the_docs_describe_scheduled_runs(self):
        docs = (self.ROOT / 'docs' / 'app-reference.md').read_text(encoding='utf-8')
        self.assertIn('\n## Scheduled runs\n', docs)
        self.assertIn('choose **Schedule**', docs)
        self.assertIn('`propose_schedule` and `list_schedules`', docs)

    def test_the_readme_describes_scheduled_runs_and_links_the_section(self):
        readme = (self.ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertIn('\n## Scheduled runs\n', readme)
        self.assertIn('docs/app-reference.md#scheduled-runs', readme)
        self.assertIn('(#scheduled-runs)', readme)

    def test_the_documents_name_no_lead_channel_or_mailbox_for_schedules(self):
        for name in ('README.md', 'docs/app-reference.md', 'codex-cli-worker/DOCS.md'):
            text = (self.ROOT / name).read_text(encoding='utf-8')
            self.assertNotIn('the-lead-agent-channel-and-mailbox', text)
            self.assertNotIn('scheduled-runs-and-the-lead-agent-channel', text)


class ScheduleEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = scheduling.Store(Path(self.temp.name) / 'schedules.sqlite3')
        self.worker = test_scheduling.FakeWorker()
        self.engine = scheduling.Engine(self.store, self.worker)
        self.store.save(test_scheduling.proposal(), owner=True, now=NOW)

    def test_a_due_schedule_does_not_fire_while_off_and_is_kept(self):
        with patch.object(scheduling, 'SCHEDULED_RUNS', False):
            self.engine.tick(NOW + 7200)
        self.assertEqual(self.worker.sent, [])
        self.assertEqual(len(self.store.schedules()), 1)
        self.assertTrue(self.store.schedules()[0]['enabled'])

    def test_a_due_schedule_fires_while_on(self):
        self.engine.tick(NOW + 7200)
        self.assertEqual(len(self.worker.sent), 1)


class ScheduleApiTests(test_scheduling.SchedulingApiTests):
    """The owner routes while the switch is off, on the same fixture the scheduling tests use."""

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
