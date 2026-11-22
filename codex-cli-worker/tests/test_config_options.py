"""The add-on Configuration page lists only options that a feature of this build reads and a test covers."""
import re
import unittest
from pathlib import Path

import yaml

WORKER = Path(__file__).resolve().parents[1]

# option -> (feature that reads it, source file that reads it, test file that proves it)
OPTIONS = {
    "model_choice": ("default model drop-down", "server.py", "test_model_list.py"),
    "codex_model": ("default model for a chat", "server.py", "test_server.py"),
    "model_pin": ("the one model a chat may run on", "server.py", "test_sign_in_page.py"),
    "model_reasoning_effort": ("default reasoning effort", "server.py", "test_conversations.py"),
    "reasoning_summary": ("reasoning headlines in the activity list", "server.py", "test_activity.py"),
    "codex_sandbox": ("sandbox mode for a run", "server.py", "test_approvals.py"),
    "notify_service": ("questions and failures sent to a notify service", "server.py", "test_choices.py"),
    "git_author_name": ("commit author and committer", "server.py", "test_git_author.py"),
    "git_author_email": ("commit author and committer", "server.py", "test_git_author.py"),
    "github_client_id": ("GitHub sign-in", "server.py", "test_github_routes.py"),
    "task_timeout_seconds": ("run time limit for a task", "server.py", "test_server.py"),
    "task_stall_seconds": ("stop a task that goes silent", "server.py", "test_sign_in_watchdog.py"),
    "auto_save_lovelace": ("save dashboards after an edit", "verification.py", "test_verification.py"),
    "config_check": ("Home Assistant configuration check after a task", "server.py", "test_config_check.py"),
    "full_snapshot": ("archive of the config folder before a message", "server.py", "test_backups.py"),
    "backup_retention_days": ("how long backups are kept", "server.py", "test_backups.py"),
    "chat_retention_days": ("delete chats after days without a message", "retention.py", "test_retention.py"),
    "browser_verification": ("built-in browser for dashboard checks", "verification.py", "test_verification.py"),
    "browser_memory_limit_mib": ("memory budget of the built-in browser", "verification.py", "test_verification.py"),
    "local_docs": ("local copy of the Home Assistant documentation", "server.py", "test_local_docs.py"),
    "warm_start": ("keep Codex running between messages", "server.py", "test_live_exchange.py"),
    "ha_url": ("address for Codex's own Home Assistant API calls", "server.py", "test_ha_url_review.py"),
    "HA_TOKEN": ("token for Codex's own Home Assistant API calls", "server.py", "test_ha_data.py"),
    "remote_control_name": ("machine name in the ChatGPT app", "server.py", "test_remote_name_and_first_reply.py"),
    "interactions_to_keep": ("records kept for Download diagnostics", "server.py", "test_interaction_diagnostics_fix.py"),
    "install_integration": ("install the integration on each start", "integration_sync.py", "test_integration_sync.py"),
    "home_assistant_mcp": ("tools of the bundled Home Assistant MCP server", "hass_mcp.py", "test_ha_mcp_option.py"),
}


def read_form(key: str, text: str) -> re.Pattern:
    """The option is read, as .get("key") or ["key"], or through a name set to "key", not only listed in a table of defaults."""
    names = re.findall(r"""^(\w+)\s*=\s*["']%s["']\s*$""" % re.escape(key), text, re.M)
    keys = "|".join([r"""["']%s["']""" % re.escape(key)] + [r"\b%s\b" % re.escape(n) for n in names])
    return re.compile(r"""(\.get\(\s*|\[\s*)(%s)""" % keys)


MAX_DESCRIPTION_WORDS = 25
CODE_WORDS = ("HA_TOKEN", "HA_URL", "_seconds", "_mib", "_days")


class ConfigurationOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load((WORKER / "config.yaml").read_text(encoding="utf-8"))
        cls.translations = yaml.safe_load((WORKER / "translations" / "en.yaml").read_text(encoding="utf-8"))["configuration"]

    def test_every_option_has_a_feature_and_a_test_and_no_option_is_missing(self):
        self.assertEqual(set(self.config["schema"]), set(OPTIONS), "add or remove the row in OPTIONS with the option")

    def test_defaults_and_schema_name_the_same_options(self):
        extra = set(self.config["options"]) - set(self.config["schema"])
        self.assertFalse(extra, f"default for an option the schema does not list: {sorted(extra)}")

    def test_each_option_is_read_by_its_feature(self):
        for key, (_feature, source, _test) in OPTIONS.items():
            with self.subTest(option=key):
                text = (WORKER / source).read_text(encoding="utf-8")
                self.assertRegex(text, read_form(key, text), f"{source} never reads {key} with .get or [ ]")

    def test_each_option_is_covered_by_a_test(self):
        for key, (_feature, _source, test) in OPTIONS.items():
            with self.subTest(option=key):
                text = (WORKER / "tests" / test).read_text(encoding="utf-8")
                self.assertRegex(text, r"\b%s\b" % re.escape(key), f"{test} never names {key}")

    def test_each_option_has_a_plain_label_and_description(self):
        self.assertEqual(set(self.translations), set(self.config["schema"]))
        for key, entry in self.translations.items():
            with self.subTest(option=key):
                self.assertTrue(entry.get("name", "").strip())
                self.assertTrue(entry.get("description", "").strip())

    def test_labels_are_plain_and_descriptions_stay_short(self):
        for key, entry in self.translations.items():
            with self.subTest(option=key):
                name = entry["name"]
                for word in CODE_WORDS:
                    self.assertNotIn(word, name, f"the name of {key} uses the code word {word}")
                words = len(entry["description"].split())
                self.assertLessEqual(words, MAX_DESCRIPTION_WORDS, f"the description of {key} has {words} words")

    def test_no_option_belongs_to_a_hidden_feature(self):
        names = " ".join(self.config["schema"]).lower()
        for word in ("schedul", "lead", "pause", "full_access"):
            self.assertNotIn(word, names)


if __name__ == "__main__":
    unittest.main()
