"""The app installs and updates its bundled Codex integration in /config, and leaves other installs alone."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SYNC_PATH = Path(__file__).resolve().parents[1] / "integration_sync.py"
SPEC = importlib.util.spec_from_file_location("integration_sync", SYNC_PATH)
assert SPEC is not None and SPEC.loader is not None
integration_sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = integration_sync  # dataclasses look the module up by name
SPEC.loader.exec_module(integration_sync)

MARKER = integration_sync.MARKER_NAME


class SyncTestCase(unittest.TestCase):
    """A bundle in the image, an empty configuration folder and empty app storage, all temporary."""

    def setUp(self):
        """Make a bundle of version 0.2.0 and point the target and backup at empty temporary folders."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bundled = self.root / "image" / "codex_cli"
        self.target = self.root / "config" / "custom_components" / "codex_cli"
        self.backup = self.root / "data" / "integration_backup" / "codex_cli"
        self.work = self.root / "config" / ".codex_cli_install"
        self.make_bundle("0.2.0", "new code")

    def make_bundle(self, version: str, code: str) -> None:
        """Write a bundle with the given manifest version and __init__.py text."""
        self.write_folder(self.bundled, version, code)

    @staticmethod
    def write_folder(folder: Path, version: str, code: str) -> None:
        """Write an integration folder with a manifest and one code file."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(json.dumps({"domain": "codex_cli", "version": version}))
        (folder / "__init__.py").write_text(code)

    def sync(self, app_version: str = "0.2.0"):
        """Run one sync of the bundle into the target."""
        return integration_sync.sync(self.bundled, self.target, self.backup, app_version, self.work)

    def leftovers(self) -> list[str]:
        """Return what a sync left behind: anything in custom_components but the target, and the work folder."""
        found = [p.name for p in self.target.parent.iterdir() if p != self.target] if self.target.parent.exists() else []
        if self.work.exists() or self.work.is_symlink():
            found.append(self.work.name)
        return sorted(found)


class InstallAndUpdateTests(SyncTestCase):
    """What a sync writes, and when it writes nothing."""

    def test_fresh_install_writes_the_bundle_and_a_marker(self):
        """With no folder, the bundle is written as it is, plus a marker naming the app and bundle."""
        result = self.sync()
        self.assertEqual((result.action, result.version, result.installed_version), ("installed", "0.2.0", ""))
        self.assertEqual((self.target / "__init__.py").read_text(), "new code")
        marker = json.loads((self.target / MARKER).read_text())
        self.assertEqual(marker["app_version"], "0.2.0")
        self.assertEqual(marker["version"], "0.2.0")
        self.assertEqual(marker["digest"], integration_sync.tree_digest(self.bundled))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(self.backup.exists(), "nothing to back up on a fresh install")

    def test_second_start_with_the_same_bundle_writes_nothing(self):
        """A folder that already holds this bundle is left as it is."""
        self.sync()
        mtime = (self.target / "__init__.py").stat().st_mtime_ns
        self.assertEqual(self.sync().action, "current")
        self.assertEqual((self.target / "__init__.py").stat().st_mtime_ns, mtime)

    def test_update_replaces_an_older_bundle_and_backs_it_up(self):
        """A folder the app wrote from an older bundle is replaced; the old one is kept in app storage."""
        self.sync()
        self.make_bundle("0.3.0", "newer code")
        result = self.sync("0.3.0")
        self.assertEqual((result.action, result.version, result.installed_version), ("updated", "0.3.0", "0.2.0"))
        self.assertEqual((self.target / "__init__.py").read_text(), "newer code")
        self.assertEqual(json.loads((self.target / MARKER).read_text())["version"], "0.3.0")
        self.assertEqual((self.backup / "__init__.py").read_text(), "new code")
        self.assertEqual(self.leftovers(), [])

    def test_a_changed_bundle_with_the_same_version_is_still_written(self):
        """The marker records the bundle's contents, so a rebuilt bundle is installed even at one version."""
        self.sync()
        self.make_bundle("0.2.0", "fixed code")
        self.assertEqual(self.sync().action, "updated")
        self.assertEqual((self.target / "__init__.py").read_text(), "fixed code")

    def test_caches_are_not_copied_and_do_not_count_as_changes(self):
        """__pycache__ in the bundle or the installed folder neither travels nor triggers an update."""
        (self.bundled / "__pycache__").mkdir()
        (self.bundled / "__pycache__" / "x.pyc").write_bytes(b"cache")
        self.sync()
        self.assertFalse((self.target / "__pycache__").exists())
        (self.target / "__pycache__").mkdir()
        (self.target / "__pycache__" / "y.pyc").write_bytes(b"other cache")
        self.assertEqual(self.sync().action, "current")

    def test_an_image_without_a_bundle_writes_nothing(self):
        """An image built without the integration reports no_bundle and leaves /config alone."""
        (self.bundled / "manifest.json").unlink()
        result = self.sync()
        self.assertEqual(result.action, "no_bundle")
        self.assertFalse(self.target.parent.exists())


