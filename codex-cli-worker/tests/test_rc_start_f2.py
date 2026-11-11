"""Closure fixes for root-only UI files and bounded recovery storage."""
import hashlib
import fcntl
import os
import threading
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import remote_control as rc

PLANTED = 'name: owner edit\nshell_command:\n  example: echo fixture\n'


class WatchClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'config';self.root.mkdir()
        self.data=Path(self.temp.name)/'data';self.data.mkdir()
        self.watch=rc.ConfigWatch(self.root,self.data/'baseline.json')
        self.path=self.root/'configuration.yaml';self.path.write_text('name: baseline\n')
        self.watch.take_baseline()

    def copies(self):
        return [p for p in (self.data/'remote_control_undo').rglob('*') if p.is_file()]

    def test_d1_nested_ui_names_are_removed_or_restored(self):
        for folder in ('packages','nested/packages'):
            for name in ('automations.yaml','scripts.yaml','scenes.yaml'):
                for existing in (False,True):
                    with self.subTest(folder=folder,name=name,existing=existing):
                        path=self.root/folder/name;path.parent.mkdir(parents=True,exist_ok=True)
                        path.unlink(missing_ok=True)
                        if existing:path.write_text('name: baseline\n')
                        self.watch.take_baseline();path.write_text(PLANTED)
                        found=self.watch.new_findings(remote_on=True)
                        report=next(item for item in found if item['file']==path.relative_to(self.root).as_posix())
                        self.assertEqual(report['undone'],'restored' if existing else 'removed')
                        self.assertTrue(report['recovery_copy'])
                        if existing:self.assertEqual(path.read_text(),'name: baseline\n')
                        else:self.assertFalse(path.exists())
                        path.unlink(missing_ok=True)

    def test_d1_root_ui_files_still_report_without_undo(self):
        for name in ('automations.yaml','scripts.yaml','scenes.yaml'):
            with self.subTest(name=name):
                path=self.root/name;path.write_text('name: baseline\n');self.watch.take_baseline()
                path.write_text(PLANTED)
                report=next(item for item in self.watch.new_findings(remote_on=True) if item['file']==name)
                self.assertFalse(report['undone']);self.assertIn('Home Assistant',report['undo_note'])
                self.assertEqual(path.read_text(),PLANTED)

    def test_d2_identical_replants_reuse_one_verified_copy_across_files_and_restart(self):
        reports=[]
        for index in range(5):
            path=self.root/f'package-{index}.yaml';path.write_text(PLANTED)
            if index==3:self.watch=rc.ConfigWatch(self.root,self.data/'baseline.json')
            reports.append(next(item for item in self.watch.new_findings(remote_on=True) if item['file']==path.name))
            self.assertFalse(path.exists())
        self.assertEqual(len({item['recovery_copy'] for item in reports}),1)
        self.assertEqual(len(self.copies()),1)
        self.assertEqual(self.copies()[0].read_text(),PLANTED)
        self.assertEqual(self.copies()[0].stat().st_mode & 0o777,0o600)

    def test_d2_distinct_versions_remain_recoverable(self):
        versions=[PLANTED+'# first\n',PLANTED+'# second\n']
        for body in versions:
            self.path.write_text(body);self.watch.new_findings(remote_on=True)
        self.assertEqual({p.read_text() for p in self.copies()},set(versions))

    def test_d2_copy_count_limit_preserves_old_copies_and_reports_no_undo(self):
        with patch.object(rc,'UNDO_MAX_FILES',2,create=True):
            for index in range(3):
                self.path.write_text(PLANTED+f'# version {index}\n')
                reports=self.watch.new_findings(remote_on=True)
        self.assertEqual(len(self.copies()),2)
        self.assertIn('# version 2',self.path.read_text())
        report=next(item for item in reports if item['file']==self.path.name)
        self.assertFalse(report['undone']);self.assertIn('store is full',report['undo_note'])

    def test_d2_byte_limit_refuses_new_copy_without_losing_current_bytes(self):
        with patch.object(rc,'UNDO_MAX_BYTES',len(PLANTED.encode())-1,create=True):
            self.path.write_text(PLANTED);report=self.watch.new_findings(remote_on=True)[0]
        self.assertEqual(self.path.read_text(),PLANTED);self.assertFalse(report['undone'])
        self.assertEqual(self.copies(),[])

    def test_d2_existing_copy_can_be_reused_even_when_store_is_full(self):
        self.path.write_text(PLANTED);first=self.watch.new_findings(remote_on=True)[0]
        with patch.object(rc,'UNDO_MAX_FILES',1,create=True),patch.object(rc,'UNDO_MAX_BYTES',len(PLANTED.encode()),create=True):
            self.path.write_text(PLANTED);second=self.watch.new_findings(remote_on=True)[0]
        self.assertEqual(first['recovery_copy'],second['recovery_copy'])
        self.assertEqual(second['undone'],'restored');self.assertEqual(len(self.copies()),1)

    def test_d2_old_nested_copies_count_toward_the_limit(self):
        old=self.data/'remote_control_undo/packages/old.yaml.timestamp';old.parent.mkdir(parents=True)
        old.write_text('old owner version\n')
        with patch.object(rc,'UNDO_MAX_FILES',1,create=True):
            self.path.write_text(PLANTED);report=self.watch.new_findings(remote_on=True)[0]
        self.assertEqual(self.path.read_text(),PLANTED);self.assertFalse(report['undone'])
        self.assertEqual(old.read_text(),'old owner version\n')

    def test_d2_corrupt_or_linked_digest_copy_never_justifies_undo(self):
        for linked in (False,True):
            with self.subTest(linked=linked):
                store=self.data/'remote_control_undo';store.mkdir(exist_ok=True)
                candidate=store/(hashlib.sha256(PLANTED.encode()).hexdigest()+'.yaml')
                candidate.unlink(missing_ok=True)
                if linked:
                    outside=self.data/'outside';outside.write_text(PLANTED);candidate.symlink_to(outside)
                else:candidate.write_text('corrupt')
                self.path.write_text(PLANTED);report=self.watch.new_findings(remote_on=True)[0]
                self.assertEqual(self.path.read_text(),PLANTED);self.assertFalse(report['undone'])

    def test_d2_busy_store_reports_without_waiting_for_the_other_writer(self):
        store=self.data/'remote_control_undo';store.mkdir()
        fd=os.open(store,os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(fd,fcntl.LOCK_EX)
        self.path.write_text(PLANTED)
        reports=[]
        thread=threading.Thread(target=lambda:reports.extend(self.watch.new_findings(remote_on=True)))
        thread.start();thread.join(.25)
        completed=not thread.is_alive()
        try:
            self.assertTrue(completed,'A busy recovery store must not stall the watch')
            self.assertEqual(self.path.read_text(),PLANTED);self.assertFalse(reports[0]['undone'])
        finally:
            os.close(fd);thread.join(2)
