"""'Allow for this chat': the worker maps it to Codex's session decision, only where Codex has one."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_approvals import HA_USERS, Transport, ingress_as  # first: it puts the worker folder on the import path
from test_server import server
from approvals import (AppServerSession, COMMAND_APPROVAL, FILE_APPROVAL, SESSION_DECISION, TOOL_APPROVAL,
                       decision_result)

# Codex 0.166.0's question before a call of the plain Home Assistant tool, with invented arguments.
TOOL_QUESTION = {
    "serverName": "home_assistant_call", "mode": "form",
    "message": 'Allow the home_assistant_call MCP server to run tool "call"?',
    "requestedSchema": {"type": "object", "properties": {}},
    "_meta": {"codex_approval_kind": "mcp_tool_call", "persist": ["session", "always"],
              "tool_params": {"service": "light.turn_off", "target": {"entity_id": "light.example"}}},
}


class ChatDecisionTests(unittest.TestCase):
    """The session offers the chat-wide answer for a command or file change, and sends acceptForSession for it."""

    def make(self, *, shared: bool) -> AppServerSession:
        """Start a turn on an owned connection (the start path) or a shared one (the live process)."""
        self.writer = Transport()
        self.saved = []
        session = AppServerSession(
            self.writer, lock=threading.RLock(),
            thread_params={"sandbox": "workspace-write", "approvalPolicy": "on-request", "approvalsReviewer": "user",
                           **({"threadId": "thread"} if shared else {})},
            turn_params={"input": [{"type": "text", "text": "Check a sensor"}]},
            on_event=lambda event: None, on_approvals=self.saved.append, on_final=lambda text: None,
            is_active=lambda: True, redact=lambda text: text.replace("secret-value", "[redacted]"),
            owns_connection=not shared,
        )
        session.start()
        if shared:
            session.receive({"id": f"{session.request_prefix}-1", "result": {"turn": {"id": "turn"}}})
        else:
            session.receive({"id": "worker-1", "result": {}})
            session.receive({"id": "worker-2", "result": {"thread": {"id": "thread"}}})
            session.receive({"id": "worker-3", "result": {"turn": {"id": "turn"}}})
        return session

    def ask(self, session, method=COMMAND_APPROVAL, request_id=5, **fields):
        """Send one request with its real protocol field names; return the record the page reads."""
        params = {"threadId": "thread", "turnId": "turn", "itemId": "item"}
        if method == COMMAND_APPROVAL:
            params.update({"command": "ls /homeassistant", "cwd": "/homeassistant"})
        session.receive({"id": request_id, "method": method, "params": {**params, **fields}})
        return self.saved[-1][-1]

    def sent(self):
        """The last message the worker wrote to Codex."""
        return self.writer.messages()[-1]

    def test_live_chat_command_is_allowed_for_the_chat_with_the_session_decision(self):
        session = self.make(shared=True)
        record = self.ask(session)
        self.assertTrue(record["can_allow_chat"])
        self.assertEqual(record["chat_label"], "Allow for this chat")
        self.assertIn("this same command in this folder", record["chat_scope"])
        self.assertIn("until the chat is paused or the app restarts", record["chat_scope"])
        session.decide(record["approval_id"], "allow_chat")
        self.assertEqual(self.sent(), {"id": 5, "result": {"decision": "acceptForSession"}})
        self.assertEqual((self.saved[-1][-1]["status"], self.saved[-1][-1]["allowed_for"]),
                         ("allowed", "Allowed for this chat"))

    def test_start_path_says_the_answer_lasts_for_this_message(self):
        """A Codex process of its own ends with the exchange, and so does Codex's session cache."""
        session = self.make(shared=False)
        record = self.ask(session)
        self.assertEqual(record["chat_label"], "Allow for this message")
        self.assertIn("until this message is done", record["chat_scope"])
        session.decide(record["approval_id"], "allow_chat")
        self.assertEqual(self.sent()["result"], {"decision": SESSION_DECISION})
        self.assertEqual(self.saved[-1][-1]["allowed_for"], "Allowed for this message")

    def test_file_change_is_allowed_for_the_chat(self):
        session = self.make(shared=True)
        session.receive({"method": "item/started", "params": {
            "threadId": "thread", "turnId": "turn", "item": {"id": "item", "type": "fileChange", "status": "inProgress",
                "changes": [{"path": "/homeassistant/example.yaml", "kind": {"type": "update"}, "diff": "-a\n+b"}]}}})
        record = self.ask(session, FILE_APPROVAL)
        self.assertIn("these same files", record["chat_scope"])
        session.decide(record["approval_id"], "allow_chat")
        self.assertEqual(self.sent()["result"], {"decision": "acceptForSession"})

    def test_network_request_names_the_host(self):
        session = self.make(shared=True)
        record = self.ask(session, networkApprovalContext={"host": "example.test", "protocol": "https"})
        self.assertIn("Codex reaches example.test without asking again", record["chat_scope"])

    def test_allow_once_still_sends_one_approval(self):
        session = self.make(shared=True)
        record = self.ask(session)
        session.decide(record["approval_id"], "allow")
        self.assertEqual(self.sent()["result"], {"decision": "accept"})
        self.assertNotIn("allowed_for", self.saved[-1][-1])

    def test_tool_question_has_no_chat_answer(self):
        """A tool's session answer would allow every later call with any arguments, so the card does not offer it."""
        session = self.make(shared=True)
        before = len(self.writer.messages())
        record = self.ask(session, TOOL_APPROVAL, **TOOL_QUESTION)
        self.assertTrue(record["can_allow"])
        self.assertFalse(record["can_allow_chat"])
        self.assertNotIn("chat_label", record)
        with self.assertRaises(ValueError):
            session.decide(record["approval_id"], "allow_chat")
        self.assertEqual(len(self.writer.messages()), before)
        self.assertEqual(self.saved[-1][-1]["status"], "pending")
        with self.assertRaises(ValueError):
            decision_result("tool", "allow_chat")

    def test_requests_that_cannot_be_allowed_once_cannot_be_allowed_for_the_chat(self):
        session = self.make(shared=True)
        for request_id, fields in enumerate([
            {"kind": "writeStdin"},
            {"command": "tool --password secret-value"},
            {"command": ""},
        ]):
            with self.subTest(fields=fields):
                record = self.ask(session, request_id=request_id, **fields)
                self.assertFalse(record["can_allow_chat"])
                with self.assertRaises(ValueError):
                    session.decide(record["approval_id"], "allow_chat")
        self.assertEqual(self.writer.messages()[-1]["method"], "turn/start")

    def test_the_offer_follows_the_decisions_codex_lists(self):
        session = self.make(shared=True)
        listed = self.ask(session, request_id=1, availableDecisions=["accept", "acceptForSession", "decline"])
        self.assertTrue(listed["can_allow_chat"])
        unlisted = self.ask(session, request_id=2, availableDecisions=["accept", "decline", "cancel"])
        self.assertFalse(unlisted["can_allow_chat"])
        with self.assertRaises(ValueError):
            session.decide(unlisted["approval_id"], "allow_chat")

    def test_unknown_decision_words_are_refused(self):
        session = self.make(shared=True)
        record = self.ask(session)
        for word in ("acceptForSession", "allow_always", "ALLOW_CHAT", ""):
            with self.subTest(word=word), self.assertRaises(ValueError):
                session.decide(record["approval_id"], word)
        self.assertEqual(self.saved[-1][-1]["status"], "pending")


