"""Tool requests name their chat by the thread id that Codex sends; the per-exchange keys are gone.

The real reader of the thread id: Codex writes it into `params._meta.threadId` of each `tools/call` request
(codex-rs/core/src/mcp_tool_call.rs at rust-v0.166.0). The tool program passes it on as JSON, and the worker
looks it up in tool_threads.CHATS. The model writes the call's arguments, not `_meta`.

Corpus families (the parser-or-guard skill): size and empty, case and form, invisible characters, nesting
(a list or an object in the place of the text), time of check (after end, after a cancel, a new exchange).
n/a: quotes, comments, line ends, multiline, wrappers, separators, git shapes, template readers - the id is
compared whole against ids that the worker itself bound, and no shell, YAML or template reads it.
Add a case to CORPUS below.
"""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
import approvals
import github_access
from ha_call import RELAY_TASK, HomeAssistantCall, RelayCall
import ha_call_mcp
from ha_data import DataError, HomeAssistantData
import ha_data_mcp
import tool_threads
from verification import Verification
import verification_mcp

CHAT = "1a1b1c1d-2e2f-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-aaaaaaaaaaaa"
CHILD = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"

# (id, value, expected: "allow" or "refuse", family)
CORPUS = [
    ("T01", CHAT, "allow", "form"),
    (
        "T02",
        "00000000-0000-0000-0000-000000000000",
        "refuse",
        "form",
    ),  # well formed, not bound
    ("T03", None, "refuse", "empty"),
    ("T04", "", "refuse", "empty"),
    ("T05", " ", "refuse", "empty"),
    ("T06", CHAT.upper(), "refuse", "case"),
    ("T07", CHAT + "\n", "refuse", "form"),
    ("T08", " " + CHAT, "refuse", "form"),
    ("T09", CHAT.replace("-", ""), "refuse", "form"),
    ("T10", "{" + CHAT + "}", "refuse", "form"),
    ("T11", CHAT[:-1] + "٥", "refuse", "invisible"),  # an Arabic-Indic digit five
    ("T12", CHAT[:8] + "​" + CHAT[8:], "refuse", "invisible"),
    ("T13", CHAT + "\x00", "refuse", "invisible"),
    ("T14", [CHAT], "refuse", "nesting"),
    ("T15", {"threadId": CHAT}, "refuse", "nesting"),
    ("T16", 11111111, "refuse", "nesting"),
    ("T17", CHAT * 2, "refuse", "size"),
    ("T18", "x" * 100000, "refuse", "size"),
    ("T19", CHAT + "," + OTHER, "refuse", "separators"),
    # Reviewer cases of the second read (CX-SPEED.c3.kc addendum).
    ("T20", CHAT + "\r", "refuse", "form"),
    ("T21", CHAT.encode(), "refuse", "nesting"),
    ("T22", True, "refuse", "nesting"),
    ("T23", CHAT[:8] + "\u2010" + CHAT[9:], "refuse", "invisible"),  # a Unicode hyphen
    ("T24", CHAT[:2] + "A" + CHAT[3:], "refuse", "case"),  # one upper-case letter
    ("T25", float("nan"), "refuse", "nesting"),
    ("T26", "\ufeff" + CHAT, "refuse", "invisible"),  # a byte order mark
    ("T27", CHAT[:-1] + "\uff15", "refuse", "invisible"),  # a full-width digit five
    ("T28", "urn:uuid:" + CHAT, "refuse", "form"),
    ("T29", "\u200f" + CHAT, "refuse", "invisible"),  # a right-to-left mark
]


