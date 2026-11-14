"""Scheduling and mailbox tools run from a chat; a tool question the page cannot show is reported."""

from __future__ import annotations

import json
import threading
import unittest

from test_server import server
from approvals import AppServerSession
from test_approvals import Transport
import scheduling_mcp

TOOLS = ("propose_schedule", "list_schedules", "read_mailbox", "post_mailbox")


def scheduling_settings(args: list[str]) -> dict[str, object]:
    """Return the codex_scheduling settings that a Codex command line carries."""
    prefix = "mcp_servers.codex_scheduling."
    settings = {}
    for flag, value in zip(args, args[1:]):
        if flag == "--config" and value.startswith(prefix):
            name, _, raw = value[len(prefix) :].partition("=")
            settings[name] = json.loads(raw)
    return settings


class SchedulingToolApprovalTests(unittest.TestCase):
    """Each scheduling and mailbox tool is approved ahead, as the verify and data tools are."""

    def test_chat_page_turns_run_every_tool_without_a_request(self):
        settings = scheduling_settings(server.build_app_server_args())
        for tool in TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(settings.get(f"tools.{tool}.approval_mode"), "approve")

    def test_only_the_four_documented_tools_are_approved(self):
        settings = scheduling_settings(server.scheduling_mcp_args())
        approved = {
            name.split(".")[1] for name in settings if name.endswith(".approval_mode")
        }
        self.assertEqual(approved, set(TOOLS))

    def test_the_list_matches_the_tools_the_server_offers(self):
        self.assertEqual({tool["name"] for tool in scheduling_mcp.TOOLS}, set(TOOLS))

    def test_scheduled_and_lead_turns_get_the_same_tools(self):
        """The other launcher registers the same server with the same settings."""
        self.assertEqual(
            scheduling_settings(server.scheduling_mcp_args()),
            scheduling_settings(server.build_app_server_args()),
        )

    def test_remote_threads_still_get_no_scheduling_tools(self):
        self.assertNotIn("codex_scheduling", " ".join(server.relay_read_mcp_args()))


class ElicitationTests(unittest.TestCase):
    """A tool's own question is declined where the chat shows it, never silently."""

    def setUp(self):
        self.writer = Transport()
        self.events = []
        self.session = AppServerSession(
            self.writer,
            lock=threading.RLock(),
            thread_params={
                "sandbox": "workspace-write",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
            turn_params={"input": [{"type": "text", "text": "Read the mailbox"}]},
            on_event=self.events.append,
            on_approvals=lambda records: None,
            on_final=lambda text: None,
            is_active=lambda: True,
            redact=lambda text: text,
        )
        self.session.start()
        self.session.receive({"id": "worker-1", "result": {}})
        self.session.receive(
            {
                "id": "worker-2",
                "result": {
                    "thread": {"id": "thread"},
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                },
            }
        )
        self.session.receive({"id": "worker-3", "result": {"turn": {"id": "turn"}}})

    def elicit(self, **params):
        self.session.receive(
            {
                "id": 7,
                "method": "mcpServer/elicitation/request",
                "params": {"threadId": "thread", "turnId": "turn", **params},
            }
        )
        return self.writer.messages()[-1]

    def test_question_is_declined_and_shown_in_the_chat(self):
        reply = self.elicit(serverName="codex_scheduling")
        self.assertEqual(reply, {"id": 7, "result": {"action": "decline"}})
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[-1]["type"], "error")
        self.assertIn(
            "The codex_scheduling tool asked a question", self.events[-1]["message"]
        )
        self.assertIn("nothing was changed", self.events[-1]["message"])

    def test_unusual_server_name_is_not_repeated(self):
        self.elicit(serverName="<b>click here</b>")
        self.assertTrue(
            self.events[-1]["message"].startswith("A tool asked a question")
        )
        self.assertNotIn("click", self.events[-1]["message"])

    def test_missing_server_name_still_reports(self):
        self.elicit()
        self.assertTrue(
            self.events[-1]["message"].startswith("A tool asked a question")
        )


if __name__ == "__main__":
    unittest.main()
