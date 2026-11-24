"""AGENTS.md lives in the app's own addon_config folder, and Codex loads it from there."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app_folders import isolated_app_folders
from test_server import server

ROOT = Path(__file__).resolve().parents[1]


class AgentsHome(unittest.TestCase):
    """migrate_agents_file() and link_agents_file() in a temporary tree shaped like the app's folders."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "config"
        self.addon = self.root / "addon_config"
        self.data = self.root / "data"
        self.home = self.data / "codex-home"
        for path in (self.config, self.addon, self.home):
            path.mkdir(parents=True)
        self.legacy = self.config / "AGENTS.md"
        self.agents = self.addon / "AGENTS.md"
        self.link = self.home / "AGENTS.md"
        self.backup = self.data / "agents_before_move.md"
        patches = patch.multiple(
            server, AGENTS_PATH=self.agents, LEGACY_AGENTS_PATH=self.legacy, CODEX_HOME=self.home,
            CODEX_AGENTS_LINK=self.link, AGENTS_MOVE_BACKUP=self.backup,
            CODEX_CONFIG_PATH=self.home / "config.toml")
        patches.start()
        self.addCleanup(patches.stop)

    def run_quiet(self, function):
        with contextlib.redirect_stdout(io.StringIO()) as log:
            result = function()
        self.log = log.getvalue()
        return result

    # The move

    def test_an_old_file_in_config_is_moved_after_a_backup(self):
        self.legacy.write_text("Owner rules\n", encoding="utf-8")
        self.assertTrue(self.run_quiet(server.migrate_agents_file))
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "Owner rules\n")
        self.assertEqual(self.backup.read_text(encoding="utf-8"), "Owner rules\n")
        self.assertIn(str(self.agents), self.log)

    def test_the_move_is_not_a_copy_codex_would_read_both(self):
        self.legacy.write_text("Owner rules\n", encoding="utf-8")
        self.run_quiet(server.migrate_agents_file)
        self.assertFalse(os.path.lexists(self.legacy))

    def test_nothing_to_move_changes_nothing(self):
        self.assertFalse(self.run_quiet(server.migrate_agents_file))
        self.assertFalse(self.agents.exists())
        self.assertFalse(self.backup.exists())

    def test_a_file_in_both_places_is_left_alone_and_logged(self):
        self.legacy.write_text("Old\n", encoding="utf-8")
        self.agents.write_text("New\n", encoding="utf-8")
        self.assertFalse(self.run_quiet(server.migrate_agents_file))
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "Old\n")
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "New\n")
        self.assertIn("Both", self.log)

    def test_a_link_in_config_is_not_moved(self):
        target = self.root / "elsewhere.md"
        target.write_text("Owner text behind a link.\n", encoding="utf-8")
        self.legacy.symlink_to(target)
        self.assertFalse(self.run_quiet(server.migrate_agents_file))
        self.assertTrue(self.legacy.is_symlink())
        self.assertFalse(self.agents.exists())

    def test_a_failed_move_keeps_the_old_file_and_does_not_stop_the_start(self):
        self.legacy.write_text("Owner rules\n", encoding="utf-8")
        with patch.object(server.shutil, "copy2", side_effect=OSError(28, "No space left on device")):
            self.assertFalse(self.run_quiet(server.migrate_agents_file))
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "Owner rules\n")
        self.assertFalse(self.agents.exists())
        self.assertIn("Could not move", self.log)

    def test_the_move_runs_once_a_second_start_finds_nothing(self):
        self.legacy.write_text("Owner rules\n", encoding="utf-8")
        self.assertTrue(self.run_quiet(server.migrate_agents_file))
        self.assertFalse(self.run_quiet(server.migrate_agents_file))

    def test_no_starter_is_written_while_an_old_file_waits_in_config(self):
        self.legacy.write_text("Owner rules\n", encoding="utf-8")
        self.assertFalse(self.run_quiet(server.write_starter_agents_file))
        self.assertFalse(self.agents.exists())

    def test_the_starter_goes_to_the_app_folder_not_config(self):
        self.assertTrue(self.run_quiet(server.write_starter_agents_file))
        self.assertTrue(self.agents.is_file())
        self.assertFalse(os.path.lexists(self.legacy))

    # The path Codex loads and the editor uses

    def test_codex_loads_the_file_through_a_link_in_its_home(self):
        self.agents.write_text("Owner rules\n", encoding="utf-8")
        self.assertTrue(self.run_quiet(server.link_agents_file))
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), str(self.agents))
        self.assertEqual(self.link.read_text(encoding="utf-8"), "Owner rules\n")

    def test_the_link_is_made_even_before_the_file_exists(self):
        self.assertTrue(self.run_quiet(server.link_agents_file))
        self.agents.write_text("Written later\n", encoding="utf-8")
        self.assertEqual(self.link.read_text(encoding="utf-8"), "Written later\n")

    def test_a_correct_link_is_left_as_it_is(self):
        self.run_quiet(server.link_agents_file)
        self.assertFalse(self.run_quiet(server.link_agents_file))

    def test_a_link_to_somewhere_else_is_repointed(self):
        self.link.symlink_to(self.root / "old-place.md")
        self.assertTrue(self.run_quiet(server.link_agents_file))
        self.assertEqual(os.readlink(self.link), str(self.agents))

    def test_a_regular_file_in_the_codex_home_is_never_replaced(self):
        self.link.write_text("Not ours\n", encoding="utf-8")
        self.assertFalse(self.run_quiet(server.link_agents_file))
        self.assertEqual(self.link.read_text(encoding="utf-8"), "Not ours\n")
        self.assertIn("left alone", self.log)

    def test_the_editor_reads_and_writes_the_app_folder(self):
        server.write_agents_file("Edited in the editor")
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "Edited in the editor\n")
        self.assertEqual(server.read_agents_file(), "Edited in the editor\n")
        self.assertFalse(os.path.lexists(self.legacy))
        self.assertEqual(self.link.read_text(encoding="utf-8") if self.link.exists() else "Edited in the editor\n",
                         "Edited in the editor\n")


