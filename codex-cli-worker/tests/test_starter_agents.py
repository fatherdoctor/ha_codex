"""A new install gets starter workspace instructions once; an owner's instructions are never replaced."""
import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server

ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "starter_agents.md"


class StarterAgentsWrite(unittest.TestCase):
    """What write_starter_agents_file() does to /config/AGENTS.md, in a temporary folder."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.agents = self.root / "config" / "AGENTS.md"
        self.agents.parent.mkdir()
        self.codex_config = self.root / "codex-home" / "config.toml"
        self.codex_config.parent.mkdir()
        patches = patch.multiple(server, AGENTS_PATH=self.agents, CODEX_CONFIG_PATH=self.codex_config)
        patches.start()
        self.addCleanup(patches.stop)

    def write(self) -> bool:
        with contextlib.redirect_stdout(io.StringIO()) as self.log:
            return server.write_starter_agents_file()

    def test_first_start_writes_the_starter_text_unchanged(self):
        self.assertTrue(self.write())
        self.assertEqual(self.agents.read_bytes(), STARTER.read_bytes())
        self.assertIn(str(self.agents), self.log.getvalue())

    def test_the_editor_shows_the_starter_after_the_first_start(self):
        self.write()
        self.assertEqual(server.read_agents_file(), STARTER.read_text(encoding="utf-8"))

    def test_an_owner_file_is_never_replaced(self):
        self.agents.write_text("Owner rules, unchanged.\n", encoding="utf-8")
        self.assertFalse(self.write())
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "Owner rules, unchanged.\n")

    def test_an_empty_owner_file_is_never_replaced(self):
        self.agents.write_text("", encoding="utf-8")
        self.assertFalse(self.write())
        self.assertEqual(self.agents.read_bytes(), b"")

    def test_a_link_is_not_followed(self):
        target = self.root / "elsewhere.md"
        target.write_text("Owner text behind a link.\n", encoding="utf-8")
        self.agents.symlink_to(target)
        self.assertFalse(self.write())
        self.assertEqual(target.read_text(encoding="utf-8"), "Owner text behind a link.\n")

    def test_a_dangling_link_creates_no_file(self):
        target = self.root / "missing.md"
        self.agents.symlink_to(target)
        self.assertFalse(self.write())
        self.assertFalse(target.exists())

    def test_a_folder_with_that_name_is_left_alone(self):
        self.agents.mkdir()
        self.assertFalse(self.write())
        self.assertTrue(self.agents.is_dir())

    def test_a_failed_write_leaves_no_file_and_the_next_start_tries_again(self):
        real_fdopen = server.os.fdopen

        def full_disk(descriptor, mode):
            stream = real_fdopen(descriptor, mode)
            stream.write = lambda data: (_ for _ in ()).throw(OSError(28, "No space left on device"))
            return stream

        with patch.object(server.os, "fdopen", side_effect=full_disk):
            self.assertFalse(self.write())
        self.assertFalse(self.agents.exists())
        self.assertIn("No space left on device", self.log.getvalue())
        self.assertTrue(self.write())
        self.assertEqual(self.agents.read_bytes(), STARTER.read_bytes())

    def test_a_later_start_writes_nothing(self):
        self.codex_config.write_text("", encoding="utf-8")
        self.assertFalse(self.write())
        self.assertFalse(self.agents.exists())

    def test_a_deleted_starter_does_not_come_back(self):
        self.assertTrue(self.write())
        self.codex_config.write_text("", encoding="utf-8")  # What ensure_runtime_files() leaves after any start.
        self.agents.unlink()
        self.assertFalse(self.write())
        self.assertFalse(self.agents.exists())

    def test_a_config_folder_that_cannot_be_written_does_not_stop_the_start(self):
        with patch.object(server, "AGENTS_PATH", self.root / "no-such-folder" / "AGENTS.md"):
            self.assertFalse(self.write())
        self.assertIn("Could not write the starter", self.log.getvalue())

    def test_a_missing_starter_text_does_not_stop_the_start(self):
        with patch.object(server, "STARTER_AGENTS_PATH", self.root / "no-starter.md"):
            self.assertFalse(self.write())
        self.assertFalse(self.agents.exists())


class StarterAgentsAtStartup(unittest.TestCase):
    """Where main() calls the write, and what the image ships."""

    def test_main_offers_the_starter_before_it_writes_the_codex_config(self):
        calls = []

        class StopHere(Exception):
            """Ends main() once the order of the two steps is known."""

        def prepare():
            calls.append("ensure_runtime_files")
            raise StopHere

        with (
            patch.object(server, "read_options", return_value={}),
            patch.object(server.protected_policy, "policy_problem", return_value=""),
            patch.object(server, "make_worker_undumpable", return_value=True),
            patch.object(server, "write_starter_agents_file", side_effect=lambda: calls.append("starter")),
            patch.object(server, "ensure_runtime_files", side_effect=prepare),
            self.assertRaises(StopHere),
        ):
            server.main()
        self.assertEqual(calls, ["starter", "ensure_runtime_files"])

    def test_a_start_the_policy_refuses_writes_no_starter(self):
        with (
            patch.object(server, "read_options", return_value={}),
            patch.object(server.protected_policy, "policy_problem", return_value="synthetic problem"),
            patch.object(server, "write_starter_agents_file") as write,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            server.main()
        write.assert_not_called()

    def test_the_image_ships_the_starter_beside_the_worker(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        # The worker's first COPY line; other files may share it.
        self.assertRegex(dockerfile, r"(?m)^COPY server\.py account_access\.py (\S+ )*starter_agents\.md( \S+)* /$")
        self.assertEqual(server.STARTER_AGENTS_PATH, Path(server.__file__).resolve().parent / "starter_agents.md")


class StarterAgentsText(unittest.TestCase):
    """The starter is short, general, and holds the rules DOCS.md names."""

    def setUp(self):
        self.text = STARTER.read_text(encoding="utf-8")

    def test_it_is_small_enough_to_save_from_the_editor(self):
        self.assertLess(len(STARTER.read_bytes()), 4096)
        self.assertLess(len(STARTER.read_bytes()), server.AGENTS_MAX_BYTES)

    def test_it_says_the_file_is_the_owners_to_change_or_delete(self):
        self.assertIn("The file is yours: change it, add the rules of your home, or\ndelete it.", self.text)

    def test_it_holds_the_four_general_rules(self):
        for heading in ("Change the home only on the owner's word.", "Keep secrets secret.",
                        "Keep the old version.", "Check before a restart."):
            with self.subTest(rule=heading):
                self.assertIn(heading, self.text)

    def test_it_does_not_ask_for_backup_copies_inside_the_config_folder(self):
        # The worker gives each message its own backup folder and tells Codex to leave no other copies in /config.
        self.assertNotRegex(self.text, r"\.bak|\.orig|backup cop")

    def test_it_names_no_real_home(self):
        # No address, phone number, web address, mail address or entity id: only Home Assistant's own file names.
        for pattern in (r"\d{1,3}(\.\d{1,3}){3}", r"https?://", r"\S+@\S+\.\S+", r"\d{3}[-. ]\d{3}[-. ]\d{4}",
                        r"\b(light|switch|lock|cover|sensor|climate|alarm_control_panel)\.[a-z0-9_]+"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.text))

    def test_the_documents_say_where_the_starter_comes_from(self):
        for path in (ROOT.parent / "README.md", ROOT / "README.md"):
            with self.subTest(path=str(path.relative_to(ROOT.parent))):
                self.assertIn("gets a short starter file there once", " ".join(path.read_text(encoding="utf-8").split()))
        self.assertIn("**Starter instructions.**", (ROOT / "DOCS.md").read_text(encoding="utf-8"))
        self.assertIn("server.py: write_starter_agents_file", (ROOT.parent / "AUDITING.md").read_text(encoding="utf-8"))

    def test_it_is_plain_ascii_with_one_final_newline(self):
        self.assertTrue(self.text.isascii())
        self.assertTrue(self.text.endswith("\n") and not self.text.endswith("\n\n"))


if __name__ == "__main__":
    unittest.main()
