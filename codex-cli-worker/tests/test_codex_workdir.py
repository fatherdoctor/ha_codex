"""Codex's working folder, /media/.agents/codex: created at start, used by every Codex process, never a way around a mask."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_server import server

SHIPPED_WORKDIR = server.CODEX_WORKDIR  # read before any test patches it


class CodexWorkdirTests(unittest.TestCase):
    """Where Codex starts, and what that changes for the checks that read the paths it names."""

    def setUp(self) -> None:
        # resolve(): the working folder check refuses a link anywhere in the path, the temp folder's included.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.config = self.root / "config"
        self.config.mkdir()
        self.media = self.root / "media"
        self.media.mkdir()
        self.workdir = self.media / ".agents" / "codex"
        self.enterContext(patch.object(server, "CONFIG_ROOT", self.config))
        self.enterContext(patch.object(server, "MEDIA_ROOT", self.media))
        self.enterContext(patch.object(server, "CODEX_WORKDIR", self.workdir))

    def test_first_start_creates_the_folder(self) -> None:
        """A first install has no /media/.agents/codex; the start makes it, with its parent, and logs nothing."""
        with patch("builtins.print") as printed:
            server.ensure_codex_workdir()
        printed.assert_not_called()
        self.assertTrue(self.workdir.is_dir())
        self.assertEqual(server.codex_workdir(), str(self.workdir))

    def test_a_later_start_leaves_the_owners_files_alone(self) -> None:
        """An existing folder keeps every file in it: the start never moves, empties or rewrites it."""
        self.workdir.mkdir(parents=True)
        note = self.workdir / "AGENTS.md"
        note.write_text("Owner notes\n", encoding="utf-8")
        server.ensure_codex_workdir()
        self.assertEqual(note.read_text(encoding="utf-8"), "Owner notes\n")
        self.assertEqual(sorted(p.name for p in self.workdir.iterdir()), ["AGENTS.md"])

    def test_a_folder_that_cannot_be_made_falls_back_to_config(self) -> None:
        """When /media/.agents/codex cannot be made, the start goes on and Codex works from /config as before."""
        (self.media / ".agents").write_text("not a folder", encoding="utf-8")
        with patch("builtins.print") as printed:
            server.ensure_codex_workdir()
        self.assertIn("Cannot create", printed.call_args.args[0])
        self.assertEqual(server.codex_workdir(), str(self.config))

    def test_a_link_is_not_followed(self) -> None:
        """A link at /media/.agents/codex could point Codex anywhere; it is not used as the working folder."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.workdir.parent.mkdir(parents=True)
        self.workdir.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(server.codex_workdir(), str(self.config))

    def test_a_link_higher_in_the_path_is_not_followed(self) -> None:
        """A link at /media/.agents is refused too, and the start creates nothing through it."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.media / ".agents").symlink_to(elsewhere, target_is_directory=True)
        with patch("builtins.print") as printed:
            server.ensure_codex_workdir()
        self.assertEqual(list(elsewhere.iterdir()), [], "nothing is made through the link")
        self.assertEqual(server.codex_workdir(), str(self.config))
        printed.assert_called_once()
        self.assertIn("has a link in its path", printed.call_args.args[0])

    def test_without_media_the_app_works_from_config_and_logs_one_line(self) -> None:
        """Without the media folder the start makes nothing (a folder in the container would be lost at update)."""
        self.media.rmdir()
        with patch("builtins.print") as printed:
            server.ensure_codex_workdir()
        self.assertFalse(self.media.exists())
        self.assertEqual(server.codex_workdir(), str(self.config))
        printed.assert_called_once()

    def test_the_folder_is_inside_a_writable_root(self) -> None:
        """Writing in the working folder needs no approval: it is under one of the task's writable roots."""
        self.assertEqual(SHIPPED_WORKDIR, Path("/media/.agents/codex"))
        self.assertTrue(any(SHIPPED_WORKDIR.is_relative_to(root) for root in server.TASK_WRITABLE_ROOTS))

    def test_a_write_anywhere_in_media_needs_no_approval(self) -> None:
        """The owner's ruling of 2026-11-22: all of /media is writable, as it is for the Claude add-on."""
        for path in ("/media/.agents/claude/notes.md", "/media/recordings/clip.mp4", "/media/x"):
            with self.subTest(path=path):
                self.assertTrue(any(Path(path).is_relative_to(root) for root in server.TASK_WRITABLE_ROOTS))
        self.assertFalse(any(Path("/data/github/token").is_relative_to(root) for root in server.TASK_WRITABLE_ROOTS))

    def test_the_page_rules_name_the_folder_codex_starts_in(self) -> None:
        """The rules say where Codex works and that /config is Home Assistant's configuration.

        They name the folder itself, not the fallback, so the rules text stays the same from turn to turn.
        """
        rules = server.build_page_instructions()
        self.assertIn(f"Working folder: {self.workdir} ", rules)
        self.assertIn("Home Assistant configuration: /config", rules)
        self.assertNotIn("@@WORKDIR@@", rules)

    def test_a_secret_named_relative_to_the_working_folder_is_still_a_secret(self) -> None:
        """A diff of secrets.yaml is blanked whether Codex names it from /config or from its working folder."""
        self.workdir.mkdir(parents=True)
        config = self.config.as_posix()
        for path in (f"{config}/secrets.yaml", "secrets.yaml", "../../../config/secrets.yaml",
                     "../codex/../../../config/.storage/auth"):
            with self.subTest(path=path):
                self.assertTrue(server.secret_config_path(path))
        for path in (f"{self.workdir}/notes.md", "notes.md", f"{config}/automations.yaml"):
            with self.subTest(path=path):
                self.assertFalse(server.secret_config_path(path))


if __name__ == "__main__":
    unittest.main()