class AgentsHomeDefaults(unittest.TestCase):
    """The real constants and the app's mounts, so the paths do not collide."""

    def test_the_paths_are_the_app_folder_and_codex_home(self):
        self.assertEqual(server.AGENTS_PATH, Path("/addon_config/AGENTS.md"))
        self.assertEqual(server.LEGACY_AGENTS_PATH, Path("/homeassistant/AGENTS.md"))
        self.assertEqual(server.CODEX_AGENTS_LINK, server.CODEX_HOME / "AGENTS.md")

    def test_config_yaml_maps_addon_config_apart_from_the_home_assistant_config(self):
        mounts = {m["type"]: m for m in yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["map"]}
        self.assertEqual(mounts["addon_config"]["path"], "/addon_config")
        self.assertFalse(mounts["addon_config"]["read_only"])
        self.assertEqual(mounts["homeassistant_config"]["path"], "/homeassistant")

    def test_main_moves_then_offers_the_starter_then_links_after_the_codex_config(self):
        calls = []

        class StopHere(Exception):
            """Ends main() once the order is known."""

        def record(name, stop=False):
            def call(*_args, **_kwargs):
                calls.append(name)
                if stop:
                    raise StopHere
            return call

        # The steps this test does not replace (the chat move, the old sign-in images) run in a temporary copy
        # of /data, /homeassistant and /addon_config.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with (
            isolated_app_folders(server, Path(folder.name)),
            patch.object(server, "read_options", return_value={}),
            patch.object(server.protected_policy, "policy_problem", return_value=""),
            patch.object(server, "make_worker_undumpable", return_value=True),
            patch.object(server, "migrate_agents_file", side_effect=record("migrate")),
            patch.object(server, "write_starter_agents_file", side_effect=record("starter")),
            patch.object(server, "ensure_runtime_files", side_effect=record("ensure")),
            patch.object(server, "link_agents_file", side_effect=record("link")),
            patch.object(server, "write_full_access_requirements_at_start", side_effect=record("done", stop=True)),
            self.assertRaises(StopHere),
        ):
            server.main()
        self.assertEqual(calls, ["migrate", "starter", "ensure", "link", "done"])


if __name__ == "__main__":
    unittest.main()
