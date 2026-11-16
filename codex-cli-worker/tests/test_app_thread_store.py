"""What a page exchange stores in the Codex thread: the fixed rules as developer instructions, the user's words
as the message, and this message's details through exchange_info.

The app-server messages here are written by hand from the Codex 0.166.0 protocol types (thread/start and
thread/resume take developerInstructions; turn/start takes input). They are not recordings of a real app server.
"""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_server import server
import test_approvals as approvals
import test_attachments as attachments
import test_ha_data as data
import ha_data_mcp
from ha_data import DataError, HomeAssistantData

OWNER_TEXT = "Turn the porch light on at sunset"


class ProtocolTests(unittest.TestCase):
    """The requests the worker sends to the app server for one page exchange."""

    def setUp(self):
        """A running task and a fake app-server transport that keeps what the worker writes."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in (("tasks", {}), ("running_processes", {})):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(server, "write_task_log")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.writer = approvals.Transport()
        self.proc = type("Process", (), {"stdin": self.writer, "poll": lambda _: None})()
        server.tasks["task"] = {"task_id": "task", "status": "running", "current_turn_id": "worker-turn",
                                "turns": [{"turn_id": "worker-turn", "status": "running"}]}
        server.running_processes["task"] = self.proc

    def requests(self, session_id):
        """Run initialize and the thread request, and return the thread and turn requests the worker sent."""
        session = server.create_approval_session("task", self.proc, OWNER_TEXT, Path(self.temp.name) / "final",
                                                 session_id, {}, server.DEFAULT_OPTIONS)
        session.start()
        initialize = self.writer.messages()[-1]
        session.receive({"id": initialize["id"], "result": {}})
        thread = self.writer.messages()[-1]
        session.receive({"id": thread["id"], "result": {
            "thread": {"id": session_id or "new-thread"}, "approvalPolicy": "on-request", "approvalsReviewer": "user"}})
        turn = self.writer.messages()[-1]
        return thread, turn

    def test_a_new_thread_gets_the_rules_as_developer_instructions_and_the_owner_text_as_its_message(self):
        """thread/start carries the fixed rules; turn/start input holds only the user's words, so they become the title."""
        thread, turn = self.requests(None)
        self.assertEqual(thread["method"], "thread/start")
        self.assertEqual(thread["params"]["developerInstructions"], server.PAGE_INSTRUCTIONS)
        self.assertEqual(turn["method"], "turn/start")
        self.assertEqual(turn["params"]["input"], [{"type": "text", "text": OWNER_TEXT}])

    def test_a_resumed_thread_gets_the_same_rules(self):
        """thread/resume carries them too: Codex keeps the first value, and uses this one after a compaction."""
        thread, turn = self.requests("thread")
        self.assertEqual(thread["method"], "thread/resume")
        self.assertEqual(thread["params"]["threadId"], "thread")
        self.assertEqual(thread["params"]["developerInstructions"], server.PAGE_INSTRUCTIONS)
        self.assertEqual(turn["params"]["input"], [{"type": "text", "text": OWNER_TEXT}])


class FixedTextTests(unittest.TestCase):
    """The fixed rules hold nothing that changes from one message to the next."""

    def test_the_rules_name_no_per_message_detail(self):
        """No task id, backup folder, attachment, token note, documentation date, GitHub note or user message."""
        text = server.PAGE_INSTRUCTIONS
        for changing in ("Task id", "codex_tasks", "copy it to", "The user attached", "HA_URL", "stored locally",
                         "github_git", "Current user message"):
            self.assertNotIn(changing, text)
        self.assertIn("call the home_assistant_data exchange_info tool once", text)
        self.assertIn("If exchange_info is not available, make no backup copy, and say in your answer that this "
                      "change has no backup.", text)

    def test_the_tool_needs_no_approval_and_the_app_thread_rules_point_to_it(self):
        """exchange_info is approved like history; the remote-control rules tell an app thread to call it on the page."""
        args = server.data_mcp_args()
        self.assertIn('mcp_servers.home_assistant_data.tools.exchange_info.approval_mode="approve"', args)
        tool = next(tool for tool in ha_data_mcp.TOOLS if tool["name"] == "exchange_info")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual(tool["inputSchema"]["properties"], {})
        with patch.object(server, "read_options", return_value={}):
            self.assertIn("call the home_assistant_data exchange_info tool first", server.remote_control_instructions({}))


