"""Codex's own request before a tool call becomes a page card that shows the request in full (CX-SIMPLE.B)."""

from __future__ import annotations

import json
import threading
import unittest

from test_approvals import Transport  # first: it puts the worker folder on the import path
import approvals
from approvals import AppServerSession

QUESTION = 'Allow the home_assistant_call MCP server to run tool "call"?'


def tool_params(**overrides):
    """A Home Assistant action as the plain tool receives it, with invented entity ids."""
    params = {"service": "light.turn_off", "target": {"entity_id": "light.kitchen_ceiling"}}
    params.update(overrides)
    return params


class ToolCardTests(unittest.TestCase):
    """One card for each tool request of the Home Assistant tool; every other question is declined as before."""

    def setUp(self):
        self.writer = Transport()
        self.events = []
        self.saved = []
        self.session = AppServerSession(
            self.writer,
            lock=threading.RLock(),
            thread_params={"sandbox": "workspace-write", "approvalPolicy": "on-request", "approvalsReviewer": "user"},
            turn_params={"input": [{"type": "text", "text": "Turn off the kitchen light"}]},
            on_event=self.events.append,
            on_approvals=self.saved.append,
            on_final=lambda text: None,
            is_active=lambda: True,
            redact=lambda text: text.replace("hidden-secret", "[redacted]"),
        )
        self.session.start()
        self.session.receive({"id": "worker-1", "result": {}})
        self.session.receive({"id": "worker-2", "result": {
            "thread": {"id": "thread"}, "approvalPolicy": "on-request", "approvalsReviewer": "user"}})
        self.session.receive({"id": "worker-3", "result": {"turn": {"id": "turn"}}})

    def ask(self, request_id=7, arguments=None, **overrides):
        """Send the request Codex 0.166.0 sends before a tool call; return the records saved for the page."""
        params = {
            "threadId": "thread", "turnId": "turn", "serverName": "home_assistant_call", "mode": "form",
            "message": QUESTION, "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {"codex_approval_kind": "mcp_tool_call", "persist": ["session", "always"],
                      "tool_params": tool_params() if arguments is None else arguments},
        }
        params.update(overrides)
        before = len(self.writer.messages())
        self.session.receive({"id": request_id, "method": "mcpServer/elicitation/request", "params": params})
        return self.writer.messages()[before:]

    def record(self):
        return self.saved[-1][-1]

    def test_request_waits_for_the_owner_and_sends_nothing_yet(self):
        self.assertEqual(self.ask(), [])
        record = self.record()
        self.assertEqual((record["kind"], record["status"], record["tool"]), ("tool", "pending", "home_assistant_call.call"))
        self.assertTrue(record["can_allow"])
        self.assertTrue(self.session.waiting_for_user())

    def test_card_shows_every_argument_exactly(self):
        arguments = tool_params(service="notify.notify", target=None,
                                data={"title": "Kitchen", "message": "Line one.\nLine two, with light.porch in it."})
        self.ask(arguments=arguments)
        self.assertEqual(json.loads(self.record()["arguments"]), arguments)

    def test_plain_words_name_the_action_and_only_its_target(self):
        self.ask(arguments=tool_params(data={"note": "not light.porch"}))
        plain = self.record()["confirmation"]
        self.assertEqual(plain["what"], "Run the Home Assistant action light.turn_off.")
        self.assertEqual(plain["where"], "light.kitchen_ceiling.")
        self.assertEqual(plain["effect"], "This turns off light.kitchen_ceiling.")

    def test_allow_once_accepts_this_call_only(self):
        self.ask()
        self.session.decide(self.record()["approval_id"], "allow")
        # No persist value: Codex reads a plain accept as one approval and asks again before the next call.
        self.assertEqual(self.writer.messages()[-1],
                         {"id": 7, "result": {"action": "accept", "content": None, "_meta": None}})
        self.assertEqual(self.record()["status"], "allowed")

    def test_deny_declines(self):
        self.ask()
        self.session.decide(self.record()["approval_id"], "deny")
        self.assertEqual(self.writer.messages()[-1],
                         {"id": 7, "result": {"action": "decline", "content": None, "_meta": None}})

    def assert_deny_only(self):
        record = self.record()
        self.assertFalse(record["can_allow"])
        self.assertIn("cannot be allowed here", record["confirmation"]["effect"])
        before = len(self.writer.messages())
        with self.assertRaises(ValueError):
            self.session.decide(record["approval_id"], "allow")
        self.assertEqual(len(self.writer.messages()), before)

    def test_clipped_request_has_no_allow(self):
        self.ask(arguments=tool_params(data={"message": "x" * (approvals.DISPLAY_LIMIT + 1)}))
        self.assert_deny_only()

    def test_request_with_a_hidden_value_has_no_allow(self):
        self.ask(arguments=tool_params(data={"message": "hidden-secret"}))
        self.assertNotIn("hidden-secret", self.record()["arguments"])
        self.assert_deny_only()

    def test_request_without_arguments_has_no_allow(self):
        self.ask(arguments="light.turn_off")
        self.assert_deny_only()

    def test_other_questions_are_still_declined_in_the_chat(self):
        cases = {
            "another server": {"serverName": "codex_scheduling",
                               "message": 'Allow the codex_scheduling MCP server to run tool "post_mailbox"?'},
            "message names another server": {"message": 'Allow the github MCP server to run tool "call"?'},
            "another tool of the server": {"message": 'Allow the home_assistant_call MCP server to run tool "read"?'},
            "the server's own question": {"_meta": None, "message": "Pick a light"},
            "not a form": {"mode": "url"},
            "a different sentence": {"message": QUESTION + " Also allow everything"},
        }
        for number, (name, overrides) in enumerate(cases.items(), start=10):
            with self.subTest(name):
                reply = self.ask(request_id=number, **overrides)
                self.assertEqual(reply, [{"id": number, "result": {"action": "decline"}}])
                self.assertEqual(self.events[-1]["type"], "error")
        self.assertEqual(self.saved, [])

    def test_request_for_another_turn_is_refused(self):
        with self.assertRaises(ValueError):
            self.ask(turnId="old-turn")
        self.assertEqual(self.saved, [])

    def test_resolved_elsewhere_expires_the_card(self):
        self.ask()
        self.session.receive({"method": "serverRequest/resolved", "params": {"threadId": "thread", "requestId": 7}})
        self.assertEqual(self.record()["status"], "expired")
        with self.assertRaises(LookupError):
            self.session.decide(self.record()["approval_id"], "allow")

    def test_closed_exchange_expires_the_card(self):
        self.ask()
        self.session.close()
        self.assertEqual(self.record()["status"], "expired")
        self.assertFalse(any(message.get("result", {}).get("action") == "accept" for message in self.writer.messages()))

    def test_over_the_limit_is_declined_in_the_tool_form(self):
        self.session._records.extend({"approval_id": str(n), "status": "denied"} for n in range(approvals.REQUEST_LIMIT))
        reply = self.ask()
        self.assertEqual(reply, [{"id": 7, "result": {"action": "decline", "content": None, "_meta": None}}])


if __name__ == "__main__":
    unittest.main()
