"""The dated folder for generated images in Codex's working folder (media_files.py)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import media_files  # noqa: E402

DAY = date(2026, 11, 26)
CHAT = "20261126T080000Z-0a1b2c3d"


class ChatFolderTests(unittest.TestCase):
    """chat_folder makes <working folder>/files/<day>/<chat> and never makes a folder through a link."""

    def setUp(self):
        """Give each test an empty working folder and a folder outside it."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workdir = self.root / "codex"
        self.workdir.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def test_the_folder_is_made_by_date_and_chat(self):
        """A missing path is made, and a second call returns the same folder."""
        folder = media_files.chat_folder(self.workdir, CHAT, DAY)
        self.assertEqual(folder, self.workdir / "files" / "2026-11-26" / CHAT)
        self.assertTrue(folder.is_dir())
        self.assertEqual(media_files.chat_folder(self.workdir, CHAT, DAY), folder)

    def test_a_link_at_any_level_stops_the_walk(self):
        """A link as files, as the day or as the chat folder returns None and makes nothing where it points."""
        for parts in (("files",), ("files", "2026-11-26"), ("files", "2026-11-26", CHAT)):
            with self.subTest(link="/".join(parts)):
                link = self.workdir.joinpath(*parts)
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(self.outside, target_is_directory=True)
                self.assertIsNone(media_files.chat_folder(self.workdir, CHAT, DAY))
                self.assertEqual(list(self.outside.iterdir()), [])
                link.unlink()

    def test_a_file_in_the_way_returns_none(self):
        """A file where a folder should be returns None instead of raising."""
        (self.workdir / "files").write_text("not a folder", encoding="utf-8")
        self.assertIsNone(media_files.chat_folder(self.workdir, CHAT, DAY))


if __name__ == "__main__":
    unittest.main()