class ToolThreadsCorpus(unittest.TestCase):
    """The whole corpus against the map, with CHAT bound and nothing else."""

    def test_corpus(self):
        chats = tool_threads.ChatThreads()
        chats.bind("chat", CHAT)
        wrong = [
            case
            for case, value, expected, _family in CORPUS
            if (chats.task_for(value) == "chat") != (expected == "allow")
        ]
        self.assertEqual(wrong, [], "corpus cases with the wrong answer")

    def test_only_the_meta_of_the_call_counts(self):
        """A threadId in the arguments is the model's text; a _meta that is not an object gives no id."""
        cases = [
            ({"name": "verify", "arguments": {}, "_meta": {"threadId": CHAT}}, CHAT),
            ({"name": "verify", "arguments": {"threadId": CHAT}}, ""),
            ({"name": "verify", "arguments": {"_meta": {"threadId": CHAT}}}, ""),
            ({"name": "verify", "arguments": {}, "_meta": [{"threadId": CHAT}]}, ""),
            ({"name": "verify", "arguments": {}, "_meta": "threadId=" + CHAT}, ""),
            ({"name": "verify", "arguments": {}, "_meta": {"threadId": [CHAT]}}, ""),
            ({"name": "verify", "arguments": {}, "_meta": {"thread_id": CHAT}}, ""),
            ([CHAT], ""),
            (None, ""),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(tool_threads.thread_id_from_call(params), expected)

    def test_release_removes_the_chat_and_its_sub_agents_only(self):
        chats = tool_threads.ChatThreads()
        chats.bind("a", CHAT)
        chats.bind("a", CHILD)
        chats.bind("b", OTHER)
        chats.release("a")
        self.assertEqual(
            [chats.task_for(CHAT), chats.task_for(CHILD), chats.task_for(OTHER)],
            [None, None, "b"],
        )

    def test_only_a_thread_id_form_can_be_bound(self):
        chats = tool_threads.ChatThreads()
        for case, value, _expected, _family in CORPUS:
            if case not in {"T01", "T02"}:
                with self.subTest(case=case):
                    self.assertFalse(chats.bind("a", value))
        self.assertEqual(chats.threads_of("a"), [])

    def test_a_later_exchange_takes_the_thread_only_after_the_first_ends(self):
        chats = tool_threads.ChatThreads()
        self.assertTrue(chats.bind("a", CHAT))
        self.assertFalse(chats.bind("b", CHAT))
        self.assertEqual(chats.task_for(CHAT), "a")
        chats.release("a")
        self.assertTrue(chats.bind("b", CHAT))
        self.assertEqual(chats.task_for(CHAT), "b")


class BridgesSendTheThreadId(unittest.TestCase):
    """Each tool program sends the id from _meta, never one from the arguments and never a key."""

    def call(self, module, function, name):
        sent = {}

        def capture(*args):
            sent["args"] = args
            return {"status": "observed"}

        request = {
            "method": "tools/call",
            "id": 1,
            "params": {"name": name, "arguments": {}, "_meta": {"threadId": CHAT}},
        }
        with patch.object(module, function, capture):
            module.respond(request)
        return sent["args"]

    def test_verification_and_data_and_call_bridges(self):
        self.assertEqual(self.call(verification_mcp, "verify", "verify")[-1], CHAT)
        self.assertEqual(self.call(ha_data_mcp, "fetch", "history")[-1], CHAT)
        self.assertEqual(self.call(ha_call_mcp, "call", "call")[-1], CHAT)

    def test_the_call_bridge_sends_no_key_and_uses_the_socket_it_is_given(self):
        sent = []

        class Socket:
            def __init__(self, *args):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, value):
                pass

            def connect(self, path):
                sent.append(path)

            def sendall(self, data):
                sent.append(data)

            def makefile(self, mode):
                import io

                return io.BytesIO(b'{"status": "refused"}\n')

        with patch.object(ha_call_mcp.socket, "socket", Socket):
            with patch.dict(
                "os.environ",
                {
                    "HA_CALL_SOCKET": "/synthetic/app.sock",
                    "HA_CALL_CAPABILITY": "old-key",
                },
            ):
                ha_call_mcp.call({}, CHAT)
            with patch.dict("os.environ", {}) as environment:
                environment.pop("HA_CALL_SOCKET", None)
                ha_call_mcp.call({}, CHAT)
        self.assertEqual(sent[0], "/synthetic/app.sock")
        self.assertEqual(sent[2], "/data/ha-call.sock")
        for request in (sent[1], sent[3]):
            self.assertIn(('"thread_id": "%s"' % CHAT).encode(), request)
            self.assertNotIn(b"capability", request)