class OtherInstallTests(SyncTestCase):
    """Folders the app did not write are never changed."""

    def test_marketplace_folder_without_a_marker_is_left_alone(self):
        """A folder from the Marketplace, HACS or a manual copy has no marker and is not touched."""
        self.write_folder(self.target, "0.1.0", "marketplace code")
        result = self.sync()
        self.assertEqual((result.action, result.installed_version), ("foreign", "0.1.0"))
        self.assertEqual((self.target / "__init__.py").read_text(), "marketplace code")
        self.assertFalse((self.target / MARKER).exists())
        self.assertFalse(self.backup.exists())

    def test_marketplace_update_over_an_app_install_hands_the_folder_over(self):
        """When the Marketplace replaces the folder it drops the marker, and the app then leaves it alone."""
        self.sync()
        (self.target / MARKER).unlink()
        (self.target / "__init__.py").write_text("marketplace code")
        self.assertEqual(self.sync().action, "foreign")
        self.assertEqual((self.target / "__init__.py").read_text(), "marketplace code")

    def test_a_symbolic_link_is_left_alone(self):
        """A linked development copy is never replaced, even if it carries a marker."""
        source = self.root / "dev" / "codex_cli"
        self.write_folder(source, "0.0.1", "dev code")
        (source / MARKER).write_text(json.dumps({"digest": "old"}))
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(source)
        self.assertEqual(self.sync().action, "foreign")
        self.assertEqual((source / "__init__.py").read_text(), "dev code")

    def test_a_folder_written_over_in_place_is_left_alone(self):
        """Another installer that copies over the app's folder keeps the marker; the app still leaves it."""
        self.sync()
        self.write_folder(self.target, "0.5.0", "other installer code")
        result = self.sync()
        self.assertEqual((result.action, result.installed_version), ("foreign", "0.5.0"))
        self.assertIn("changed after the app wrote it", result.detail)
        self.make_bundle("0.2.1", "app update")
        self.assertEqual(self.sync("0.2.1").action, "foreign")
        self.assertEqual((self.target / "__init__.py").read_text(), "other installer code")
        self.assertFalse(self.backup.exists())

    def test_a_hand_edit_is_kept_through_an_app_update(self):
        """An owner's edit to a file of the app's folder makes it theirs; the next app update leaves it."""
        self.sync()
        (self.target / "__init__.py").write_text("new code # my fix")
        self.make_bundle("0.2.2", "app update")
        self.assertEqual(self.sync("0.2.2").action, "foreign")
        self.assertEqual((self.target / "__init__.py").read_text(), "new code # my fix")

    def test_a_folder_missing_a_file_is_reported_not_current(self):
        """A damaged folder (a file removed) no longer matches the marker and is reported, not called current."""
        self.sync()
        (self.target / "__init__.py").unlink()
        self.assertEqual(self.sync().action, "foreign")

    def test_a_linked_custom_components_is_left_alone(self):
        """When custom_components is a link, nothing is written where it points."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.target.parent.parent.mkdir(parents=True)
        self.target.parent.symlink_to(elsewhere)
        result = self.sync()
        self.assertEqual(result.action, "foreign")
        self.assertIn("link", result.detail)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_an_unreadable_marker_counts_as_another_install(self):
        """A marker that is not this app's JSON object does not let the app overwrite the folder."""
        self.write_folder(self.target, "0.1.0", "hand-edited code")
        (self.target / MARKER).write_text("not json")
        self.assertEqual(self.sync().action, "foreign")
        self.assertEqual((self.target / "__init__.py").read_text(), "hand-edited code")