class ChatDecisionApiTests(unittest.TestCase):
    """The page sends the word allow_chat; the route passes it to the session and never a protocol value."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for item in [patch.dict(server.tasks, {}, clear=True), patch.object(server, "approval_sessions", {}),
                     patch.object(server, "running_processes", {}), patch.object(server.store, "save_index"),
                     patch.object(server, "api_token", return_value="worker-secret"),
                     patch.object(server, "lookup_ha_users", return_value=HA_USERS),
                     patch.object(server, "admin_cache", {}),
                     patch.object(server.store, "task_dir", return_value=Path(self.temp.name) / "task"),
                     patch.object(server, "task_root", return_value=Path(self.temp.name)),
                     patch.object(server, "TASK_STATE_FILE", Path(self.temp.name) / "index.json"),
                     patch.object(server, "write_task_log"), patch.object(server, "notify_permission_async")]:
            item.start()
            self.addCleanup(item.stop)
        self.writer = Transport()
        proc = type("Process", (), {"stdin": self.writer, "poll": lambda _: None})()
        server.tasks["task"] = {"task_id": "task", "status": "running", "current_turn_id": "worker-turn",
                                "turns": [{"turn_id": "worker-turn", "status": "running"}]}
        server.running_processes["task"] = proc
        session = server.create_approval_session("task", proc, "Read history", Path(self.temp.name) / "final", None, {},
                                                 server.DEFAULT_OPTIONS)
        session.thread_id, session.turn_id = "thread", "turn"
        server.approval_sessions["task"] = session
        self.session = session
        self.client = server.app.test_client()

    def ask(self, request_id, method=COMMAND_APPROVAL, **fields):
        params = {"threadId": "thread", "turnId": "turn", "itemId": "item", "command": "ls", "cwd": "/homeassistant"}
        self.session.receive({"id": request_id, "method": method, "params": {**params, **fields}})
        return server.tasks["task"]["approvals"][-1]["approval_id"]

    def answer(self, approval_id, decision):
        return self.client.post(f"/tasks/task/approvals/{approval_id}",
                                json={"decision": decision, "turn_id": "worker-turn"}, **ingress_as())

    def test_allow_chat_reaches_codex_as_its_session_decision(self):
        response = self.answer(self.ask(10), "allow_chat")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.writer.messages(), [{"id": 10, "result": {"decision": "acceptForSession"}}])

    def test_a_request_without_the_offer_is_refused_and_still_waits(self):
        approval_id = self.ask(11, TOOL_APPROVAL, **TOOL_QUESTION)
        self.assertEqual(self.answer(approval_id, "allow_chat").status_code, 400)
        self.assertEqual(self.answer(approval_id, "acceptForSession").status_code, 400)
        self.assertEqual(self.writer.messages(), [])
        self.assertEqual(server.tasks["task"]["approvals"][-1]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