class TwoChats(unittest.TestCase):
    """Chat a, which a person attends, and chat b, a scheduled run, both with a running exchange."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = patch.dict(server.tasks, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key, value in {
            "DATA_ROOT": self.root,
            "TASK_STATE_FILE": self.root / "index.json",
        }.items():
            patcher = patch.object(server, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(
            server,
            "read_options",
            return_value={
                **server.DEFAULT_OPTIONS,
                "task_root": str(self.root / "tasks"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = HomeAssistantData(server, self.root / "ha-data")
        self.checks = Verification(server)
        self.calls = HomeAssistantCall(server)
        for task_id, thread_id in (("a", CHAT), ("b", OTHER)):
            server.tasks[task_id] = {
                "task_id": task_id,
                "status": "running",
                "current_turn_id": "turn-" + task_id,
            }
            tool_threads.CHATS.bind(task_id, thread_id)
            self.addCleanup(tool_threads.CHATS.release, task_id)
            self.data.begin(task_id)
            self.data.set_exchange_info(task_id, {"backup_folder": "folder-" + task_id})
            self.checks.begin(task_id)
        self.calls.begin("a")  # a person attends chat a; chat b is a scheduled run

    def info(self, thread_id):
        return self.data.dispatch(
            {"operation": "exchange_info", "thread_id": thread_id}
        )


class ModulesFindTheChat(TwoChats):
    """The three checks of the design, section 6, in each module that answers a tool request."""

    def info(self, thread_id):
        return self.data.dispatch(
            {"operation": "exchange_info", "thread_id": thread_id}
        )

    def refused_everywhere(self, thread_id):
        """Return the modules that did not refuse a request with this thread id."""
        accepted = []
        try:
            self.info(thread_id)
            accepted.append("ha_data")
        except DataError:
            pass
        try:
            self.checks.dispatch({"operation": "unsupported", "thread_id": thread_id})
            accepted.append("verification")
        except ValueError:
            pass
        if self.calls.task_for({"thread_id": thread_id, "arguments": {}}) is not None:
            accepted.append("ha_call")
        if github_access.find_task_by_thread(self.data, thread_id) is not None:
            accepted.append("github")
        return accepted

    def test_each_chat_gets_its_own_exchange(self):
        self.assertEqual(self.info(CHAT)["backup_folder"], "folder-a")
        self.assertEqual(self.info(OTHER)["backup_folder"], "folder-b")
        self.assertEqual(github_access.find_task_by_thread(self.data, OTHER), "b")

    def test_corpus_refusals_hold_in_every_module(self):
        for case, value, expected, _family in CORPUS:
            if expected == "refuse":
                with self.subTest(case=case):
                    self.assertEqual(self.refused_everywhere(value), [])

    def test_no_running_exchange_is_refused(self):
        for outcome in ("ended", "canceled", "completed", "released"):
            with self.subTest(outcome=outcome):
                self.setUp()
                if outcome == "ended":
                    self.data.end("a")
                    self.checks.end("a")
                    self.calls.end("a")
                elif outcome == "canceled":
                    server.tasks["a"]["cancellation_requested"] = True
                elif outcome == "completed":
                    server.tasks["a"]["status"] = "completed"
                else:
                    tool_threads.CHATS.release("a")
                self.assertEqual(self.refused_everywhere(CHAT), [])
                self.assertEqual(self.info(OTHER)["backup_folder"], "folder-b")

    def test_a_query_of_one_chat_does_not_count_for_the_other(self):
        self.data.queries["a"] = 5
        self.assertEqual(self.data.queries["b"], 0)
        self.assertEqual(self.data.exchange_for(OTHER)[0], "b")

    def test_the_call_tool_needs_a_running_exchange_of_that_chat(self):
        self.assertEqual(self.calls.task_for({"thread_id": CHAT, "arguments": {}}), "a")
        self.assertIsNone(self.calls.task_for({"thread_id": OTHER, "arguments": {}}))
        answer = self.calls.dispatch(
            {"thread_id": OTHER, "arguments": {"service": "light.turn_off"}}
        )
        self.assertEqual(answer["status"], "unavailable")

    def test_the_socket_not_a_key_says_who_asks(self):
        # The chat socket answers only a running chat's thread; nothing on it is ever the relay's.
        for payload in (
            {"arguments": {}},
            {"capability": "invented-key", "arguments": {}},
            {"thread_id": RELAY_TASK, "arguments": {}},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(self.calls.task_for(payload))
        self.assertEqual(
            self.calls.task_for(
                {"capability": "invented-key", "thread_id": CHAT, "arguments": {}}
            ),
            "a",
        )
        # The app's socket answers while the relay runs, and never names a chat, even with a chat's thread id.
        running = [True]
        relay = RelayCall(server, relay_running=lambda: running[0])
        self.assertEqual(
            relay.task_for({"thread_id": CHAT, "arguments": {}}), RELAY_TASK
        )
        running[0] = False
        self.assertIsNone(relay.task_for({"thread_id": CHAT, "arguments": {}}))

    def test_a_scheduling_request_needs_the_thread_id(self):
        with patch.object(server, "ha_data", self.data):
            with self.assertRaises(ValueError):
                server.schedule_service.dispatch(
                    {
                        "operation": "list_schedules",
                        "arguments": {},
                        "capability": self.data.capabilities["a"],
                    }
                )
            with self.assertRaisesRegex(ValueError, "ended"):
                server.schedule_service.dispatch(
                    {
                        "operation": "list_schedules",
                        "arguments": {},
                        "thread_id": "00000000-0000-0000-0000-000000000000",
                    }
                )


class TheChatHoldsNoKey(unittest.TestCase):
    """No tool key in the chat's Codex command line or in remote control's (D-CX-KEYS)."""

    def test_the_chat_command_line(self):
        text = " ".join(server.build_app_server_args())
        self.assertNotIn("CAPABILITY", text)
        self.assertNotIn("shell_environment_policy", text)

    def test_remote_control_holds_no_tool_key(self):
        with patch.object(server, "read_options", return_value={}):
            text = " ".join(server.relay_tool_args())
        # The exclude stays for the hass-mcp token (HA_MCP_CAPABILITY), which only that server gets.
        self.assertIn("shell_environment_policy.exclude", text)
        self.assertIn(
            'mcp_servers.home_assistant_call.env_vars=["HA_CALL_SOCKET"]', text
        )
        self.assertNotIn("HA_CALL_CAPABILITY", text)
        self.assertNotIn("HA_RELAY_CAPABILITY", text)

    def test_run_task_puts_no_key_in_the_environment(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        # The exchange's stages (preflight, launch, run_task) sit between these two lines of server.py.
        body = source[
            source.index("class Cancelled(") : source.index("def _run_background_task(")
        ]
        self.assertNotIn("_CAPABILITY", body)
        self.assertIn("env=codex_env(),", body)


# The collab tools in which the model writes the receiver (CollabAgentTool, app-server-protocol at rust-v0.166.0).
MODEL_TARGETED_TOOLS = (
    "sendInput",
    "wait",
    "closeAgent",
    "resumeAgent",
    "sendMessage",
    "followupTask",
    "interruptAgent",
    "listAgents",
)


def collab_item(tool, receivers, sender=CHAT):
    """Return a collabAgentToolCall item as the app server sends it."""
    return {
        "id": "1",
        "type": "collabAgentToolCall",
        "tool": tool,
        "status": "completed",
        "senderThreadId": sender,
        "receiverThreadIds": receivers,
    }


class SubAgentThreads(unittest.TestCase):
    """A sub-agent's tool calls name its own thread; only the parent's spawn item links it to the chat."""

    def session(self, events):
        session = approvals.AppServerSession.__new__(approvals.AppServerSession)
        session.thread_id = CHAT
        session.on_event = events.append
        return session

    def test_the_spawn_item_reports_the_sub_agent_threads(self):
        events = []
        self.session(events)._item(
            "item/completed", collab_item("spawnAgent", [CHILD, 5])
        )
        self.assertEqual(
            events, [{"type": "agent.threads", "parent": CHAT, "threads": [CHILD]}]
        )

    def test_r10_another_thread_spawn_is_not_reported(self):
        events = []
        self.session(events)._item(
            "item/completed", collab_item("spawnAgent", [CHILD], sender=OTHER)
        )
        self.assertEqual(events, [])

    def test_r1_r2_a_model_written_receiver_is_not_reported(self):
        for tool in MODEL_TARGETED_TOOLS:
            for method in ("item/started", "item/completed"):
                with self.subTest(tool=tool, method=method):
                    events = []
                    self.session(events)._item(method, collab_item(tool, [OTHER]))
                    self.assertEqual(events, [])

    def test_a_spawn_counts_only_when_it_completes(self):
        events = []
        session = self.session(events)
        session._item("item/started", collab_item("spawnAgent", [CHILD]))
        for item in (
            {**collab_item("spawnAgent", [CHILD]), "tool": None},
            {
                key: value
                for key, value in collab_item("spawnAgent", [CHILD]).items()
                if key != "tool"
            },
            collab_item("SpawnAgent", [CHILD]),
            collab_item(["spawnAgent"], [CHILD]),
        ):
            session._item("item/completed", item)
        self.assertEqual(events, [])


class TheFirstChatKeepsItsThread(unittest.TestCase):
    """ChatThreads.bind refuses a thread that another task holds; the same task may bind it again."""

    def test_bind(self):
        threads = tool_threads.ChatThreads()
        self.assertTrue(threads.bind("b", OTHER))
        self.assertTrue(threads.bind("b", OTHER))
        self.assertFalse(threads.bind("a", OTHER))
        self.assertEqual(threads.task_for(OTHER), "b")
        threads.release("a")
        self.assertEqual(threads.task_for(OTHER), "b")
        threads.release("b")
        self.assertTrue(threads.bind("a", OTHER))
        self.assertEqual(threads.task_for(OTHER), "a")


class TheWorkerBindsAndReleases(TwoChats):
    """The wiring in server.py: the event function of create_approval_session, run_task and its finally."""

    def setUp(self):
        super().setUp()
        for name, value in (
            ("running_processes", {}),
            ("write_task_log", lambda *_args: None),
        ):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def exchange(self, task_id, thread_id):
        """Return the app-server session of the task's exchange, with the thread that Codex reported."""
        writer = io.StringIO()
        proc = type("Process", (), {"stdin": writer, "poll": lambda _: None})()
        server.running_processes[task_id] = proc
        holder = {}
        session = server.create_approval_session(
            task_id,
            proc,
            "Check the porch light",
            self.root / "final",
            None,
            holder,
            server.DEFAULT_OPTIONS,
        )
        session.on_event({"type": "thread.started", "thread_id": thread_id})
        session.thread_id = thread_id
        return session, holder

    def test_the_reported_thread_is_bound(self):
        tool_threads.CHATS.release("a")
        _session, holder = self.exchange("a", CHAT)
        self.assertEqual(holder["session_id"], CHAT)
        self.assertEqual(tool_threads.CHATS.task_for(CHAT), "a")

    def test_r9_the_chat_binds_its_spawned_sub_agent(self):
        session, _holder = self.exchange("a", CHAT)
        session._item("item/completed", collab_item("spawnAgent", [CHILD]))
        self.assertEqual(tool_threads.CHATS.task_for(CHILD), "a")
        self.assertEqual(self.info(CHILD)["backup_folder"], "folder-a")

    def test_agent_threads_of_another_parent_bind_nothing(self):
        session, _holder = self.exchange("a", CHAT)
        session.on_event({"type": "agent.threads", "parent": OTHER, "threads": [CHILD]})
        self.assertIsNone(tool_threads.CHATS.task_for(CHILD))

    def test_r3_to_r6_a_model_written_target_cannot_take_another_chat(self):
        for tool in ("sendInput", "wait"):
            with self.subTest(tool=tool):
                self.setUp()
                session, _holder = self.exchange("a", CHAT)
                session._item("item/started", collab_item(tool, [OTHER]))
                session._item("item/completed", collab_item(tool, [OTHER]))
                # Even an agent.threads event of this chat that names b's thread leaves it with b.
                session.on_event(
                    {"type": "agent.threads", "parent": CHAT, "threads": [OTHER]}
                )
                self.assertEqual(tool_threads.CHATS.task_for(OTHER), "b")  # R3
                self.assertIsNone(
                    self.calls.task_for({"thread_id": OTHER, "arguments": {}})
                )  # R4
                self.assertEqual(self.info(OTHER)["backup_folder"], "folder-b")  # R6
                tool_threads.CHATS.release("a")
                self.assertEqual(self.info(OTHER)["backup_folder"], "folder-b")  # R5

    def test_r7_a_stray_key_field_never_names_a_chat(self):
        with self.assertRaises(ValueError):
            self.checks.dispatch(
                {
                    "operation": "unsupported",
                    "capability": self.checks.capabilities["a"],
                }
            )
        self.checks.dispatch(
            {
                "operation": "unsupported",
                "thread_id": CHAT,
                "capability": self.checks.capabilities["b"],
            }
        )
        self.assertFalse(server.tasks["b"].get("verification"))

    def test_r8_a_key_field_neither_opens_nor_closes_a_chat(self):
        # D-CX-KEYS: the chat socket reads no key; the thread id alone names the chat.
        self.assertEqual(
            self.calls.task_for(
                {"capability": None, "thread_id": CHAT, "arguments": {}}
            ),
            "a",
        )
        self.assertIsNone(
            self.calls.task_for(
                {"capability": None, "thread_id": "unbound", "arguments": {}}
            )
        )

    def test_a_follow_up_binds_its_thread_at_the_start(self):
        follow_up = "cccccccc-dddd-4eee-8fff-000000000000"
        server.tasks["c"] = {
            "task_id": "c",
            "status": "queued",
            "current_turn_id": "turn-c",
            "session_id": follow_up,
        }
        seen = []

        def stop_here(task_id, _turn_id):
            seen.append(tool_threads.CHATS.task_for(follow_up))
            raise StopRun

        with (
            patch.object(server, "start_activity", side_effect=stop_here),
            patch.object(server, "verification", self.checks),
            patch.object(server, "ha_data", self.data),
        ):
            with self.assertRaises(StopRun):
                server.run_task(
                    "c", "And the back door?", follow_up, "And the back door?"
                )
        tool_threads.CHATS.release("c")
        self.assertEqual(seen, ["c"])

    def test_the_end_of_the_exchange_releases_every_thread(self):
        def run(task_id, *_args):
            tool_threads.CHATS.bind(task_id, CHAT)
            tool_threads.CHATS.bind(task_id, CHILD)

        tool_threads.CHATS.release("a")
        quiet = {
            name: patch.object(server, name, lambda *_args, **_kwargs: None)
            for name in (
                "record_diagnostic",
                "finish_activity",
                "remove_run_artifacts",
                "start_next_queued",
                "cleanup_backups",
            )
        }
        with (
            patch.object(server, "run_task", side_effect=run),
            patch.object(server, "verification", self.checks),
            patch.object(server, "ha_data", self.data),
        ):
            for patcher in quiet.values():
                patcher.start()
            try:
                server._run_background_task("a", "Check the porch light", None, None)
            finally:
                for patcher in quiet.values():
                    patcher.stop()
        self.assertEqual(tool_threads.CHATS.threads_of("a"), [])
        self.assertEqual(tool_threads.CHATS.task_for(OTHER), "b")


