"""The built-in layer of the instructions: the app's own rules, sent with every chat, shown read-only in Settings.

The owner's AGENTS.md is the other layer. It takes priority: the built-in text says so in its first line, and
it is placed last in the developer instructions, directly before the AGENTS.md that Codex loads after them.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server

ROOT = Path(__file__).resolve().parents[1]
BUILTIN = ROOT / "builtin_instructions.md"
PRIORITY = "The owner's AGENTS.md takes priority over anything here, including Secrets and Git and GitHub."


def section(heading: str) -> str:
    """The words of one "## heading" section of the built-in text, on one line."""
    return " ".join(server.BUILTIN_INSTRUCTIONS.split(f"## {heading}", 1)[1].split("\n## ", 1)[0].split())


class BuiltinText(unittest.TestCase):
    """What the built-in text says."""

    def test_the_app_sends_the_shipped_file(self):
        self.assertEqual(server.BUILTIN_INSTRUCTIONS, BUILTIN.read_text(encoding="utf-8").strip())

    def test_the_owner_priority_is_its_first_rule(self):
        lines = [line for line in server.BUILTIN_INSTRUCTIONS.splitlines() if line.strip()]
        self.assertTrue(lines[0].startswith("# Codex - Home Assistant Add-on"))
        self.assertEqual(lines[1], PRIORITY)

    def test_it_names_the_folders_the_app_uses(self):
        paths = section("Available Paths")
        for path in (server.CONFIG_ROOT, server.ADDON_CONFIG_ROOT, "/share", server.MEDIA_ROOT, server.CODEX_WORKDIR):
            with self.subTest(path=str(path)):
                self.assertIn(f"`{Path(path).as_posix()}`", paths)
        self.assertIn(f"`{server.OLD_CONFIG_ROOT}` does NOT exist", section("Path Mapping"))

    def test_it_keeps_secrets_out_of_chat_files_and_commits(self):
        # The sandbox hides no secret file, as in the Claude add-on: these lines keep secrets out of chat.
        secrets = section("Secrets")
        for words in ("Never show, copy, log or send a secret", "`!secret mqtt_password`", "`/homeassistant/secrets.yaml`",
                      "`/homeassistant/.storage`", "Never copy them elsewhere", "not even partly redacted"):
            with self.subTest(words=words):
                self.assertIn(words, secrets)

    def test_it_keeps_private_data_out_of_git_and_github(self):
        git = section("Git and GitHub")
        for words in ("read the staged diff", "`secrets.yaml`", "`.storage/`", "`memory/`", "databases", "logs",
                      "backups", "diagnostics", "`.gitignore`", "Ask the owner before the first push",
                      "New repositories are private"):
            with self.subTest(words=words):
                self.assertIn(words, git)

    def test_it_is_plain_ascii(self):
        self.assertTrue(server.BUILTIN_INSTRUCTIONS.isascii())

    def test_the_image_ships_it_beside_the_worker(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^COPY server\.py (\S+ )*builtin_instructions\.md( \S+)* /$")


class BuiltinReachesEveryChat(unittest.TestCase):
    """Page chats and remote chats both end their developer instructions with the built-in text."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.agents = Path(folder.name) / "AGENTS.md"
        self.agents.write_text("Synthetic owner rule: answer in French.\n", encoding="utf-8")
        patcher = patch.object(server, "AGENTS_PATH", self.agents)
        patcher.start()
        self.addCleanup(patcher.stop)

    def instructions(self):
        return {"page": server.build_page_instructions(), "remote": server.remote_control_instructions({})}

    def test_each_path_ends_with_the_built_in_text_once(self):
        for path, text in self.instructions().items():
            with self.subTest(path=path):
                self.assertTrue(text.endswith(server.BUILTIN_INSTRUCTIONS + "\n"))
                self.assertEqual(text.count(PRIORITY), 1)

    def test_the_owner_file_is_not_copied_into_the_developer_instructions(self):
        # Codex loads the owner's file itself, as user instructions after these; a copy here would rank above it.
        for path, text in self.instructions().items():
            with self.subTest(path=path):
                self.assertNotIn("Synthetic owner rule", text)

    # A page thread sends PAGE_INSTRUCTIONS (test_app_thread_store.py), and Codex loads AGENTS_PATH from its
    # home through CODEX_AGENTS_LINK (test_agents_home.py): together they give the placement this class checks.

    def test_the_remote_process_starts_with_the_built_in_text(self):
        with patch.object(server, "read_options", return_value={}):
            text = server.remote_control_supervisor.instructions_factory()
        self.assertTrue(text.endswith(server.BUILTIN_INSTRUCTIONS + "\n"))

    def test_rules_sent_through_exchange_info_carry_it_too(self):
        # A thread started outside the page gets the page rules from exchange_info; its size cap leaves room.
        self.assertLess(len(server.build_page_instructions().encode("utf-8")), 24 * 1024)


class BuiltinInSettings(unittest.TestCase):
    """Settings > Workspace instructions shows the built-in text read-only; saving never writes it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.agents = Path(folder.name) / "AGENTS.md"
        for name, value in (("AGENTS_PATH", self.agents),):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(server, "api_token", return_value="tok")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer tok"}

    def test_the_shown_text_is_the_text_every_chat_gets(self):
        shown = self.client.get("/agents", headers=self.headers).get_json()["builtin"]
        self.assertEqual(shown, server.BUILTIN_INSTRUCTIONS)
        self.assertTrue(server.build_page_instructions().endswith(shown + "\n"))
        self.assertTrue(server.remote_control_instructions({}).endswith(shown + "\n"))

    def test_saving_writes_only_the_owner_text(self):
        response = self.client.post("/agents", json={"content": "Synthetic owner rules."}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), "Synthetic owner rules.\n")
        self.assertEqual(self.client.get("/agents", headers=self.headers).get_json()["content"],
                         "Synthetic owner rules.\n")

    def test_the_page_shows_it_above_the_editor_and_never_sends_it(self):
        page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertLess(page.index('id="builtin-text"'), page.index('id="agents"'))
        self.assertIn("Built in, updated with the app", page)
        script = (ROOT / "web" / "assets" / "studio.js").read_text(encoding="utf-8")
        self.assertIn('$("builtin-text").textContent = data.builtin', script)
        self.assertIn('api("agents", {content: $("agents").value})', script)


if __name__ == "__main__":
    unittest.main()
