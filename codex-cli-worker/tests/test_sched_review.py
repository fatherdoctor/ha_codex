"""CX-SCHED.f regression checks for the lead's F1–F9 review."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_server import server
import scheduling
import approvals
import gate_rules
from test_scheduling import FakeWorker, NOW, proposal
import test_scheduling as fixtures
from test_server import server


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'schedules.sqlite3'
        self.store = scheduling.Store(self.path)

    def test_f1_approved_schedule_survives_lead_edit(self):
        item = self.store.save(proposal(), owner=True, now=NOW)
        with self.assertRaises(LookupError):
            self.store.save(proposal(prompt='Changed'), owner=False, now=NOW, schedule_id=item['id'])
        self.assertEqual(self.store.schedules()[0], item)

    def test_f1_proposals_have_actor_ownership(self):
        item = self.store.save(proposal(), owner=False, author='codex', now=NOW)
        with self.assertRaises(LookupError):
            self.store.save(proposal(), owner=False, author='lead', now=NOW, schedule_id=item['id'])
        with self.assertRaises(LookupError): self.store.delete_proposal(item['id'], 'lead')
        self.store.delete_proposal(item['id'], 'codex')
        self.assertEqual(self.store.schedules(), [])

    def test_f2_per_turn_cap_and_daily_cap_survive_clear(self):
        for i in range(20): self.store.mail('message', 'lead', NOW, turn_id='turn')
        with self.assertRaises(ValueError): self.store.mail('overflow', 'lead', NOW, turn_id='turn')
        self.store.clear_mail()
        with self.assertRaises(ValueError): self.store.mail('overflow', 'lead', NOW, turn_id='turn')
        for i in range(44): self.store.mail('message', 'lead', NOW, turn_id=str(i))
        with self.assertRaises(ValueError): self.store.mail('overflow', 'lead', NOW, turn_id='new')
        self.store.mail('next day', 'lead', NOW + 86400, turn_id='next')
        self.store.mail('owner can still communicate', 'owner', NOW)

    def test_f2_full_mailbox_prunes_threads_and_cannot_brick_channel(self):
        with patch.object(scheduling, 'MAX_MESSAGES', 3):
            first = self.store.mail('root', 'owner', NOW)
            self.store.mail('reply', 'owner', NOW+1, first)
            self.store.mail('second', 'owner', NOW+2)
            self.store.mail('new', 'owner', NOW+3)
            self.assertEqual([m['body'] for m in self.store.messages()], ['second', 'new'])
            self.assertEqual(self.store.clear_mail(), 2)

    def test_f2_lead_clear_only_own_threads(self):
        owner = self.store.mail('owner', 'owner', NOW)
        lead = self.store.mail('lead', 'lead', NOW+1, turn_id='turn')
        self.store.mail('reply', 'owner', NOW+2, lead)
        with self.assertRaises(LookupError): self.store.clear_mail(author='lead', parent=owner)
        self.assertEqual(self.store.clear_mail(author='lead'), 2)
        self.assertEqual([m['id'] for m in self.store.messages()], [owner])

    def test_f4_database_and_persistent_journal_exist_before_task(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        journal = Path(str(self.path)+'-journal')
        self.assertTrue(journal.exists())
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'persist')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_f6_once_uses_home_assistant_wall_time(self):
        item = scheduling.validate(proposal(spec={'kind':'once','at':'2027-03-02T09:00:00'}), NOW, 'America/Chicago')
        self.assertEqual(item['next_due'], datetime.fromisoformat('2027-03-02T15:00:00+00:00').timestamp())
        with self.assertRaises(ValueError): scheduling.validate(proposal(), NOW, None)
        with self.assertRaises(ValueError):
            scheduling.validate(proposal(spec={'kind':'once','at':'2027-03-14T02:30:00'}), NOW, 'America/Chicago')

    def test_f6_cron_keeps_nine_am_across_both_dst_switches(self):
        spec = {'kind':'cron','expression':'0 9 * * *'}
        for start, expected, hours in [('2027-03-13T15:00:00+00:00','2027-03-14T14:00:00+00:00',23), ('2027-11-06T14:00:00+00:00','2027-11-07T15:00:00+00:00',25)]:
            with self.subTest(start=start):
                after = datetime.fromisoformat(start).timestamp()
                due = scheduling.next_due(spec, after, 'America/Chicago')
                self.assertEqual(due, datetime.fromisoformat(expected).timestamp())
                self.assertEqual(due-after, hours*3600)

    def test_f6_missing_minute_skipped_and_repeated_minute_only_first(self):
        after = datetime.fromisoformat('2027-03-14T06:00:00+00:00').timestamp()
        due = scheduling.next_due({'kind':'cron','expression':'30 2 * * *'}, after, 'America/Chicago')
        self.assertEqual(due, datetime.fromisoformat('2027-03-15T07:30:00+00:00').timestamp())
        after = datetime.fromisoformat('2027-11-07T06:30:00+00:00').timestamp()
        due = scheduling.next_due({'kind':'cron','expression':'30 1 * * *'}, after, 'America/Chicago')
        self.assertEqual(due, datetime.fromisoformat('2027-11-08T07:30:00+00:00').timestamp())

    def test_f7_redaction_marker_never_saved(self):
        with self.assertRaises(ValueError): self.store.save(proposal(prompt='Use [redacted]'), owner=True, now=NOW)
        self.assertFalse(self.store.schedules())

    def test_f8_busy_after_reservation_preserves_one_off_and_retries(self):
        item = self.store.save(proposal(spec={'kind':'once','at':NOW+60}), owner=True, now=NOW)
        worker = FakeWorker(); engine = scheduling.Engine(self.store, worker)
        with patch.object(worker, 'submit', side_effect=scheduling.Busy('race')): engine.tick(NOW+61)
        current = self.store.schedules()[0]
        self.assertTrue(current['enabled']); self.assertEqual(current['next_due'], NOW+60)
        self.assertEqual(self.store.runs()[0]['status'], 'deferred')
        engine.tick(NOW+62); self.assertEqual(len(worker.sent), 1)
        self.assertFalse(self.store.schedules()[0]['enabled'])

    def test_f8_deferral_never_undoes_owner_pause(self):
        item = self.store.save(proposal(spec={'kind':'once','at':NOW+60}), owner=True, now=NOW)
        run = self.store.reserve(NOW+61, item['id'])
        self.store.action(item['id'], 'pause', NOW+62)
        self.store.defer(run)
        self.assertFalse(self.store.schedules()[0]['enabled'])

    def test_f9_proposals_cannot_take_owner_capacity(self):
        with patch.object(scheduling, 'MAX_PROPOSALS', 2), patch.object(scheduling, 'MAX_SCHEDULES', 2):
            for i in range(2): self.store.save(proposal(), owner=False, now=NOW)
            with self.assertRaises(ValueError): self.store.save(proposal(), owner=False, now=NOW)
            for i in range(2): self.store.save(proposal(), owner=True, now=NOW)
            candidate = next(s for s in self.store.schedules() if not s['approved'])
            with self.assertRaises(ValueError): self.store.action(candidate['id'], 'approve', NOW, candidate['revision'])
            self.assertEqual(len(self.store.schedules()), 4)

    def test_schema_migration_preserves_unattributed_legacy_proposals(self):
        legacy = Path(self.temp.name)/'old.db'
        with sqlite3.connect(legacy) as db:
            db.execute('CREATE TABLE schedules(id TEXT PRIMARY KEY,name TEXT,prompt TEXT,mode TEXT,spec TEXT,approved INTEGER,enabled INTEGER,next_due REAL,updated REAL,revision TEXT)')
            db.execute('INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?)', ('old','Old','Do nothing','default',json.dumps({'kind':'interval','seconds':60}),0,0,NOW+60,NOW,'revision'))
        store = scheduling.Store(legacy)
        self.assertEqual(store.schedules()[0]['author'], 'owner')
        with self.assertRaises(LookupError): store.delete_proposal('old', 'lead')


class ReviewApiTests(unittest.TestCase):
    setUp = fixtures.SchedulingApiTests.setUp
    request_owner = fixtures.SchedulingApiTests.request_owner
    # Reuse the authenticated, network-free fixture and existing boundary checks.
    def test_f1_lead_delete_refuses_approved_and_other_authors(self):
        value = self.request_owner('POST','/schedules',proposal()).json['schedule']
        self.assertEqual(self.client.delete('/lead/schedules/'+value['id'],headers=self.lead).status_code,404)
        value = self.client.post('/lead/schedules',json=proposal(),headers=self.lead).json['schedule']
        self.assertEqual(self.client.delete('/lead/schedules/'+value['id'],headers=self.lead).status_code,200)

    def test_f2_lead_mail_http_429_and_clear_recovery(self):
        for i in range(20):
            self.assertEqual(self.client.post('/lead/mailbox',json={'body':'sample','turn_id':'turn'},headers=self.lead).status_code,201)
        self.assertEqual(self.client.post('/lead/mailbox',json={'body':'sample','turn_id':'turn'},headers=self.lead).status_code,429)
        self.assertEqual(self.client.delete('/lead/mailbox',headers=self.lead).json['removed'],20)
        self.assertEqual(self.request_owner('POST','/schedule-mailbox',{'body':'owner'}).status_code,201)
        self.assertEqual(self.request_owner('DELETE','/schedule-mailbox').json['removed'],1)

    def test_f2_codex_verified_turn_limit(self):
        task_id='sample'; server.tasks[task_id]={'status':'running','current_turn_id':'verified-turn'}
        cap=server.ha_data.begin(task_id); self.addCleanup(lambda: server.ha_data.end(task_id))
        payload={'operation':'post_mailbox','arguments':{'body':'sample'},'capability':cap}
        for i in range(20): self.service.dispatch(payload)
        with self.assertRaises(scheduling.RateLimit): self.service.dispatch(payload)

    def test_f9_tool_can_page_past_approved_capacity_into_proposals(self):
        store=self.service.engine().store
        for i in range(scheduling.MAX_SCHEDULES): store.save(proposal(),owner=True,now=NOW)
        for i in range(scheduling.MAX_PROPOSALS): store.save(proposal(),owner=False,now=NOW)
        task_id='sample'; server.tasks[task_id]={'status':'running','current_turn_id':'verified-turn'}
        cap=server.ha_data.begin(task_id); self.addCleanup(lambda: server.ha_data.end(task_id))
        result=self.service.dispatch({'operation':'list_schedules','arguments':{'offset':110,'limit':10},'capability':cap})
        self.assertEqual(len(result['items']),10); self.assertIsNone(result['next_offset'])

    def test_f3_background_outcomes_are_log_only_and_chat_still_pushes(self):
        with patch.object(server,'notify') as notify, patch.object(server,'notify_question') as question:
            for source in ('lead','schedule'):
                server.tasks['background']={'task_source':source}
                for status in ('completed','failed','needs_input'):
                    server.notify_task_outcome('background',status,'sample','turn','question',['choice'])
            notify.assert_not_called(); question.assert_not_called()
            server.tasks['chat']={}
            server.notify_task_outcome('chat','completed','sample'); notify.assert_called_once()
            server.notify_task_outcome('chat','needs_input','sample','turn','question',['choice']); question.assert_called_once()

    def test_f7_masked_prompt_flag_and_edit_rejection(self):
        item=self.request_owner('POST','/schedules',proposal(prompt='hidden sample')).json['schedule']
        with patch.object(server,'redact',side_effect=lambda s:s.replace('hidden','[redacted]')):
            row=self.request_owner('GET','/schedules').json['schedules'][0]
        self.assertTrue(row['prompt_redacted'])
        self.assertEqual(self.request_owner('PATCH','/schedules/'+item['id'],proposal(prompt=row['prompt'])).status_code,400)
        self.assertEqual(self.service.engine().store.schedules()[0]['prompt'],'hidden sample')

    def test_f8_dispatch_reserves_and_submits_under_chat_slot_lock(self):
        store=self.service.engine().store
        store.save(proposal(spec={'kind':'once','at':NOW+60}),owner=True,now=NOW)
        original=store.reserve
        def reserve(*args):
            self.assertTrue(server.lock._is_owned())
            return original(*args)
        with patch.object(store,'reserve',side_effect=reserve): self.service.engine().tick(NOW+61)
        server.start_background_task.assert_called_once()


class ProtectionTests(unittest.TestCase):
    def test_f4_database_and_all_sidecars_are_protected(self):
        for suffix in ('','-journal','-wal','-shm'):
            path='/data/schedules.sqlite3'+suffix
            with self.subTest(path=path):
                self.assertTrue(approvals.touches_protected_path(path))
                self.assertTrue(approvals.command_names_protected_path('cat '+path))
                self.assertIsNotNone(gate_rules._policy_restriction('cat '+path))

    def test_f4_wrapper_masks_each_existing_private_file_after_caller_binds(self):
        worker=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); record=root/'argv.json'; real=root/'real'
            real.write_text(f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n'); real.chmod(0o755)
            source=(worker/'codex-bwrap').read_text().replace('REAL_BWRAP=/opt/codex-sandbox/bwrap.real',f'REAL_BWRAP={real}')
            for suffix in ('','-journal','-wal','-shm'):
                (root/('schedules.sqlite3'+suffix)).write_text('synthetic private data')
            source=source.replace('/data/schedules.sqlite3',str(root/'schedules.sqlite3'))
            wrapper=root/'wrapper'; wrapper.write_text(source); wrapper.chmod(0o755)
            subprocess.run([str(wrapper),'--bind',str(root),str(root),'--','/bin/true'],check=True,timeout=5)
            argv=json.loads(record.read_text()); stop=argv.index('--')
            for suffix in ('','-journal','-wal','-shm'):
                path=str(root/('schedules.sqlite3'+suffix)); index=argv.index(path,argv.index('--bind')+3)
                self.assertEqual(argv[index-2:index],['--ro-bind','/dev/null'])
                self.assertLess(index,stop)