class TheEndAndTheLog(TwoChats):
    """Addendum A2 and A3 of the closer: a late event at the end of an exchange, and a refused bind."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(server, "write_task_log", lambda *_args: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_event_while_the_session_closes_leaves_no_thread(self):
        class LateSession:
            def close(self):
                server.bind_chat_thread(
                    "a", CHILD
                )  # one more agent.threads event from the reader

        quiet = [
            patch.object(server, name, lambda *_args, **_kwargs: None)
            for name in (
                "record_diagnostic",
                "finish_activity",
                "remove_run_artifacts",
                "start_next_queued",
                "cleanup_backups",
            )
        ]
        with (
            patch.object(server, "run_task", lambda *_args: None),
            patch.dict(server.approval_sessions, {"a": LateSession()}),
            patch.object(server, "verification", self.checks),
            patch.object(server, "ha_data", self.data),
        ):
            for patcher in quiet:
                patcher.start()
            try:
                server._run_background_task("a", "Check the porch light", None, None)
            finally:
                for patcher in quiet:
                    patcher.stop()
        self.assertEqual(tool_threads.CHATS.threads_of("a"), [])

    def test_a_refused_bind_is_logged(self):
        for value in (OTHER, CHAT.upper()):
            with self.subTest(value=value):
                out = io.StringIO()
                with redirect_stdout(out):
                    server.bind_chat_thread("a", value)
                self.assertIn("not bound to task a", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            server.bind_chat_thread("a", CHAT)
        self.assertEqual(out.getvalue(), "")


class StopRun(Exception):
    """Ends run_task in a test at the point that the test checks."""


if __name__ == "__main__":
    unittest.main()
