"""The add-on's own grant layer is gone: no launch approves a house action ahead of Codex's own permissions.

Version 0.3.5 removed the action groups, the permission overrides and the ``act`` tool
(``home_assistant_actions``). These tests fail if any part of that layer comes back by mistake.
"""

from __future__ import annotations

import unittest
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from test_server import server

WORKER = Path(__file__).resolve().parents[1]
REMOVED_SERVER = "home_assistant_actions"
REMOVED_MODULES = ("permission_overrides", "permission_overrides_api", "permission_overrides_mcp")


def configured_servers(args: list[str]) -> set[str]:
    """Return the name of every MCP server that a Codex command line configures."""
    names = set()
    for flag, value in pairwise(args):
        if flag == "--config" and value.startswith("mcp_servers."):
            names.add(value[len("mcp_servers."):].split(".", 1)[0])
    return names


class LaunchTests(unittest.TestCase):
    """Each way Codex starts: the chat page (and a schedule run, which starts the same way) and the ChatGPT app."""

    def page_launches(self) -> dict[str, list[str]]:
        task = {"task_id": "t", "turns": [], "chat_settings": server.DEFAULT_CHAT_SETTINGS}
        with patch.object(server, "tasks", {"t": task}), patch.object(server, "read_options", return_value={}):
            return {
                "page": server.build_app_server_args(),
                "schedule run": server.build_app_server_args(attended=False),
            }

    def test_no_page_or_schedule_launch_registers_the_act_tool(self):
        for name, args in self.page_launches().items():
            with self.subTest(launch=name):
                servers = configured_servers(args)
                # The other tool servers are still there, so the parsing above is not what makes this pass.
                self.assertIn("home_assistant", servers)
                self.assertNotIn(REMOVED_SERVER, servers)
                for module in REMOVED_MODULES:
                    self.assertFalse(any(module in arg for arg in args))

    def test_the_app_launch_registers_the_read_tool_and_the_call_tool_only(self):
        self.assertEqual(configured_servers(server.relay_tool_args()), {"home_assistant_relay", "home_assistant_call"})

    def test_the_page_rules_no_longer_name_the_act_tool(self):
        self.assertNotIn(REMOVED_SERVER, server.PAGE_INSTRUCTIONS)
        self.assertNotIn(".act", server.PAGE_INSTRUCTIONS)


class RemovedPartsTests(unittest.TestCase):

    def test_the_settings_route_is_gone(self):
        rules = {rule.rule for rule in server.app.url_map.iter_rules()}
        self.assertIn("/health", rules)
        self.assertNotIn("/permissions-overrides", rules)

    def test_no_module_of_the_layer_is_in_the_worker_or_the_image(self):
        for module in REMOVED_MODULES:
            with self.subTest(module=module):
                self.assertFalse((WORKER / f"{module}.py").exists())
        self.assertNotIn("permission_overrides", (WORKER / "Dockerfile").read_text(encoding="utf-8"))

    def test_the_server_no_longer_opens_the_overrides_socket(self):
        source = (WORKER / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("permission-overrides.sock", source)
        self.assertNotIn("override_service", source)

    def test_the_approval_session_has_no_owner_action_request(self):
        import approvals
        self.assertFalse(hasattr(approvals.AppServerSession, "ask_owner_action"))
        self.assertFalse(hasattr(approvals, "ACTION_ANSWER_SECONDS"))


if __name__ == "__main__":
    unittest.main()