class FailureTests(SyncTestCase):
    """A failed copy leaves the old folder in place and no temporary folders behind."""

    def setUp(self):
        """Start from a folder the app wrote from an older bundle, then offer a newer bundle."""
        super().setUp()
        self.make_bundle("0.1.0", "old code")
        self.sync("0.1.0")
        self.make_bundle("0.2.0", "new code")

    def assert_old_folder_kept(self, result) -> None:
        """The sync failed, the old folder and its marker are intact, and nothing is left over."""
        self.assertEqual(result.action, "failed")
        self.assertTrue(result.detail)
        self.assertEqual((self.target / "__init__.py").read_text(), "old code")
        self.assertEqual(json.loads((self.target / MARKER).read_text())["version"], "0.1.0")
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_copy_leaves_the_old_folder(self):
        """A copy that stops part way (for example a full disk) changes nothing in the target."""
        with patch.object(integration_sync.shutil, "copytree", side_effect=OSError(28, "No space left on device")):
            self.assert_old_folder_kept(self.sync())

    def test_a_failed_swap_puts_the_old_folder_back(self):
        """If the new folder cannot be renamed into place, the old folder is renamed back."""
        real_rename = os.rename

        def rename(src, dst):
            if Path(src).name == "new":
                raise OSError(13, "Permission denied")
            return real_rename(src, dst)

        with patch.object(integration_sync.os, "rename", side_effect=rename):
            self.assert_old_folder_kept(self.sync())

    def test_leftover_folders_from_a_crash_are_replaced(self):
        """Folders left in the work folder by a start that stopped part way do not block the next sync."""
        for name in ("new", "old"):
            leftover = self.work / name
            leftover.mkdir(parents=True)
            (leftover / "junk").write_text("junk")
        self.assertEqual(self.sync().action, "updated")
        self.assertFalse((self.target / "junk").exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_planted_link_in_place_of_the_work_folder_is_not_followed(self):
        """A symbolic link in place of the work folder fails the sync; what it points to survives."""
        victim = self.root / "data" / "keep"
        victim.mkdir(parents=True)
        (victim / "secret").write_text("keep me")
        self.work.symlink_to(victim)
        result = self.sync()
        self.assertEqual(result.action, "failed")
        self.assertEqual((victim / "secret").read_text(), "keep me")
        self.assertEqual((self.target / "__init__.py").read_text(), "old code")


class WorkFolderTests(SyncTestCase):
    """Copies being written or removed are never in custom_components, where Home Assistant loads from."""

    def test_the_new_and_old_copies_are_made_outside_custom_components(self):
        """During an update the staging and old folders are in the work folder, not next to the target."""
        self.sync()
        self.make_bundle("0.3.0", "newer code")
        seen = []
        real_swap = integration_sync.swap_in

        def swap(staging, target, old):
            seen.append(sorted(p.name for p in target.parent.iterdir()))
            real_swap(staging, target, old)
            seen.append(sorted(p.name for p in target.parent.iterdir()))
            self.assertEqual((old / "__init__.py").read_text(), "new code")

        with patch.object(integration_sync, "swap_in", side_effect=swap):
            self.assertEqual(self.sync("0.3.0").action, "updated")
        self.assertEqual(seen, [["codex_cli"], ["codex_cli"]])
        self.assertEqual(self.leftovers(), [])

    def test_a_start_with_a_current_folder_removes_a_leftover(self):
        """A copy left by an interrupted start is removed even when there is nothing to write."""
        self.sync()
        (self.work / "old").mkdir(parents=True)
        (self.work / "old" / "manifest.json").write_text("{}")
        self.assertEqual(self.sync().action, "current")
        self.assertEqual(self.leftovers(), [])

    def test_an_old_copy_that_cannot_be_removed_is_reported(self):
        """The update still counts, the result says the old copy stayed, and the next start removes it."""
        self.sync()
        self.make_bundle("0.3.0", "newer code")
        real_clear = integration_sync.clear_work_dir
        calls = []

        def clear(work):
            calls.append(work)
            if len(calls) == 2:
                raise OSError(16, "Device or resource busy")
            real_clear(work)

        with patch.object(integration_sync, "clear_work_dir", side_effect=clear):
            result = self.sync("0.3.0")
        self.assertEqual(result.action, "updated")
        self.assertIn("old copy was not removed", result.detail)
        self.assertEqual(self.leftovers(), [".codex_cli_install"])
        self.assertEqual(self.sync("0.3.0").action, "current")
        self.assertEqual(self.leftovers(), [])


class NoticeTests(unittest.TestCase):
    """The notification text for each result."""

    def notice(self, action, **fields):
        """Return the notice for a result with the given action and fields."""
        return integration_sync.notice_for(integration_sync.SyncResult(action, **fields), "0.2.0")

    def test_install_and_update_ask_for_one_restart(self):
        """Both ask for a restart, under one id, so an update replaces an unread install notice."""
        installed = self.notice("installed", version="0.2.0")
        updated = self.notice("updated", version="0.2.0", installed_version="0.1.0")
        self.assertEqual(installed[0], updated[0])
        self.assertIn("Restart Home Assistant once", installed[2])
        self.assertIn("Discovered: Codex", installed[2])
        # After a stop between the two renames the folder is rewritten as "installed" for an integration that
        # may already be set up, so the text does not claim it is not.
        self.assertIn("If Codex is not set up yet", installed[2])
        self.assertIn("from version 0.1.0", updated[2])

    def test_another_install_is_explained(self):
        """The notice says the folder was left as it is and how to hand it to the app."""
        title_and_text = " ".join(self.notice("foreign", version="0.2.0", installed_version="0.1.0")[1:])
        self.assertIn("left", title_and_text)
        self.assertIn("version 0.1.0", title_and_text)
        self.assertIn("(0.2.0)", title_and_text)

    def test_quiet_results_raise_nothing(self):
        """Nothing is shown when the folder is current, the option is off, or there is no bundle."""
        for action in ("current", "disabled", "no_bundle"):
            self.assertIsNone(self.notice(action))


class MainTests(SyncTestCase):
    """The start-up entry point: the option, the notices, discovery, and never failing the start."""

    def setUp(self):
        """Point the module's fixed paths at the temporary folders and record Supervisor calls."""
        super().setUp()
        self.options = self.root / "data" / "options.json"
        self.state = self.root / "data" / "integration_sync.json"
        self.options.parent.mkdir(parents=True, exist_ok=True)
        for name, value in {
            "BUNDLED_DIR": self.bundled,
            "TARGET_DIR": self.target,
            "BACKUP_DIR": self.backup,
            "OPTIONS_PATH": self.options,
            "NOTICE_STATE_PATH": self.state,
            "WORK_DIR": self.work,
            "NOTICE_RETRY_SECONDS": 0,
        }.items():
            self.enterContext(patch.object(integration_sync, name, value))
        self.enterContext(patch.dict(os.environ, {"ADDON_VERSION": "0.2.0", "SUPERVISOR_TOKEN": "token"}))
        self.posts: list[tuple[str, dict]] = []
        self.accept = True
        self.enterContext(patch.object(integration_sync, "supervisor_post", side_effect=self.post))
        self.enterContext(patch("builtins.print"))

    def post(self, path, payload, token):
        """Record one Supervisor call and answer as set in self.accept."""
        self.posts.append((path, payload))
        return self.accept

    def notices(self) -> list[str]:
        """Return the ids of the notifications shown."""
        return [p["notification_id"] for path, p in self.posts if path.endswith("persistent_notification/create")]

    def test_fresh_install_shows_the_restart_notice_and_leaves_discovery_to_announce(self):
        """A first start installs and asks for a restart; it does not post discovery, which --announce does."""
        self.assertEqual(integration_sync.main([]), 0)
        self.assertTrue((self.target / "manifest.json").exists())
        self.assertEqual(self.notices(), [integration_sync.NOTICE_RESTART])
        self.assertNotIn("/discovery", [path for path, _ in self.posts])

    def test_option_off_writes_nothing(self):
        """With install_integration false, /config is not touched and nothing is posted."""
        self.options.write_text(json.dumps({"install_integration": False}))
        self.assertEqual(integration_sync.main([]), 0)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.posts, [])

    def test_the_kept_notice_is_removed_once_the_folder_is_the_apps_again(self):
        """After a 'left as installed' notice, a start that finds the app's own folder dismisses it once."""
        self.write_folder(self.target, "0.1.0", "marketplace code")
        integration_sync.main([])
        self.assertEqual(self.notices(), [integration_sync.NOTICE_KEPT])
        shutil.rmtree(self.target)  # the owner removes the other install
        integration_sync.main([])  # the app installs its own copy
        integration_sync.main([])  # and finds it current
        dismissed = [p["notification_id"] for path, p in self.posts if path.endswith("persistent_notification/dismiss")]
        self.assertEqual(dismissed, [integration_sync.NOTICE_KEPT])
        self.assertNotIn("kept_notice_version", json.loads(self.state.read_text()))

    def test_a_kept_notice_that_cannot_be_dismissed_is_tried_again(self):
        """If Core refuses the dismissal, the record stays and the next start tries again."""
        self.state.write_text(json.dumps({"kept_notice_version": "0.2.0"}))
        self.accept = False
        integration_sync.main([])
        self.assertEqual(json.loads(self.state.read_text())["kept_notice_version"], "0.2.0")
        self.accept = True
        integration_sync.main([])
        self.assertNotIn("kept_notice_version", json.loads(self.state.read_text()))

    def test_a_damaged_record_is_not_replayed_as_a_notice(self):
        """Only a restart notice is ever owed; a record holding another id shows nothing."""
        integration_sync.main([])
        self.posts.clear()
        self.state.write_text(json.dumps({"restart_notice": ["something_else", "title", "message"]}))
        integration_sync.main([])
        self.assertEqual(self.notices(), [])

    def test_a_file_name_that_cannot_be_encoded_is_reported_not_raised(self):
        """A ValueError from the copy (a name that cannot be encoded) gives the failed notice, not a traceback."""
        with patch.object(integration_sync, "tree_digest", side_effect=UnicodeEncodeError("utf-8", "x", 0, 1, "bad")):
            self.assertEqual(integration_sync.main([]), 0)
        self.assertEqual(self.notices(), [integration_sync.NOTICE_FAILED])

    def test_option_missing_means_on(self):
        """Options written before this version have no install_integration; the default is on."""
        self.options.write_text(json.dumps({"codex_model": "default"}))
        self.assertTrue(integration_sync.install_option_on(self.options))
        self.options.unlink()
        self.assertTrue(integration_sync.install_option_on(self.options))

    def test_another_install_is_reported_once_per_app_version(self):
        """The 'left as installed' notice is shown on the first start of each app version only."""
        self.write_folder(self.target, "0.1.0", "marketplace code")
        integration_sync.main([])
        integration_sync.main([])
        self.assertEqual(self.notices(), [integration_sync.NOTICE_KEPT])
        with patch.dict(os.environ, {"ADDON_VERSION": "0.3.0"}):
            integration_sync.main([])
        self.assertEqual(self.notices(), [integration_sync.NOTICE_KEPT] * 2)

    def test_an_unshown_notice_is_tried_again_next_start(self):
        """If Core cannot take the notice (still starting), it is retried, and recorded only once shown."""
        self.write_folder(self.target, "0.1.0", "marketplace code")
        self.accept = False
        integration_sync.main([])
        self.assertEqual(self.notices(), [integration_sync.NOTICE_KEPT] * integration_sync.NOTICE_ATTEMPTS)
        self.assertFalse(self.state.exists())
        self.accept = True
        self.posts.clear()
        integration_sync.main([])
        self.assertEqual(self.notices(), [integration_sync.NOTICE_KEPT])

    def test_an_unshown_restart_notice_is_posted_at_later_starts(self):
        """If Core cannot take the restart notice, the next starts post it until it is shown once."""
        self.accept = False
        integration_sync.main([])
        self.assertEqual(self.notices(), [integration_sync.NOTICE_RESTART] * integration_sync.NOTICE_ATTEMPTS)
        self.accept = True
        self.posts.clear()
        integration_sync.main([])
        self.assertEqual(self.notices(), [integration_sync.NOTICE_RESTART])
        self.posts.clear()
        integration_sync.main([])
        self.assertEqual(self.notices(), [])

    def test_an_io_error_before_the_copy_is_reported(self):
        """An error reading the bundle or the folder gives the failed notice, and the start goes on."""
        with patch.object(integration_sync, "tree_digest", side_effect=OSError(5, "Input/output error")):
            self.assertEqual(integration_sync.main([]), 0)
        self.assertEqual(self.notices(), [integration_sync.NOTICE_FAILED])

    def test_a_notice_record_that_cannot_be_saved_does_not_stop_the_start(self):
        """When the record cannot be written, the start goes on; at worst a notice is shown again."""
        self.write_folder(self.target, "0.1.0", "marketplace code")
        self.state.mkdir()  # writing a file at this path fails
        self.assertEqual(integration_sync.main([]), 0)
        self.assertEqual(self.notices(), [integration_sync.NOTICE_KEPT])

    def test_a_failed_copy_does_not_stop_the_start(self):
        """A failure is reported in a notice and main still returns 0, so run.sh starts the worker."""
        with patch.object(integration_sync.shutil, "copytree", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(integration_sync.main([]), 0)
        self.assertEqual(self.notices(), [integration_sync.NOTICE_FAILED])


class AnnounceTests(unittest.TestCase):
    """--announce posts discovery only once Home Assistant has loaded the integration."""

    def setUp(self):
        """Answer flow_handlers reads from self.answers in turn and record posts and waits."""
        self.answers: list = []
        self.posts: list[str] = []
        self.waits = 0
        self.enterContext(patch.object(integration_sync, "core_knows_integration", side_effect=lambda token: self.answers.pop()))
        self.enterContext(patch.object(integration_sync, "supervisor_post", side_effect=self.post))
        self.enterContext(patch("builtins.print"))

    def post(self, path, payload, token):
        """Record one post and accept it."""
        self.posts.append(path)
        return True

    def run_announce(self, *answers):
        """Run announce with core_knows_integration giving the answers in order; return the waits."""
        self.answers.extend(reversed(answers))

        def sleep(seconds):
            self.waits += 1

        integration_sync.announce("token", sleep=sleep)
        return self.waits

    def test_a_loaded_integration_is_announced_at_once(self):
        """When Home Assistant already lists codex_cli (an update, a later app start), discovery goes out now."""
        self.assertEqual(self.run_announce(True), 0)
        self.assertEqual(self.posts, ["/discovery"])

    def test_a_fresh_install_waits_for_the_restart(self):
        """Not listed yet (written after Home Assistant started): wait, and announce once it is listed."""
        self.assertEqual(self.run_announce(False, False, None, False, True), 4)
        self.assertEqual(self.posts, ["/discovery"])

    def test_an_unreadable_core_announces_after_the_limit(self):
        """If Home Assistant cannot be read ANNOUNCE_UNREAD_LIMIT times in a row, the app announces anyway."""
        limit = integration_sync.ANNOUNCE_UNREAD_LIMIT
        self.assertEqual(self.run_announce(*([None] * limit)), limit - 1)
        self.assertEqual(self.posts, ["/discovery"])

    def test_main_with_announce_only_announces(self):
        """main(["--announce"]) does not sync and calls announce with the Supervisor token."""
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "token"}), \
                patch.object(integration_sync, "announce") as announce, \
                patch.object(integration_sync, "sync") as sync:
            self.assertEqual(integration_sync.main(["--announce"]), 0)
        announce.assert_called_once_with("token")
        sync.assert_not_called()


