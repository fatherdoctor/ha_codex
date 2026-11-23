"""What a page exchange stores in the Codex thread: the fixed rules as developer instructions, the user's words
as the message, this message's details through exchange_info, and the answer as plain text, with the page's
status, question and choices from the ask_owner and report_failure tool calls.

The app-server messages here are written by hand from the Codex 0.166.0 protocol types (thread/start and
thread/resume take developerInstructions; turn/start takes input; an mcpToolCall item carries server, tool, status
and arguments). They are not recordings of a real app server.
"""
import json
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_server import server
import tool_threads
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
        # A chat from before 0.6 resumes in the new folder and is told what its old /config/ paths mean (CX-HA-PATH).
        self.assertEqual(thread["params"]["cwd"], server.CONFIG_ROOT.as_posix())
        self.assertIn(server.old_config_note(), thread["params"]["developerInstructions"])
        self.assertEqual(turn["params"]["input"], [{"type": "text", "text": OWNER_TEXT}])


class FixedTextTests(unittest.TestCase):
    """The fixed rules hold nothing that changes from one message to the next."""

    def test_the_rules_map_an_old_config_path_and_keep_home_assistants_own_yaml(self):
        """Since 0.6 the folder is /homeassistant; /config/ in Home Assistant's own YAML stays (CX-HA-PATH)."""
        text = server.PAGE_INSTRUCTIONS
        self.assertIn("Workspace: /homeassistant\n", text)
        self.assertIn("Home Assistant's configuration folder is /homeassistant. /config does not exist here; a path "
                      "in this chat or in the owner's text that starts with /config/ means the same file under "
                      "/homeassistant/. Inside Home Assistant's own YAML, /config/ is Home Assistant's own path and "
                      "stays as it is.", text)
        self.assertNotIn("@@", text)

    def test_the_rules_name_no_per_message_detail(self):
        """No task id, backup folder, attachment, token note, documentation date, GitHub note or user message."""
        text = server.PAGE_INSTRUCTIONS
        for changing in ("Task id", "codex_tasks", "copy it to", "The user attached", "HA_URL", "stored locally",
                         "github_git", "Current user message"):
            self.assertNotIn(changing, text)
        self.assertIn("call the home_assistant_data exchange_info tool once", text)
        self.assertIn("If exchange_info is not available, make no backup copy, and say in your answer that this "
                      "change has no backup.", text)

    def test_a_plain_message_needs_no_tool_call(self):
        """A greeting or a one-number reply is answered directly: no forced exchange_info call, no file check.

        Codex loads the owner's AGENTS.md through the CODEX_HOME link when a chat starts, so the rules never send
        the model to the shell to look for it.
        """
        text = server.PAGE_INSTRUCTIONS
        self.assertNotIn("At the start of each message", text)
        self.assertNotIn("check for the file yourself", text)
        self.assertIn("needs no tool call and no command: answer it directly", text)
        self.assertIn("Codex has already loaded that file into this chat when the chat started, so never run a "
                      "command to look for it or to read it.", text)

    def test_any_command_file_change_or_tool_call_still_gets_this_message_details_first(self):
        """A message that acts (a Home Assistant call, a file edit, a command) still calls exchange_info first.

        Only the two status tools are exempt.
        """
        text = server.PAGE_INSTRUCTIONS
        self.assertIn("before your first command, file change or tool call in a message (ask_owner and "
                      "report_failure excepted), and when the message can refer to a file", text)
        self.assertIn("call the home_assistant_data exchange_info tool once.", text)
        self.assertIn("Follow its backups text before you change, move, or delete a file under /homeassistant.", text)

    def test_a_message_about_a_document_calls_exchange_info_without_the_word_attached(self):
        """Only exchange_info names an attached document (images go in the turn input), so "Summarize this" with a
        document must call it: the rule follows what the message can refer to, not the word "attached"."""
        text = server.PAGE_INSTRUCTIONS
        self.assertNotIn("when the user says they attached or uploaded a file", text)
        self.assertIn("when the message can refer to a file, a document or something that you cannot see in this "
                      "chat (only exchange_info names the documents the user attached), call the home_assistant_data "
                      "exchange_info tool once.", text)

    def test_the_exchange_info_tool_description_agrees_with_the_page_rules(self):
        """Codex reads the tool description with the page rules on every message, so both give the same order."""
        tool = next(tool for tool in ha_data_mcp.TOOLS if tool["name"] == "exchange_info")
        description = tool["description"]
        self.assertNotIn("at the start of each message", description.lower())
        self.assertIn("Call it once in a message before your first command, file change or other tool call", description)
        self.assertIn("when the message can refer to a file, a document or something you cannot see in this chat",
                      description)
        self.assertIn("A message you can answer from what you know and from the chat needs no call.", description)

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
        """A thread from the ChatGPT app or from 0.3.2 or earlier has no record.

        The message points to exchange_info, which has the rules.
        """
        task_id = self.create(OWNER_TEXT)
        server.update_task(task_id, session_id=attachments.SESSION_ID)
        self.assertNotIn("page_instructions", server.tasks[task_id])
        info, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="Check the porch light")
        # Its permissions are told once too: what the app set for the thread is not known here.
        changed = server.PERMISSIONS_CHANGED.format(note=info["permissions"])
        self.assertEqual(message, server.RULES_POINTER + "\n\n" + changed + "\n\nCheck the porch light")
        self.assertEqual(info["rules"], server.build_page_instructions())
        # A resume never marks the thread: only a thread this page starts gets the rules at its first turn.
        self.assertNotIn("page_instructions", server.tasks[task_id])

    def test_a_chat_switched_to_full_access_is_told_once_on_its_next_message(self):
        """A chat that started in Read only and is switched to Full access is told so on the next message only."""
        task_id = self.create(OWNER_TEXT)
        server.update_task(task_id, permission_mode="read-only")
        info, message = self.run_and_record_plain(task_id)
        self.assertEqual(message, "Draw a sheep")
        self.assertIn("sandbox read-only", info["permissions"])
        server.update_task(task_id, permission_mode="full-access")
        with patch.object(server.full_access, "is_on", return_value=True):
            info, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="Fix the porch")
            full = server.permissions_note(server.protected_policy.mode_settings("full-access"))
            self.assertEqual(message, server.PERMISSIONS_CHANGED.format(note=full) + "\n\nFix the porch")
            self.assertEqual(info["permissions"], full)
            self.assertIn("sandbox danger-full-access", full)
            _, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="And the gate")
        self.assertEqual(message, "And the gate")

    def test_full_access_with_the_switch_off_tells_codex_the_mode_it_really_runs_in(self):
        """The owner's switch off makes a Full access chat run in Default; Codex is told Default, not Full access."""
        task_id = self.create(OWNER_TEXT)
        server.update_task(task_id, permission_mode="read-only")
        self.run_and_record_plain(task_id)
        server.update_task(task_id, permission_mode="full-access")
        with patch.object(server.full_access, "is_on", return_value=False):
            info, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="Fix it")
        self.assertIn("sandbox workspace-write, approval policy on-request", info["permissions"])
        self.assertTrue(message.startswith(server.PERMISSIONS_CHANGED.format(note=info["permissions"])))

    def test_a_resumed_thread_with_no_record_is_told_its_permissions_once(self):
        """A thread from the ChatGPT app or an earlier version has no record of what it was told."""
        task_id = self.create(OWNER_TEXT)
        server.update_task(task_id, session_id=attachments.SESSION_ID, page_instructions=True)
        info, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="Check the porch")
        self.assertEqual(message, server.PERMISSIONS_CHANGED.format(note=info["permissions"]) + "\n\nCheck the porch")
        _, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="And the gate")
        self.assertEqual(message, "And the gate")

    def test_a_message_whose_turn_never_started_is_told_again_on_the_next_one(self):
        """A failed start (here the process cannot start) means Codex never read the line, so the next message has it."""
        task_id = self.create(OWNER_TEXT)
        server.update_task(task_id, permission_mode="read-only")
        self.run_and_record_plain(task_id)
        server.update_task(task_id, permission_mode="full-access")
        with patch.object(server.full_access, "is_on", return_value=True):
            with patch.object(server.subprocess, "Popen", side_effect=OSError("no such file")):
                server.run_task(task_id, "Draw a sheep", attachments.SESSION_ID, "Fix the porch")
            server.active_task_runners.discard(task_id)
            self.assertEqual(server.tasks[task_id]["status"], "failed")
            info, message = self.run_and_record_plain(task_id, session_id=attachments.SESSION_ID, reply="Fix it now")
        self.assertEqual(message, server.PERMISSIONS_CHANGED.format(note=info["permissions"]) + "\n\nFix it now")
        self.assertIn("sandbox danger-full-access", info["permissions"])

    def test_with_a_read_only_sandbox_default_and_auto_review_give_different_notes(self):
        """The app's sandbox option at read-only: a change between Default and Auto-review still changes the note."""
        task_id = self.create(OWNER_TEXT)
        options = {"codex_sandbox": "read-only"}
        server.update_task(task_id, permission_mode="default")
        default = server.permissions_note(server.task_mode_settings(task_id, options))
        server.update_task(task_id, permission_mode="auto-review")
        auto = server.permissions_note(server.task_mode_settings(task_id, options))
        self.assertIn("sandbox read-only", default)
        self.assertIn("sandbox read-only", auto)
        self.assertIn("the person's approval", default)
        self.assertIn("the automatic reviewer's approval", auto)

    def test_the_rules_are_the_threads_developer_instructions(self):
        """Every thread this page starts gets the fixed rules through the app-server thread settings."""
        task_id = self.create(OWNER_TEXT)
        session = server.create_approval_session(
            task_id, attachments.StubAppServer(attachments.SESSION_ID), "Prompt", Path("final"), None, {},
            {"codex_sandbox": "workspace-write"})
        self.assertEqual(session.thread_params["developerInstructions"], server.build_page_instructions())

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
        tool_threads.CHATS.bind("chat", data.THREAD)
        self.addCleanup(tool_threads.CHATS.release, "chat")
        self.capability = self.engine.begin("chat")
        self.engine.set_exchange_info("chat", {"task_id": "chat", "backups": "Copy files to /homeassistant/backup-a."})

    def call(self, **fields):
        """Send one exchange_info request as the MCP bridge does."""
        return self.engine.dispatch({"operation": "exchange_info", "thread_id": data.THREAD, **fields})

    def test_the_running_exchange_reads_its_own_details(self):
        """The reply holds the recorded details, reads no Home Assistant data and uses no query budget."""
        result = self.call()
        self.assertEqual(result, {"status": "observed", "operation": "exchange_info", "task_id": "chat",
                                  "backups": "Copy files to /homeassistant/backup-a."})
        self.worker.ha_token.assert_not_called()
        self.assertEqual(self.engine.queries["chat"], 0)

    def test_arguments_and_a_wrong_thread_or_ended_exchange_are_refused(self):
        """Arguments are refused; a wrong thread and an ended or released exchange get nothing."""
        with self.assertRaises(DataError):
            self.call(path="/homeassistant")
        for thread in ("not-a-thread", "", None, "99999999-2222-4333-8444-555555555555"):
            with self.assertRaises(DataError):
                self.engine.dispatch({"operation": "exchange_info", "thread_id": thread})
        with self.assertRaises(DataError):
            self.engine.dispatch({"operation": "exchange_info", "capability": self.capability})
        self.capability = self.engine.begin("chat")
        self.assertEqual(self.call(), {"status": "unavailable", "message": "No details are recorded for this exchange"})
        self.engine.end("chat")
        with self.assertRaises(DataError):
            self.call()
        self.capability = self.engine.begin("chat")
        tool_threads.CHATS.release("chat")
        with self.assertRaises(DataError):
            self.call()

    def test_the_bridge_accepts_the_longer_reply_only_for_exchange_info(self):
        """The rules pass 8 KiB, so exchange_info has its own limit; history keeps the old one."""
        reply = json.dumps({"status": "observed", "rules": "x" * (ha_data_mcp.MAX_RESPONSE_BYTES + 100)}) + "\n"
        self.assertGreater(len(server.PAGE_INSTRUCTIONS), ha_data_mcp.MAX_RESPONSE_BYTES // 2)
        self.assertLess(len(json.dumps({"rules": server.PAGE_INSTRUCTIONS}).encode()) + 4096,
                        ha_data_mcp.MAX_INFO_RESPONSE_BYTES)
        if os.environ.get("PYTEST_CURRENT_TEST"):
            # Match the Home Assistant pytest harness: permit this Unix socket only, with every IP connection
            # still blocked.
            import pytest_socket
            pytest_socket.socket_allow_hosts([], allow_unix_socket=True)
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

            answering = threading.Thread(target=answer_twice, daemon=True)
            answering.start()
            with patch.dict("os.environ", {"HA_DATA_SOCKET": path}):
                self.assertEqual(len(ha_data_mcp.fetch("exchange_info", {}, data.THREAD)["rules"]), ha_data_mcp.MAX_RESPONSE_BYTES + 100)
                with self.assertRaises(ValueError):
                    ha_data_mcp.fetch("history", {}, data.THREAD)
            answering.join(timeout=5)
            self.assertFalse(answering.is_alive())


def tool_call(tool, arguments, *, status="completed", server_name="home_assistant_data"):
    """An item of a finished MCP tool call, as the worker's event handler receives it (written by hand)."""
    return {"id": f"call-{tool}", "type": "mcp_tool_call", "server": server_name, "tool": tool,
            "status": status, "arguments": arguments}


ASK = tool_call("ask_owner", {"question": "Remove the duplicate automation?", "choices": ["Go ahead", "Keep both"]})
FAIL = tool_call("report_failure", {"reason": "The entity light.porch does not exist."})


class StatusToolBridgeTests(unittest.TestCase):
    """ask_owner and report_failure in the MCP bridge: answered there, approved, and held to the page's limits."""

    def call(self, name, arguments):
        """Call a tool through the bridge's JSON-RPC handler, with the worker's socket out of reach."""
        with patch.object(ha_data_mcp.socket, "socket", side_effect=AssertionError("the socket must not be used")):
            return ha_data_mcp.respond({"method": "tools/call", "params": {"name": name, "arguments": arguments}})

    def test_a_good_call_is_answered_by_the_bridge_and_tells_codex_to_write_the_question_as_its_answer(self):
        """F3.2: the result tells Codex to end the turn and put the question, or the reason, in its answer."""
        result = self.call("ask_owner", ASK["arguments"])
        self.assertFalse(result["isError"])
        self.assertIn("write the question, with its choices, as your answer", result["content"][0]["text"])
        result = self.call("report_failure", FAIL["arguments"])
        self.assertFalse(result["isError"])
        self.assertIn("explain the reason in your answer", result["content"][0]["text"])

    def test_a_call_over_the_limits_is_refused_with_the_reason(self):
        """F3.4: Codex learns which limit it broke, and can call again."""
        cases = [
            ("ask_owner", {}),
            ("ask_owner", {"question": "  "}),
            ("ask_owner", {"question": "x" * (ha_data_mcp.QUESTION_MAX_LENGTH + 1)}),
            ("ask_owner", {"question": "Which?", "choices": ["a", "b", "c", "d"]}),
            ("ask_owner", {"question": "Which?", "choices": ["x" * (ha_data_mcp.CHOICE_MAX_LENGTH + 1)]}),
            ("ask_owner", {"question": "Which?", "choices": "Go ahead"}),
            ("ask_owner", {"question": "Which?", "status": "completed"}),
            ("report_failure", {"reason": 3}),
            ("report_failure", []),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                result = self.call(name, arguments)
                self.assertTrue(result["isError"])
                self.assertTrue(result["content"][0]["text"].startswith("Not recorded:"))

    def test_the_status_tools_need_no_approval(self):
        """F3.1: without an approval mode, each question would first ask the owner for permission to ask."""
        settings = server.data_mcp_args()
        for tool in ("ask_owner", "report_failure"):
            self.assertIn(f'mcp_servers.home_assistant_data.tools.{tool}.approval_mode="approve"', settings)
        for tool in ha_data_mcp.STATUS_TOOLS:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual((server.CHOICES_MAX, server.CHOICE_MAX_LENGTH),
                         (ha_data_mcp.CHOICES_MAX, ha_data_mcp.CHOICE_MAX_LENGTH))

    def test_the_rules_ask_for_plain_text_and_the_tools(self):
        """The fixed rules name both tools and plain-text answers, and no longer describe the JSON object."""
        rules = server.PAGE_INSTRUCTIONS
        self.assertIn("call the home_assistant_data ask_owner tool", rules)
        self.assertIn("write the same question as your answer", rules)
        self.assertIn("call the home_assistant_data report_failure tool", rules)
        self.assertIn("Never answer with a JSON object", rules)
        self.assertNotIn('status "needs_input"', rules)


class StatusCallTests(unittest.TestCase):
    """Which tool calls the worker uses, and in which order (F3.3, F3.4)."""

    def test_only_a_finished_good_call_to_the_worker_tools_counts(self):
        """A call in progress or failed, to another server, or over the limits is not used."""
        self.assertEqual(server.status_call(ASK), {"tool": "ask_owner", "question": "Remove the duplicate automation?",
                                                   "choices": ["Go ahead", "Keep both"]})
        self.assertEqual(server.status_call(FAIL), {"tool": "report_failure",
                                                    "reason": "The entity light.porch does not exist."})
        ignored = [
            {**ASK, "status": "in_progress"},
            {**ASK, "status": "failed"},
            {**ASK, "server": "home_assistant"},
            {**ASK, "type": "command_execution"},
            {**ASK, "tool": "history"},
            {**ASK, "arguments": {"question": "Which?", "choices": ["a", "b", "c", "d"]}},
            {**ASK, "arguments": {"question": "x" * (ha_data_mcp.QUESTION_MAX_LENGTH + 1)}},
            {**ASK, "arguments": "Which?"},
            {**FAIL, "arguments": {"reason": ""}},
            None,
        ]
        for item in ignored:
            with self.subTest(item=item):
                self.assertIsNone(server.status_call(item))

    def test_order_of_precedence(self):
        """The worker's failure, then report_failure, then ask_owner, then completed; of one tool the last counts."""
        answer = {"status": "completed", "summary": "Plain answer text.", "details": ""}
        ask = server.status_call(ASK)
        other_ask = {**ask, "question": "Which one?", "choices": []}
        failure = server.status_call(FAIL)
        worker_failure = {"status": "failed", "summary": "Codex exited before writing the final response file.",
                          "details": "returncode=1"}

        self.assertEqual(server.apply_status_calls(worker_failure, [failure, ask]), (worker_failure, ""))
        final, reason = server.apply_status_calls(answer, [ask, failure, other_ask])
        self.assertEqual((final["status"], reason), ("failed", "The entity light.porch does not exist."))
        self.assertEqual((final["question"], final["choices"]), ("", []))
        final, reason = server.apply_status_calls(answer, [ask, other_ask])
        self.assertEqual((final["status"], final["question"], final["choices"], reason),
                         ("needs_input", "Which one?", [], ""))
        self.assertEqual(final["summary"], "Plain answer text.")
        self.assertEqual(server.apply_status_calls(answer, []), (answer, ""))

    def test_a_plain_answer_is_the_summary_and_an_old_json_answer_still_reads(self):
        """Plain text, also with a JSON example in it, is the answer.

        A JSON answer of 0.3.2 or earlier keeps its fields, also inside one code fence.
        """
        with tempfile.TemporaryDirectory() as folder:
            final_file = Path(folder) / "final.json"
            plain = 'Done. The service call was:\n\n```json\n{"entity_id": "light.porch", "status": "on"}\n```'
            final_file.write_text(plain)
            self.assertEqual(server.parse_final(final_file, 0), {"status": "completed", "summary": plain, "details": ""})
            old = {"status": "needs_input", "summary": "Two automations match.", "question": "Remove one?",
                   "details": "", "choices": ["Yes", "No"]}
            final_file.write_text(json.dumps(old))
            self.assertEqual(server.parse_final(final_file, 0), old)
            final_file.write_text("```json\n" + json.dumps(old) + "\n```")
            self.assertEqual(server.parse_final(final_file, 0), old)

    def test_a_non_zero_exit_code_fails_the_answer_and_names_the_code(self):
        """A timeout or a broken exchange passes a non-zero code; the answer is then failed, also without a file."""
        with tempfile.TemporaryDirectory() as folder:
            final_file = Path(folder) / "final.json"
            missing = server.parse_final(final_file, -1)
            self.assertEqual(missing["status"], "failed")
            self.assertIn("returncode=-1", missing["summary"])
            final_file.write_text("Partly done.")
            self.assertEqual(server.parse_final(final_file, 1),
                             {"status": "failed", "summary": "Partly done.", "details": "returncode=1"})

    def test_an_example_of_the_old_answer_inside_a_plain_answer_stays_plain(self):
        """Only a whole answer is read as an old JSON answer, so an example of one does not set the status."""
        with tempfile.TemporaryDirectory() as folder:
            final_file = Path(folder) / "final.json"
            plain = 'Done. Example of the old answer:\n\n```json\n{"status": "failed", "summary": "example"}\n```'
            final_file.write_text(plain)
            self.assertEqual(server.parse_final(final_file, 0), {"status": "completed", "summary": plain, "details": ""})


class StatusRunTests(unittest.TestCase):
    """A whole exchange through run_task with a stand-in app server (messages written by hand, see the top)."""

    # The same isolated worker as the approval run tests: temporary storage, no Home Assistant, no notifications.
    setUp = approvals.ApprovalRunTests.setUp

    def process(self, items, final_text):
        """A stand-in app server that answers the handshake, then sends the items and the final answer of the turn."""
        output = queue.Queue()
        thread_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        self.sent = []

        def emit(message):
            """Queue one protocol line for the worker's reader thread."""
            output.put(json.dumps(message) + "\n")

        class Output:
            """The app server's stdout."""

            def readline(self):
                """Wait for the next line, or EOF."""
                return output.get(timeout=5)

        sent = self.sent

        class Input(approvals.Transport):
            """The app server's stdin: answers each request of the worker in turn."""

            def write(self, value):
                """Reply to initialize, thread/start and turn/start; after turn/start, play the turn."""
                result = super().write(value)
                message = json.loads(value)
                sent.append(message)
                method = message.get("method")
                if method == "initialize":
                    emit({"id": message["id"], "result": {}})
                elif method in {"thread/start", "thread/resume"}:
                    emit({"id": message["id"], "result": {"thread": {"id": thread_id},
                          "approvalPolicy": "on-request", "approvalsReviewer": "user"}})
                elif method == "turn/start":
                    emit({"id": message["id"], "result": {"turn": {"id": "native-turn"}}})
                    for item in items:
                        emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "native-turn",
                              "item": item}})
                    emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "native-turn",
                          "item": {"id": "final", "type": "agentMessage", "phase": "final_answer", "text": final_text}}})
                    emit({"method": "turn/completed", "params": {"threadId": thread_id,
                          "turn": {"id": "native-turn", "status": "completed"}}})
                return result

            def close(self):
                """End the reader at EOF."""
                output.put("")

        class Process:
            """The app server process: it exits when the worker closes its stdin."""

            stdin, stdout, stderr = Input(), Output(), approvals.io.StringIO()
            returncode = None

            def poll(self):
                """Running until the worker waits for it."""
                return self.returncode

            def wait(self, timeout=None):
                """A clean exit."""
                self.returncode = 0
                return 0

        return Process()

    def run_exchange(self, items, final_text, *, validation_errors=(), message="Tidy up my automations"):
        """Run one page exchange and return the stored task."""
        proc = self.process(items, final_text)
        self.stack.enter_context(patch.object(server.subprocess, "Popen", return_value=proc))
        self.stack.enter_context(patch.object(server, "assess_changes", return_value={
            "validation_errors": list(validation_errors), "recovery_files": [], "lovelace_results": [],
            "config_check": {"result": "invalid" if validation_errors else "skipped", "errors": "", "warnings": ""}}))
        server.run_task("task", message)
        return server.tasks["task"]

    def mcp_item(self, call):
        """The same call as the app server sends it (camel-case type)."""
        return {**call, "type": "mcpToolCall"}

    def test_completed_the_answer_is_stored_as_plain_text_and_the_turn_had_no_schema(self):
        """No tool call: the plain answer is the summary, and turn/start carried no output schema."""
        task = self.run_exchange([], "I renamed the automation to Porch light at sunset.")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["summary"], "I renamed the automation to Porch light at sunset.")
        self.assertEqual((task["question"], task["choices"], task["details"], task["error"]), ("", [], "", ""))
        turn_start = next(message for message in self.sent if message.get("method") == "turn/start")
        self.assertNotIn("outputSchema", turn_start["params"])

    def test_a_plain_message_with_no_tool_call_still_has_its_details_ready_and_its_own_words_as_input(self):
        """A plain question answered with no tool call: the worker set this message's details once, before the turn,
        so a call would get them, and the turn input is the user's words only."""
        recorded = []
        keep = server.ha_data.set_exchange_info
        self.stack.enter_context(patch.object(server.ha_data, "set_exchange_info",
                                              side_effect=lambda *a: (recorded.append(a), keep(*a))))
        task = self.run_exchange([], "4", message="How many lights are on in the kitchen?")
        self.assertEqual((task["status"], task["summary"], task["error"]), ("completed", "4", ""))
        self.assertEqual([item[0] for item in recorded], ["task"])
        self.assertIn("backups", recorded[0][1])
        turn_start = next(message for message in self.sent if message.get("method") == "turn/start")
        self.assertEqual(turn_start["params"]["input"][0],
                         {"type": "text", "text": "How many lights are on in the kitchen?"})

    def test_needs_input_comes_from_ask_owner_with_its_choices(self):
        """ask_owner sets the question and the choices; the answer text stays the summary."""
        text = "Two automations turn on the porch light. Remove the duplicate automation? Go ahead, or Keep both."
        task = self.run_exchange([self.mcp_item(ASK)], text)
        self.assertEqual(task["status"], "waiting_for_input")
        self.assertEqual(task["summary"], text)
        self.assertEqual(task["question"], "Remove the duplicate automation?")
        self.assertEqual(task["choices"], ["Go ahead", "Keep both"])

    def test_failed_comes_from_report_failure_with_its_reason_as_the_error(self):
        """report_failure fails the task, and its reason is the task's error."""
        text = "I could not do this: the entity light.porch does not exist."
        task = self.run_exchange([self.mcp_item(FAIL)], text)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["summary"], text)
        self.assertEqual(task["error"], "The entity light.porch does not exist.")

    def test_an_old_json_answer_still_gives_its_status_and_choices(self):
        """A thread from 0.3.2 or earlier can still answer with the JSON object."""
        old = {"status": "needs_input", "summary": "Two automations match.", "question": "Remove one?",
               "details": "", "choices": ["Yes", "No"]}
        task = self.run_exchange([], json.dumps(old))
        self.assertEqual((task["status"], task["summary"], task["question"], task["choices"]),
                         ("waiting_for_input", "Two automations match.", "Remove one?", ["Yes", "No"]))

    def test_a_failed_configuration_check_wins_over_a_question(self):
        """F3.3: the worker's own failure comes first, so the owner is not asked a question over a broken file.

        The same holds for a question in an old JSON answer, which version 0.3.2 and earlier kept.
        """
        old_question = json.dumps({"status": "needs_input", "summary": "Edited.", "question": "Remove one?",
                                   "details": "", "choices": ["Yes"]})
        for items, text in (([self.mcp_item(ASK)], "Remove the duplicate automation?"), ([], old_question)):
            with self.subTest(text=text):
                server.tasks["task"].update(status="queued", question="", choices=[])
                task = self.run_exchange(items, text, validation_errors=["automations.yaml: invalid YAML"])
                self.assertEqual(task["status"], "failed")
                self.assertEqual((task["question"], task["choices"]), ("", []))
                self.assertIn("automations.yaml", task["error"])

    def test_a_failed_turn_wins_over_a_question_in_an_old_json_answer(self):
        """A turn that ends as failed fails the task, also when an old JSON answer asks a question."""
        old_question = {"status": "needs_input", "summary": "Edited.", "question": "Remove one?",
                        "details": "", "choices": ["Yes"]}
        proc = attachments.StubAppServer("019fc242-910a-7c92-a17d-54c014e19fc4", old_question,
                                         turn_error="Model connection lost")
        self.stack.enter_context(patch.object(server.subprocess, "Popen", return_value=proc))
        self.stack.enter_context(patch.object(server, "assess_changes", return_value={
            "validation_errors": [], "recovery_files": [], "lovelace_results": [],
            "config_check": {"result": "skipped", "errors": "", "warnings": ""}}))
        server.run_task("task", "Tidy up my automations")
        task = server.tasks["task"]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["question"], "")
        self.assertIn("Model connection lost", task["details"])


