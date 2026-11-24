"""The machine name is saved from the Remote connection window (0.5) and a first message gets a plain reply (0.3.2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from chat_scripts import chat_source
from test_server import server
from remote_unpaused import setUpModule, tearDownModule  # noqa: F401  (Remote Control is paused)

WEB = Path(__file__).resolve().parents[1] / "web"


def supervisor_reply(payload=None, error=None):
    """A fake requests response: raise_for_status raises error, json() returns payload."""
    response = MagicMock()
    response.raise_for_status.side_effect = error
    response.json.return_value = payload
    return response


class RenameRoute(unittest.TestCase):
    """POST /remote-control/name saves the option through Supervisor, from an administrator on the page only."""

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
        self.admin = self.stack.enter_context(patch.object(server, "ingress_admin",
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
        """The part of the remote-control state the page reads after a rename: the name from options.json."""
        name = server.read_options().get("remote_control_name")
        return {"configured_name": server.remote_control.machine_name(name)}

    def rename(self, body, **request):
        return self.client.post("/remote-control/name", json=body, **(request or self.ingress))

    def test_saves_only_the_name_and_keeps_secret_references(self):
        response = self.rename({"name": "  Lake house  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["remote_control"]["configured_name"], "Lake house")
        self.get.assert_called_once()
        self.assertEqual(self.get.call_args.args[0], "http://supervisor/addons/self/info")
        self.assertEqual(self.post.call_args.args[0], "http://supervisor/addons/self/options")
        self.assertEqual(self.post.call_args.kwargs["json"],
                         {"options": {**self.SAVED, "remote_control_name": "Lake house"}})
        local = json.loads(self.options.read_text())
        self.assertEqual(local, {"remote_control_name": "Lake house", "HA_TOKEN": "resolved-synthetic",
                                 "local_docs": True})
        self.assertEqual(self.options.stat().st_mode & 0o777, 0o600)

    def test_api_token_cannot_rename(self):
        response = self.rename({"name": "Other"}, headers={"Authorization": "Bearer tok"})
        self.assertEqual(response.status_code, 403)
        self.post.assert_not_called()

    def test_signed_in_person_who_is_not_an_administrator_cannot_rename(self):
        self.admin.return_value = (None, "Sign in as an administrator.")
        response = self.rename({"name": "Other"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Sign in as an administrator.")
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_refused_while_remote_control_is_on(self):
        self.enabled.return_value = True
        response = self.rename({"name": "Other"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Turn remote control off", response.get_json()["error"])
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_refuses_a_value_that_is_not_a_name(self):
        for body in ({"name": 7}, {"name": "x" * 64}, {"name": "ok", "extra": 1}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.rename(body).status_code, 400)
        self.post.assert_not_called()

    def test_one_limit_and_an_error_that_names_it(self):
        refused = self.rename({"name": "x" * 64}).get_json()["error"]
        self.assertEqual(refused, "Enter a name of up to 63 characters.")
        self.assertEqual(self.rename({"name": "  " + "x" * 63 + "  "}).status_code, 200)
        self.assertEqual(self.rename({"name": "Lake/house"}).status_code, 200)

    def test_local_write_failure_after_supervisor_saved_says_the_name_is_saved(self):
        self.options.write_text("not json")
        response = self.rename({"name": "Lake house"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["notice"], "The name is saved. It shows here after the app restarts.")
        self.post.assert_called_once()
        with patch.object(server, "write_local_option", side_effect=OSError("read-only")):
            self.assertIn("notice", self.rename({"name": "Other"}).get_json())

    def test_supervisor_refusal_leaves_options_unchanged(self):
        before = self.options.read_text()
        self.post.return_value = supervisor_reply(error=requests.HTTPError("400"))
        response = self.rename({"name": "Other"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("did not save", response.get_json()["error"])
        self.assertNotIn("synthetic", response.get_json()["error"])
        self.assertEqual(self.options.read_text(), before)


class FirstReplyPrompt(unittest.TestCase):
    """The page's rules ask for a reply to the person and follow AGENTS.md only when it exists."""

    def test_page_rules_follow_agents_file_only_when_it_exists(self):
        # The rules are read once for each thread, so they name the file with a condition, not a fact.
        text = server.PAGE_INSTRUCTIONS
        self.assertIn("Owner workspace instructions: if /addon_config/AGENTS.md exists, follow it.", text)
        self.assertNotIn("Follow /addon_config/AGENTS.md.", text)
        self.assertNotIn("check for the file yourself", text)

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
                self.assertIn(f"Owner workspace instructions: {agents}. ",
                              server.remote_control_instructions({}))


class PageControls(unittest.TestCase):
    """The page carries the name field, the Back control and the one-time AGENTS.md hint."""

    def test_name_field_posts_to_the_rename_action_and_has_a_back_control(self):
        html = (WEB / "index.html").read_text()
        chat = chat_source()
        studio = (WEB / "assets" / "studio.js").read_text()
        self.assertIn('id="remote-control-name-form"', html)
        self.assertIn('id="remote-control-name" maxlength="63"', html)
        self.assertIn('api("remote-control/name", { name: input.value })', chat)
        self.assertIn("Turn remote control off to change the name.", chat)
        self.assertIn('id="connection-back"', html)
        self.assertIn('$("connection-back").onclick', studio)

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
