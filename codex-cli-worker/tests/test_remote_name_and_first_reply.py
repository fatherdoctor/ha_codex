"""The machine name is set on the Configuration tab only (0.3.5), and a first message gets a plain reply (0.3.2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch


from test_server import server

WEB = Path(__file__).resolve().parents[1] / "web"


def supervisor_reply(payload=None, error=None):
    """A fake requests response: raise_for_status raises error, json() returns payload."""
    response = MagicMock()
    response.raise_for_status.side_effect = error
    response.json.return_value = payload
    return response


class RenameRoute(unittest.TestCase):
    """The page no longer renames: POST /remote-control/name is refused and nothing reaches Supervisor (0.3.5)."""

    SAVED = {"remote_control_name": "Home Assistant", "HA_TOKEN": "!secret synthetic_token", "local_docs": True}

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.options = root / "options.json"
        self.options.write_text(json.dumps({"remote_control_name": "Home Assistant", "HA_TOKEN": "resolved-synthetic",
                                            "local_docs": True}))
        self.options.chmod(0o600)
        self.stack.enter_context(patch.object(server, "OPTIONS_PATH", self.options))
        self.stack.enter_context(patch.object(server, "api_token", return_value="tok"))
        self.stack.enter_context(patch.object(server, "ha_token", return_value="synthetic-supervisor"))
        self.stack.enter_context(patch.object(server, "ingress_admin",
                                              return_value=({"user_id": "fixture-admin"}, "")))
        self.enabled = self.stack.enter_context(
            patch.object(server.remote_control_supervisor, "enabled", return_value=False))
        self.stack.enter_context(patch.object(server.remote_control_supervisor, "snapshot",
                                              side_effect=self.snapshot))
        self.get = self.stack.enter_context(patch.object(
            server.requests, "get", return_value=supervisor_reply({"data": {"options": dict(self.SAVED)}})))
        self.post = self.stack.enter_context(patch.object(server.requests, "post", return_value=supervisor_reply()))
        self.client = server.app.test_client()
        self.ingress = {"headers": {"X-Ingress-Path": "/api/hassio_ingress/test"},
                        "environ_overrides": {"REMOTE_ADDR": server.INGRESS_PROXY_IP}}

    @staticmethod
    def snapshot():
        """The part of the remote-control state the page reads: the name from options.json."""
        name = server.read_options().get("remote_control_name")
        return {"configured_name": server.remote_control.machine_name(name)}

    def rename(self, body, **request):
        return self.client.post("/remote-control/name", json=body, **(request or self.ingress))

    def test_rename_action_is_gone_and_options_stay(self):
        before = self.options.read_text()
        response = self.rename({"name": "Lake house"})
        self.assertEqual(response.status_code, 404)
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.options.read_text(), before)

    def test_page_has_no_path_that_writes_add_on_options(self):
        # The one writer of /addons/self/options was the rename; the Configuration tab is now the only one.
        source = (Path(server.__file__)).read_text()
        self.assertNotIn("/options\"", source)
        self.assertFalse(hasattr(server, "save_remote_control_name"))
        self.assertFalse(hasattr(server, "write_local_option"))


class FirstReplyPrompt(unittest.TestCase):
    """The page's rules ask for a reply to the person and follow AGENTS.md only when it exists."""

    def test_page_rules_follow_agents_file_only_when_it_exists(self):
        # The rules are read once for each thread, so they name the file with a condition, not a fact.
        text = server.PAGE_INSTRUCTIONS
        self.assertIn("Owner workspace instructions: if /config/AGENTS.md exists, follow it.", text)
        self.assertNotIn("Follow /config/AGENTS.md.", text)

    def test_answer_is_a_reply_not_a_worker_report(self):
        line = next(line for line in server.PAGE_INSTRUCTIONS.splitlines()
                    if line.startswith("Answer in plain text"))
        self.assertIn("Answer a greeting, a test message or a question directly", line)
        self.assertIn("Do not describe this worker", line)
        self.assertIn("do not report that an optional instruction file is absent", line)
        self.assertNotIn("files that were not found", line)

    def test_remote_control_instructions_name_agents_file_only_when_it_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            agents = Path(folder) / "AGENTS.md"
            with patch.object(server, "AGENTS_PATH", agents):
                self.assertNotIn("AGENTS.md", server.remote_control_instructions({}))
                agents.write_text("Synthetic owner rules")
                self.assertIn("Owner workspace instructions: /config/AGENTS.md. ",
                              server.remote_control_instructions({}))


class PageControls(unittest.TestCase):
    """The page shows the machine name without a field, and carries the one-time AGENTS.md hint."""

    def test_name_is_shown_and_set_on_the_configuration_tab(self):
        html = (WEB / "index.html").read_text()
        chat = (WEB / "assets" / "chat.js").read_text()
        self.assertNotIn('id="remote-control-name-form"', html)
        self.assertIn('<strong id="remote-control-name"></strong>', html)
        self.assertIn("Change it with Remote control machine name in Add-on configuration.", html)
        self.assertNotIn("remote-control/name", chat)

    def test_agents_hint_is_dismissible_and_outside_the_chat(self):
        html = (WEB / "index.html").read_text()
        studio = (WEB / "assets" / "studio.js").read_text()
        hint = html.index('id="agents-hint"')
        # Below the message list and above the composer, so the integration notice stays next to the list.
        self.assertLess(html.index('id="messages"'), hint)
        self.assertLess(html.index('class="compose-area"'), hint)
        self.assertLess(hint, html.index('id="composer"'))
        self.assertIn('id="agents-hint-dismiss"', html)
        self.assertIn('(await api("agents")).exists !== false', studio)
        self.assertIn("localStorage.setItem(AGENTS_HINT_KEY", studio)


if __name__ == "__main__":
    unittest.main()
