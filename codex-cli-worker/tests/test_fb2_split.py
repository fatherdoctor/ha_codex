"""Settings and the Configuration tab (0.3.5): the link to the tab, and what left the page for it."""
from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from test_server import server

WEB = Path(__file__).resolve().parents[1] / "web"


def supervisor_info(slug=None, error=None):
    """A fake reply to GET /addons/self/info with the given slug, or one that raises error."""
    response = MagicMock()
    response.raise_for_status.side_effect = error
    response.json.return_value = {"data": {"slug": slug}}
    return response


class AddonConfigurationLink(unittest.TestCase):
    """GET /addon-configuration gives the path of this app's Configuration tab, built from Supervisor's slug."""

    def setUp(self):
        patcher = patch.object(server, "_addon_slug", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("api_token", "tok"), ("ha_token", "synthetic-supervisor")):
            patcher = patch.object(server, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = server.app.test_client()

    def link(self):
        response = self.client.get("/addon-configuration", headers={"Authorization": "Bearer tok"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["url"]

    def test_the_link_uses_the_slug_with_its_repository_prefix(self):
        with patch.object(server.requests, "get", return_value=supervisor_info("a1b2c3d4_codex_cli_worker")) as get:
            self.assertEqual(self.link(), "/config/app/a1b2c3d4_codex_cli_worker/config")
            self.assertEqual(get.call_args.args[0], "http://supervisor/addons/self/info")
            # A known slug is kept, so a second Settings visit does not ask Supervisor again.
            self.assertEqual(self.link(), "/config/app/a1b2c3d4_codex_cli_worker/config")
            get.assert_called_once()

    def test_no_link_when_supervisor_cannot_say_and_it_asks_again_later(self):
        with patch.object(server.requests, "get", side_effect=requests.ConnectionError("synthetic")):
            self.assertIsNone(self.link())
        with patch.object(server.requests, "get", return_value=supervisor_info(error=requests.HTTPError("403"))):
            self.assertIsNone(self.link())
        with patch.object(server.requests, "get", return_value=supervisor_info("local_codex_cli_worker")):
            self.assertEqual(self.link(), "/config/app/local_codex_cli_worker/config")

    def test_a_slug_that_is_not_a_slug_never_reaches_the_link(self):
        for slug in ("../../profile", "x/config?y", "Codex", "", None, 7, "a" * 65):
            with self.subTest(slug=slug), patch.object(server.requests, "get", return_value=supervisor_info(slug)):
                self.assertIsNone(self.link())

    def test_the_route_needs_the_worker_token(self):
        self.assertEqual(self.client.get("/addon-configuration").status_code, 401)


class ConfigurationOption(unittest.TestCase):
    """The interactions_to_keep option: optional, 10 to 100, applied when the worker starts."""

    def test_schema_has_the_option_without_a_default(self):
        config = (Path(server.__file__).with_name("config.yaml")).read_text()
        self.assertIn("  interactions_to_keep: int(10,100)?\n", config)
        options = config.split("\noptions:\n", 1)[1].split("\nschema:\n", 1)[0]
        # No default, so an install that saved a number on the page before 0.3.5 keeps it.
        self.assertNotIn("interactions_to_keep", options)
        translations = (Path(server.__file__).parent / "translations" / "en.yaml").read_text()
        self.assertIn("  interactions_to_keep:\n    name: Interactions to keep\n", translations)

    def test_main_applies_the_option_before_any_work_starts(self):
        source = inspect.getsource(server.main)
        self.assertLess(source.index("apply_interaction_limit(read_options())"), source.index("load_task_index()"))


class SettingsPage(unittest.TestCase):
    """What Settings holds after the split (design section 1, all eight answers yes)."""

    def setUp(self):
        self.html = (WEB / "index.html").read_text()
        self.chat = (WEB / "assets" / "chat.js").read_text()
        self.studio = (WEB / "assets" / "studio.js").read_text()

    def test_moved_and_empty_parts_are_gone(self):
        # Advanced came back in 0.3.6 to hold Browser permissions (SettingsAccordion below).
        for gone in ('id="appearance-section"', 'id="diagnostic-retention"',
                     'id="save-diagnostic-retention"'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.html)
        self.assertNotIn('api("diagnostics/settings", {', self.chat)

    def test_workspace_rules_open_from_workspace_instructions(self):
        section = self.html.split('<section id="workspace-section">', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="workspace-open"', section)
        self.assertNotIn('id="agents"', section)
        # The editor lives in its own window and the Settings section stays in Settings.
        editor = self.html.split('<div id="workspace-content">', 1)[1].split("</div>", 1)[0]
        self.assertIn('id="agents"', editor)
        self.assertIn('id="save-agents"', editor)
        self.assertNotIn('append($("workspace-section"))', self.studio)

    def test_one_configuration_row_at_the_foot_opens_the_tab_in_home_assistant(self):
        self.assertEqual(self.html.count('id="addon-configuration"'), 1)
        row = self.html.split('id="addon-configuration"', 1)[1].split("</a>", 1)[0]
        self.assertIn('target="_top"', row)
        # No href until Supervisor names the slug: a guessed path is wrong on a normal install (review F2).
        self.assertNotIn("href=", row)
        self.assertIn('aria-label="Add-on configuration"', row)
        settings = self.html.split('<dialog id="settings">', 1)[1].split("</dialog>", 1)[0]
        self.assertGreater(settings.index('id="addon-configuration"'), settings.index('id="workspace-section"'))
        self.assertIn('data.url.startsWith("/config/app/")', self.chat)
        # Home Assistant 2026.5, this app's minimum, has no /hassio/ panel: a link there opens a missing page.
        self.assertNotIn("/hassio/addon/", self.chat)
        self.assertNotIn('"/hassio/addon/', inspect.getsource(server.addon_configuration))


class SettingsAccordion(unittest.TestCase):
    """One top-level Settings section open at a time, and no Browser permissions section since 0.4."""

    def setUp(self):
        self.html = (WEB / "index.html").read_text()
        self.chat = (WEB / "assets" / "chat.js").read_text()
        self.settings = self.html.split('<dialog id="settings">', 1)[1].split("</dialog>", 1)[0]

    def test_no_browser_permissions_and_no_advanced_section(self):
        """0.4 removed the dashboard browser, so Settings has no Browser permissions and no Advanced section."""
        self.assertNotIn("browser-permissions", self.settings)
        self.assertNotIn('id="advanced-section"', self.settings)
        self.assertNotIn("advanced-section", self.chat)
        self.assertNotIn("browser-permissions", self.chat)
        # Only the Add-on configuration row and the result line follow the last section.
        after = self.settings.split('id="addon-configuration"', 1)[1]
        self.assertNotIn("<section", after)

    def test_opening_a_top_level_section_closes_the_others(self):
        opener = self.chat.split("function openSettingsSection(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if (open && !parent) closeOtherSettingsSections(section);", opener)
        closer = self.chat.split("function closeOtherSettingsSections(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('document.querySelectorAll("#settings > section")', closer)
        self.assertIn("openSettingsSection(other, false)", closer)


if __name__ == "__main__":
    unittest.main()