class OwnerNoticeTests(unittest.TestCase):
    """A turn sends a Home Assistant notification only when it needs the owner.

    A plain answer to the owner's own question sends nothing: the owner reads it in the chat. A failure sends
    nothing either; it shows in the chat. A question and a permission request still send one.
    """

    # The same stand-in exchange as StatusRunTests, without running its tests a second time.
    setUp = StatusRunTests.setUp
    process = StatusRunTests.process
    run_exchange = StatusRunTests.run_exchange
    mcp_item = StatusRunTests.mcp_item

    def titles(self):
        """The titles of the notifications sent so far (server.notify is a stand-in from the setUp)."""
        return [call.args[0] for call in server.notify.call_args_list]

    def test_a_plain_answer_sends_no_notification(self):
        """No tool call: the turn is completed and nobody is notified."""
        task = self.run_exchange([], "The porch light turns on 10 minutes after sunset.")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.titles(), [])

    def test_a_question_sends_one_notification(self):
        """ask_owner: one notification with the question."""
        task = self.run_exchange([self.mcp_item(ASK)], "Remove the duplicate automation? Go ahead, or Keep both.")
        self.assertEqual(task["status"], "waiting_for_input")
        self.assertEqual(self.titles(), ["Codex needs input"])
        self.assertIn("Remove the duplicate automation?", server.notify.call_args.args[1])

    def test_a_failure_sends_no_notification(self):
        """report_failure: the failure shows in the chat, and nobody is notified."""
        task = self.run_exchange([self.mcp_item(FAIL)], "I could not do this: the entity light.porch does not exist.")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.titles(), [])

    def test_a_permission_request_sends_one_notification(self):
        """An approval prompt is notified on its own path, which a completed turn does not change."""
        withdrawn = threading.Event()
        server.notify_permission("task", {"kind": "command", "reason": "List the configuration folder"}, withdrawn)
        self.assertEqual(self.titles(), ["Codex needs permission"])

    def test_a_canceled_task_sends_no_notification(self):
        """The owner cancels a chat task on purpose: the result event goes out, a notification does not."""
        events = []
        server.fire_ha_event.side_effect = lambda name, data: events.append((name, data)) or (True, "")
        accepted, _process, error = server.request_task_cancellation("task")
        self.assertTrue(accepted, error)
        self.assertTrue(server.publish_cancelled_task_outcome("task"))
        self.assertEqual([(name, data["status"]) for name, data in events], [("codex_cli_task_result", "cancelled")])
        self.assertEqual(self.titles(), [])


if __name__ == "__main__":
    unittest.main()
