"""The GitHub sign-in's page routes, its place in /status, the tool registration and the prompt sentence.

All values are invented; no request leaves the test.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server
from test_github_access import APP_TOKEN, REPO, FakeHttp, Response, app_answers

CONTRACT = {"state", "kind", "account", "client_id_source", "repositories", "expected_repositories", "missing",
            "extra", "permissions", "permission_problem", "expires_at", "connected_at", "last_check", "last_test",
            "revoke_url", "flow"}
WRITES = ("/github/connect", "/github/cancel", "/github/token", "/github/test", "/github/check", "/github/accept",
          "/github/disconnect", "/github/repos/clone", "/github/repos/link")


class GitHubRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = server.github_service
        self.http = FakeHttp(app_answers())
        self.patches = [
            patch.object(server, "DATA_ROOT", self.root),
            patch.object(server, "read_options", return_value={**server.DEFAULT_OPTIONS,
                                                               "github_client_id": "Iv23liExampleClient"}),
            patch.object(server, "api_token", return_value="synthetic-worker-credential"),
            patch.object(server, "lookup_ha_users", return_value=[
                {"id": "synthetic-admin", "is_active": True, "group_ids": ["system-admin"]},
                {"id": "synthetic-user", "is_active": True, "group_ids": ["system-users"]}]),
            patch.object(server, "fire_ha_event", return_value=(True, "ok")),
            patch.object(server, "call_ha_service", return_value=(True, "ok")),
            patch.object(self.service, "http", self.http),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        server.admin_cache.clear()
        self.addCleanup(self.service.disconnect)
        self.client = server.app.test_client()
        self.worker = {"Authorization": "Bearer synthetic-worker-credential"}
        self.env = {"REMOTE_ADDR": server.INGRESS_PROXY_IP}

    def ingress(self, method, path, user="synthetic-admin", body=None):
        headers = {"X-Ingress-Path": "/sample", "X-Remote-User-Id": user}
        return self.client.open(path, method=method, json=body if body is not None else {}, headers=headers,
                                environ_overrides=self.env)

    def test_reads_need_auth_and_hold_only_the_contract(self):
        self.assertEqual(self.client.get("/github").status_code, 401)
        response = self.client.get("/github", headers=self.worker)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json["github"]), CONTRACT)
        self.assertEqual(response.json["github"]["state"], "disconnected")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_every_change_needs_ingress_and_an_administrator(self):
        for path in WRITES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}, headers=self.worker).status_code, 403)
                self.assertEqual(self.ingress("POST", path, user="synthetic-user").status_code, 403)
                self.assertEqual(self.ingress("POST", path, user="").status_code, 403)

    def test_a_refused_change_says_what_the_owner_does_next(self):
        """Astro F3: the refusal names GitHub, not permission requests or browser permissions."""
        response = self.client.post("/github/connect", json={}, headers=self.worker)
        self.assertEqual(response.json["error"], "Open Codex through Home Assistant to make this change.")
        response = self.ingress("POST", "/github/connect", user="synthetic-user")
        self.assertEqual(response.json["error"], "Only a Home Assistant administrator can manage GitHub.")
        self.assertEqual(self.ingress("POST", "/github/connect", user="").json["error"], server.UNVERIFIED_ACCOUNT)
        with patch.object(server, "lookup_ha_users", side_effect=RuntimeError("synthetic outage")):
            server.admin_cache.clear()
            self.assertEqual(self.ingress("POST", "/github/connect").json["error"], server.UNVERIFIED_ACCOUNT)

    def test_a_permission_answer_keeps_its_own_refusal(self):
        """The approval route still names permission requests, and still refuses the same people."""
        path = "/tasks/synthetic-task/approvals/synthetic-approval"
        response = self.client.post(path, json={"decision": "allow"}, headers=self.worker)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"],
                         "Open Codex through Home Assistant and sign in as an administrator to answer permission requests.")
        response = self.ingress("POST", path, user="synthetic-user", body={"decision": "allow"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"], "Only a Home Assistant administrator can answer permission requests.")

    def test_connect_shows_the_code_and_never_the_device_code(self):
        self.http.posts = [Response(200, {"device_code": "synthetic-device-code", "user_code": "WXYZ-1234",
                                          "verification_uri": "https://github.com/login/device",
                                          "expires_in": 900, "interval": 5})]
        response = self.ingress("POST", "/github/connect")
        self.assertEqual(response.status_code, 200, response.json)
        github = response.json["github"]
        self.assertEqual(github["state"], "connecting")
        self.assertEqual(github["flow"]["status"], "waiting_for_user")
        self.assertEqual(github["flow"]["user_code"], "WXYZ-1234")
        self.assertNotIn("synthetic-device-code", json.dumps(response.json))
        self.assertEqual(self.ingress("POST", "/github/cancel").json["github"]["flow"]["status"], "canceled")

    def test_status_carries_github_without_the_token(self):
        self.service._complete_app(APP_TOKEN)
        quiet = {name: patch.object(server, name, return_value={}) for name in (
            "codex_login_status", "auth_status_payload", "model_pin", "usage_status_payload",
            "integration_status", "remote_control_summary", "refresh_usage_status_async")}
        quiet["account_access"] = patch.object(server.account_access, "snapshot", return_value={})
        quiet["tasks"] = patch.object(server, "tasks", {})
        for item in quiet.values():
            item.start()
            self.addCleanup(item.stop)
        response = self.client.get("/status", headers=self.worker)
        self.assertEqual(response.status_code, 200)
        github = response.json["github"]
        self.assertEqual(set(github), CONTRACT)
        self.assertEqual(github["state"], "ok")
        self.assertEqual(github["account"], {"login": "example-owner"})
        self.assertNotIn(APP_TOKEN, response.get_data(as_text=True))
        self.assertNotIn("token", {key for key in github if key != "kind"})

    def test_disconnect_waits_for_a_running_task(self):
        self.service._complete_app(APP_TOKEN)
        with patch.object(server, "active_task_id", return_value="task-running"):
            self.assertEqual(self.ingress("POST", "/github/disconnect").status_code, 409)
        self.assertIsNotNone(self.service.store.read_token())
        response = self.ingress("POST", "/github/disconnect")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["github"]["state"], "disconnected")
        self.assertIsNone(self.service.store.read_token())

    def test_a_refused_token_is_a_400_with_no_echo(self):
        pasted = "github_pat_" + "Q1w2E3r4T5" * 8
        response = self.ingress("POST", "/github/token", body={"token": pasted, "repository": REPO})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(pasted, response.get_data(as_text=True))

    def test_clone_and_link_refuse_with_a_sentence_and_the_status(self):
        self.service._complete_app(APP_TOKEN)
        for path, body in (("/github/repos/clone", {"repository": REPO, "folder": ".."}),
                           ("/github/repos/link", {"repository": REPO, "path": "/nowhere"}),
                           ("/github/repos/clone", {"repository": "other-owner/x", "folder": "x"})):
            with self.subTest(path=path, body=body):
                response = self.ingress("POST", path, body=body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])
                self.assertIn("github", response.json)
                self.assertNotIn(APP_TOKEN, response.get_data(as_text=True))

    def test_a_state_change_notifies_under_its_own_id(self):
        self.service._complete_app(APP_TOKEN)
        server.call_ha_service.reset_mock()
        self.http.gets = app_answers(user_status=401)
        self.assertEqual(self.ingress("POST", "/github/check").json["github"]["state"], "revoked")
        calls = [call.args for call in server.call_ha_service.call_args_list]
        self.assertEqual(calls[-1][0], "persistent_notification.create")
        self.assertEqual(calls[-1][1]["notification_id"], "codex_cli_github")
        self.assertNotIn(APP_TOKEN, json.dumps(calls))
        events = [call.args for call in server.fire_ha_event.call_args_list]
        self.assertEqual(events[-1][0], "codex_cli_github_access")
        self.assertEqual(events[-1][1]["state"], "revoked")


class GitHubRegistrationTests(unittest.TestCase):
    def test_the_tool_is_registered_and_never_required(self):
        """F9: a GitHub socket that fails cannot stop a chat from starting."""
        for args in (server.build_app_server_args(), server.github_mcp_args()):
            text = " ".join(args)
            self.assertIn("github_mcp.py", text)
            self.assertIn('mcp_servers.codex_github.required=false', text)
            self.assertIn('mcp_servers.codex_github.tools.github_git.approval_mode="approve"', text)
            self.assertNotIn("codex_github.env_vars", text)
            self.assertNotIn("CAPABILITY", text)

    def test_the_prompt_names_the_tool_only_when_connected(self):
        with patch.object(server.github_service, "current_state", return_value="disconnected"):
            self.assertNotIn("github_git", server.github_service.prompt_note())
        with patch.object(server.github_service, "current_state", return_value="ok"):
            self.assertIn("github_git", server.github_service.prompt_note())


    def test_worker_redaction_knows_github_tokens(self):
        self.assertNotIn(APP_TOKEN, server.redact(f"remote: {APP_TOKEN}"))


if __name__ == "__main__":
    unittest.main()