class ExchangeRecordTests(unittest.TestCase):
    """run_task records which threads have the rules and what exchange_info returns for each message."""

    setUp = attachments.AttachmentTests.setUp
    create = attachments.AttachmentTests.create
    run_codex = attachments.AttachmentTests.run_codex

    def test_a_page_thread_has_the_rules_and_its_messages_are_the_owner_text(self):
        """The first exchange marks the thread; a follow-up gets no pointer and exchange_info holds no rules."""
        task_id = self.create(OWNER_TEXT)
        info, message = self.run_and_record_plain(task_id)
        self.assertEqual(message, "Draw a sheep")
        self.assertTrue(server.tasks[task_id]["page_instructions"])
        self.assertNotIn("rules", info)
        self.assertEqual(info["task_id"], task_id)
        info, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="And now the porch")
        self.assertEqual(message, "And now the porch")
        self.assertNotIn("rules", info)

    def test_a_thread_the_page_did_not_start_gets_a_pointer_and_the_rules_through_exchange_info(self):
        """A thread from the ChatGPT app or from 0.3.1 has no record: the message points to exchange_info, which has the rules."""
        task_id = self.create(OWNER_TEXT)
        server.update_task(task_id, session_id=attachments.SESSION_ID)
        self.assertNotIn("page_instructions", server.tasks[task_id])
        info, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="Check the porch light")
        self.assertEqual(message, server.RULES_POINTER + "\n\nCheck the porch light")
        self.assertEqual(info["rules"], server.PAGE_INSTRUCTIONS)
        # A resume never marks the thread: only a thread this page starts gets the rules at its first turn.
        self.assertNotIn("page_instructions", server.tasks[task_id])

    def test_the_exec_path_passes_the_rules_as_developer_instructions(self):
        """Upstream's codex exec path, reached only by tests, gets the same rules through its configuration."""
        task_id = self.create(OWNER_TEXT)
        with patch.object(server, "read_options", return_value={"codex_sandbox": "workspace-write"}):
            args = server.build_codex_args(task_id, Path("prompt"), Path("final"), None)
        self.assertIn("developer_instructions=" + json.dumps(server.PAGE_INSTRUCTIONS), args)

    def run_and_record_plain(self, task_id, session_id=None, reply=None):
        """Run one exchange through run_codex's fake process and return exchange_info's details and the message."""
        recorded = []
        keep = server.ha_data.set_exchange_info
        with patch.object(server.ha_data, "set_exchange_info", side_effect=lambda *a: (recorded.append(a), keep(*a))):
            self.run_codex(task_id, session_id=session_id, reply=reply)
        self.assertEqual([item[0] for item in recorded], [task_id])
        name = "prompt-resume.txt" if reply else "prompt.txt"
        return recorded[0][1], (server.get_run_dir(task_id) / name).read_text(encoding="utf-8")


class ExchangeInfoToolTests(unittest.TestCase):
    """The worker side of exchange_info: the running exchange's capability, no arguments, no query budget."""

    def setUp(self):
        """Begin an isolated exchange with its own data capability and recorded details."""
        self.worker = data.data_worker()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = HomeAssistantData(self.worker, Path(temporary.name) / "exports")
        self.addCleanup(self.engine.end, "chat")
        self.capability = self.engine.begin("chat")
        self.engine.set_exchange_info("chat", {"task_id": "chat", "backups": "Copy files to /config/backup-a."})

    def call(self, **fields):
        """Send one exchange_info request as the MCP bridge does."""
        return self.engine.dispatch({"operation": "exchange_info", "capability": self.capability, **fields})

    def test_the_running_exchange_reads_its_own_details(self):
        """The reply holds the recorded details, reads no Home Assistant data and uses no query budget."""
        result = self.call()
        self.assertEqual(result, {"status": "observed", "operation": "exchange_info", "task_id": "chat",
                                  "backups": "Copy files to /config/backup-a."})
        self.worker.ha_token.assert_not_called()
        self.assertEqual(self.engine.queries["chat"], 0)

    def test_arguments_and_a_wrong_or_ended_capability_are_refused(self):
        """Arguments are refused; a wrong capability and an ended or replaced exchange get nothing."""
        with self.assertRaises(DataError):
            self.call(path="/config")
        with self.assertRaises(DataError):
            self.engine.dispatch({"operation": "exchange_info", "capability": "not-the-capability"})
        old = self.capability
        self.capability = self.engine.begin("chat")
        self.assertEqual(self.call(), {"status": "unavailable", "message": "No details are recorded for this exchange"})
        with self.assertRaises(DataError):
            self.engine.dispatch({"operation": "exchange_info", "capability": old})
        self.engine.end("chat")
        with self.assertRaises(DataError):
            self.call()

    def test_the_bridge_accepts_the_longer_reply_only_for_exchange_info(self):
        """The rules pass 8 KiB, so exchange_info has its own limit; history keeps the old one."""
        reply = json.dumps({"status": "observed", "rules": "x" * (ha_data_mcp.MAX_RESPONSE_BYTES + 100)}) + "\n"
        self.assertGreater(len(server.PAGE_INSTRUCTIONS), ha_data_mcp.MAX_RESPONSE_BYTES // 2)
        self.assertLess(len(json.dumps({"rules": server.PAGE_INSTRUCTIONS}).encode()) + 4096,
                        ha_data_mcp.MAX_INFO_RESPONSE_BYTES)
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "data.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(2)
            self.addCleanup(listener.close)

            def answer_twice():
                """Answer two connections with the same long line."""
                for _ in range(2):
                    connection, _ = listener.accept()
                    with connection:
                        connection.makefile("rb").readline()
                        connection.sendall(reply.encode())

            threading.Thread(target=answer_twice, daemon=True).start()
            with patch.dict("os.environ", {"HA_DATA_SOCKET": path, "HA_DATA_CAPABILITY": "capability"}):
                self.assertEqual(len(ha_data_mcp.fetch("exchange_info", {})["rules"]), ha_data_mcp.MAX_RESPONSE_BYTES + 100)
                with self.assertRaises(ValueError):
                    ha_data_mcp.fetch("history", {})


if __name__ == "__main__":
    unittest.main()