class CoreKnowsIntegrationTests(unittest.TestCase):
    """Reading Home Assistant's list of flow handlers."""

    def answer(self, response):
        """Return what core_knows_integration says when the Supervisor request gives this response."""
        with patch.object(integration_sync, "supervisor_request", return_value=response) as request:
            known = integration_sync.core_knows_integration("token")
        request.assert_called_once_with("GET", integration_sync.FLOW_HANDLERS_PATH, "token")
        return known

    @staticmethod
    def response(body):
        """A response whose json() returns body, or raises ValueError when body is that class."""
        def json_body():
            if body is ValueError:
                raise ValueError("not JSON")
            return body
        return type("Response", (), {"json": staticmethod(json_body)})()

    def test_listed_unlisted_and_unreadable(self):
        """codex_cli in the list is True, a list without it False, anything else None."""
        self.assertIs(self.answer(self.response(["hue", "codex_cli"])), True)
        self.assertIs(self.answer(self.response(["hue"])), False)
        self.assertIsNone(self.answer(None))
        self.assertIsNone(self.answer(self.response({"message": "unexpected"})))
        self.assertIsNone(self.answer(self.response(ValueError)))


class SupervisorRequestTests(unittest.TestCase):
    """The only network call goes to the Supervisor, with no proxy and no redirects."""

    def test_no_proxy_and_no_redirects(self):
        """The session ignores proxy settings from the environment and the request never follows a redirect."""
        sessions = []

        class Session:
            def __init__(self):
                self.trust_env = True
                self.kwargs = None
                sessions.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def request(self, method, url, **kwargs):
                self.trust_env_at_post = self.trust_env
                self.method, self.url, self.kwargs = method, url, kwargs
                return type("Response", (), {"status_code": 200})()

        with patch.object(integration_sync.requests, "Session", Session):
            self.assertTrue(integration_sync.supervisor_post("/discovery", {}, "token"))
        (session,) = sessions
        self.assertEqual((session.method, session.url), ("POST", "http://supervisor/discovery"))
        self.assertFalse(session.trust_env_at_post)
        self.assertIs(session.kwargs["allow_redirects"], False)


class DigestTests(unittest.TestCase):
    """What the digest of a folder counts."""

    def test_caches_and_the_marker_do_not_change_the_digest(self):
        """__pycache__ (written by Home Assistant) and the marker are left out; a code change is not."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "__init__.py").write_text("code")
            before = integration_sync.tree_digest(folder)
            (folder / "__pycache__").mkdir()
            (folder / "__pycache__" / "x.pyc").write_bytes(b"cache")
            (folder / MARKER).write_text("{}")
            self.assertEqual(integration_sync.tree_digest(folder), before)
            (folder / "__init__.py").write_text("changed")
            self.assertNotEqual(integration_sync.tree_digest(folder), before)


class PackagingTests(unittest.TestCase):
    """The app and the bundled integration ship as one release."""

    REPO = Path(__file__).resolve().parents[2]

    def test_app_and_integration_versions_match(self):
        """config.yaml and manifest.json carry the same version, so the bundle matches the app."""
        config = (self.REPO / "codex-cli-worker" / "config.yaml").read_text(encoding="utf-8")
        app_version = next(
            line.split(":", 1)[1].strip().strip('"') for line in config.splitlines() if line.startswith("version:")
        )
        manifest = json.loads((self.REPO / "custom_components" / "codex_cli" / "manifest.json").read_text())
        self.assertEqual(app_version, manifest["version"])

    def test_the_option_is_declared_and_defaults_on(self):
        """install_integration is an option with a bool schema, default true, and a translated label."""
        config = (self.REPO / "codex-cli-worker" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("  install_integration: true\n", config)
        self.assertIn("  install_integration: bool\n", config)
        translations = (self.REPO / "codex-cli-worker" / "translations" / "en.yaml").read_text(encoding="utf-8")
        self.assertIn("  install_integration:\n    name: ", translations)

    def test_the_image_bundles_the_integration_and_runs_the_sync_first(self):
        """The Dockerfile copies the bundle and the script; run.sh runs the script before the server."""
        dockerfile = (self.REPO / "codex-cli-worker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY integration /usr/share/ha_codex/integration\n", dockerfile)
        self.assertIn("COPY integration_sync.py /\n", dockerfile)
        run = (self.REPO / "codex-cli-worker" / "run.sh").read_text(encoding="utf-8")
        self.assertLess(run.index("python3 /integration_sync.py ||"), run.index("python3 /server.py"))
        # Discovery waits in the background for Home Assistant to load the integration; it must not block.
        self.assertLess(run.index("python3 /integration_sync.py --announce &\n"), run.index("python3 /server.py"))


if __name__ == "__main__":
    unittest.main()
