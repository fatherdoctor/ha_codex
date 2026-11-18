"""The home_assistant_call tool: what reaches Home Assistant, what never does, and who must ask first.

A fake Home Assistant records each request. The tests assert the exact request it received,
not only the tool's answer (plan CX-SIMPLE, section 6).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from test_server import server  # First: it puts the worker's folder on sys.path.

# isort: split
import ha_call
import ha_call_mcp
from ha_call import HomeAssistantCall

WORKER_TOKEN = "invented-supervisor-token"


class FakeHomeAssistant:
    """Stands in for requests.post in ha_call: records each request and answers with a fixed status and body.

    No socket is opened, so the test plugin of Home Assistant, which blocks the network, lets it run.
    """

    def __init__(self, status=200, answer=None):
        self.requests = []
        self.status = status
        self.answer = answer if answer is not None else []

    def post(self, url, **options):
        """Record the request as Home Assistant would receive it, and answer it."""
        self.requests.append({"method": "POST", "url": url, **options})
        return SimpleNamespace(status_code=self.status, json=lambda: self.answer, text=json.dumps(self.answer))


def call_worker(status="running"):
    """A worker with one chat, t1, in the given status."""
    return SimpleNamespace(lock=threading.RLock(), tasks={"t1": {"task_id": "t1", "status": status}},
                           task_cancellation_requested=lambda task_id: False, ha_token=lambda: WORKER_TOKEN)


class CallTestCase(unittest.TestCase):
    """A call service with a fake Home Assistant in place of requests.post."""

    answer: ClassVar[object] = [{"entity_id": "light.kitchen_ceiling", "state": "off"}]
    status = 200

    def setUp(self):
        self.fake = FakeHomeAssistant(self.status, self.answer)
        patcher = patch.object(ha_call.requests, "post", self.fake.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = call_worker()
        self.calls = HomeAssistantCall(self.worker)
        self.key = self.calls.begin("t1")

    def call(self, arguments, key=None):
        return self.calls.dispatch({"capability": self.key if key is None else key, "arguments": arguments})

    def sent(self):
        """Return (path, body) of each request Home Assistant received."""
        return [(item["url"].removeprefix("http://supervisor/core"), item["json"]) for item in self.fake.requests]


class ExactRequestTests(CallTestCase):

    def test_light_turn_off_sends_that_entity_and_nothing_else(self):
        result = self.call({"service": "light.turn_off", "target": {"entity_id": "light.kitchen_ceiling"}})
        self.assertEqual(self.fake.requests, [{"method": "POST",
                                               "url": "http://supervisor/core/api/services/light/turn_off",
                                               "json": {"entity_id": "light.kitchen_ceiling"},
                                               "headers": {"Authorization": f"Bearer {WORKER_TOKEN}"},
                                               "timeout": ha_call.CALL_TIMEOUT_SECONDS,
                                               "allow_redirects": False}])
        self.assertEqual(result, {"status": "ok", "service": "light.turn_off", "changed": ["light.kitchen_ceiling"]})

    def test_notify_keeps_its_target_and_its_full_message(self):
        message = "The garage door is still open. " * 200
        self.call({"service": "notify.mobile_app_example_phone", "data": {"title": "Garage", "message": message}})
        self.assertEqual(self.sent(), [("/api/services/notify/mobile_app_example_phone",
                                        {"title": "Garage", "message": message})])

    def test_data_and_target_go_side_by_side(self):
        self.call({"service": "light.turn_on", "target": {"area_id": "kitchen", "entity_id": ["light.a", "light.b"]},
                   "data": {"brightness_pct": 10}})
        self.assertEqual(self.sent(), [("/api/services/light/turn_on",
                                        {"brightness_pct": 10, "area_id": "kitchen",
                                         "entity_id": ["light.a", "light.b"]})])

    def test_a_reload_is_a_service_too(self):
        self.call({"service": "automation.reload"})
        self.assertEqual(self.sent(), [("/api/services/automation/reload", {})])

    def test_any_domain_is_sent_once_the_person_allows_it(self):
        """The tool refuses no service by name: Codex's question to the person is the gate."""
        for service in ("hassio.addon_restart", "codex_cli.run_task", "update.install", "lock.unlock"):
            with self.subTest(service=service):
                self.assertEqual(self.call({"service": service})["status"], "ok")
        self.assertEqual([path for path, _ in self.sent()],
                         ["/api/services/hassio/addon_restart", "/api/services/codex_cli/run_task",
                          "/api/services/update/install", "/api/services/lock/unlock"])


class NothingSentTests(CallTestCase):

    def assert_nothing_sent(self, result, status="unavailable"):
        self.assertEqual(result["status"], status)
        self.assertEqual(self.fake.requests, [])

    def test_no_key_a_wrong_key_or_a_read_tools_key_sends_nothing(self):
        # A read tool's key is issued by another service, so the call service has never seen it.
        for key in ("", "wrong", "invented-verification-key", None):
            with self.subTest(key=key):
                payload = {"arguments": {"service": "light.turn_off"}}
                if key is not None:
                    payload["capability"] = key
                self.assert_nothing_sent(self.calls.dispatch(payload))

    def test_a_key_of_an_ended_turn_sends_nothing(self):
        self.calls.end("t1")
        self.assert_nothing_sent(self.call({"service": "light.turn_off"}))

    def test_a_new_turn_replaces_the_old_key(self):
        old = self.key
        self.calls.begin("t1")
        self.assert_nothing_sent(self.call({"service": "light.turn_off"}, key=old))

    def test_a_chat_that_is_not_running_sends_nothing(self):
        for status in ("completed", "failed", "queued"):
            with self.subTest(status=status):
                self.worker.tasks["t1"]["status"] = status
                self.assert_nothing_sent(self.call({"service": "light.turn_off"}))

    def test_a_cancelled_chat_sends_nothing(self):
        self.worker.task_cancellation_requested = lambda task_id: True
        self.assert_nothing_sent(self.call({"service": "light.turn_off"}))

    def test_malformed_requests_send_nothing(self):
        cases = [
            {"service": "light"}, {"service": "Light.Turn_Off"}, {"service": "light.turn_off/../x"},
            {"service": "light.turn_off", "target": "light.kitchen_ceiling"},
            {"service": "light.turn_off", "target": {"entity": "light.kitchen_ceiling"}},
            {"service": "light.turn_off", "target": {"entity_id": []}},
            {"service": "light.turn_off", "target": {"entity_id": [1]}},
            {"service": "light.turn_off", "data": ["x"]},
            {"service": "light.turn_off", "data": {"entity_id": "light.kitchen_ceiling"}},
            {"service": "light.turn_off", "extra": 1},
            "light.turn_off",
        ]
        # Spellings that are not domain.service: none may reach Home Assistant as some other path.
        cases += [{"service": service} for service in (
            "light.turn_off\n", " light.turn_off", "LIGHT.turn_off", "light .turn_off", "light..turn_off",
            "\u04bbassio.addon_stop", "light.turn_off?x=1", "light/turn_off", "../light.turn_off", "light.turn_off#",
            "light.\u0440un", "light.turn_off.extra", "light.", ".turn_off", "", "x" * 201)]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assert_nothing_sent(self.call(arguments), status="refused")


class FailedCallTests(CallTestCase):
    status = 400
    answer: ClassVar[object] = {"message": "invented validation error"}

    def test_a_failed_call_is_reported_and_not_sent_again(self):
        result = self.call({"service": "light.turn_on", "data": {"brightness_pct": 500}})
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 400)


class NoAnswerTests(CallTestCase):

    def test_no_answer_is_reported_and_not_sent_again(self):
        attempts = []

        def unreachable(url, **options):
            attempts.append(url)
            raise ha_call.requests.ConnectionError("invented")

        with patch.object(ha_call.requests, "post", unreachable):
            result = self.call({"service": "light.turn_off"})
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result, {"status": "failed", "message": "No answer from Home Assistant (ConnectionError)"})


class BridgeTests(CallTestCase):
    """The bridge forwards to the worker's socket; the socket, not the bridge, decides."""

    def setUp(self):
        super().setUp()
        if os.environ.get("PYTEST_CURRENT_TEST"):
            # The Home Assistant test plugin blocks sockets: allow the Unix socket only, the network stays blocked.
            import pytest_socket
            pytest_socket.socket_allow_hosts([], allow_unix_socket=True)
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "call.sock"
        self.addCleanup(lambda: (sock.unlink(missing_ok=True), sock.parent.rmdir()))
        self.socket_server = self.calls.serve(sock)
        self.addCleanup(self.socket_server.server_close)
        self.addCleanup(self.socket_server.shutdown)
        self.env = patch.dict("os.environ", {"HA_CALL_SOCKET": str(sock), "HA_CALL_CAPABILITY": self.key})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tool_call(self, arguments):
        answer = ha_call_mcp.respond({"method": "tools/call", "params": {"name": "call", "arguments": arguments}})
        return answer["isError"], json.loads(answer["content"][0]["text"])

    def test_a_call_through_the_bridge_sends_the_exact_request(self):
        error, result = self.tool_call({"service": "light.turn_off", "target": {"entity_id": "light.kitchen_ceiling"}})
        self.assertFalse(error)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.sent(), [("/api/services/light/turn_off", {"entity_id": "light.kitchen_ceiling"})])

    def test_a_malformed_request_through_the_bridge_sends_nothing(self):
        error, result = self.tool_call({"service": "light.turn_off", "target": {"code": "invented"}})
        self.assertTrue(error)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.fake.requests, [])

    def test_a_shell_command_with_its_own_key_meets_the_same_checks(self):
        """A request written straight to the socket, as a command could, gets no further than the bridge's."""
        with patch.dict("os.environ", {"HA_CALL_CAPABILITY": "guessed"}):
            _, result = self.tool_call({"service": "light.turn_off"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.fake.requests, [])

    def test_the_tool_is_marked_as_one_that_changes_things(self):
        tools = ha_call_mcp.respond({"method": "tools/list"})["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["call"])
        self.assertFalse(tools[0]["annotations"]["readOnlyHint"])
        self.assertTrue(tools[0]["annotations"]["destructiveHint"])


def configured(args):
    """Return every --config value of a Codex command line."""
    return [value for flag, value in pairwise(args) if flag == "--config"]


class LaunchTests(unittest.TestCase):
    """Which launch gets the tool, that none approves it ahead, and that the keys stay out of shell commands."""

    def launches(self):
        task = {"task_id": "t", "turns": [], "chat_settings": server.DEFAULT_CHAT_SETTINGS}
        with patch.object(server, "tasks", {"t": task}), patch.object(server, "read_options", return_value={}):
            return {"page": server.build_app_server_args(),
                    "schedule": server.build_app_server_args(attended=False),
                    "exec": server.build_codex_args("t", Path("p"), Path("f"), None)}

    def test_only_an_attended_page_chat_gets_the_tool(self):
        launches = self.launches()
        prefix = "mcp_servers.home_assistant_call."
        call_values = {name: [value for value in configured(args) if value.startswith(prefix)]
                       for name, args in launches.items()}
        self.assertTrue(call_values["page"])
        self.assertEqual(call_values["schedule"], [])
        self.assertEqual(call_values["exec"], [])
        self.assertIn('mcp_servers.home_assistant_call.env_vars=["HA_CALL_CAPABILITY"]', call_values["page"])

    def test_the_page_launch_asks_for_each_call(self):
        call_values = [value for value in configured(self.launches()["page"])
                       if value.startswith("mcp_servers.home_assistant_call.")]
        self.assertIn('mcp_servers.home_assistant_call.tools.call.approval_mode="prompt"', call_values)

    def test_no_launch_approves_the_tool_ahead(self):
        for name, args in self.launches().items():
            with self.subTest(launch=name):
                for value in configured(args):
                    if value.startswith("mcp_servers.home_assistant_call."):
                        self.assertNotIn("default_tools_approval_mode", value)
                        if "approval_mode" in value:
                            self.assertEqual(value, 'mcp_servers.home_assistant_call.tools.call.approval_mode="prompt"')

    def test_no_read_tool_holds_the_call_key(self):
        for value in configured(self.launches()["page"]):
            if ".env_vars=" in value and not value.startswith("mcp_servers.home_assistant_call."):
                self.assertNotIn("HA_CALL_CAPABILITY", value)

    def test_each_launch_keeps_the_tool_keys_out_of_shell_commands(self):
        for name, args in self.launches().items():
            with self.subTest(launch=name):
                self.assertIn('shell_environment_policy.exclude=["HA_*_CAPABILITY"]', configured(args))

    def test_codex_never_inherits_a_call_key_from_the_worker(self):
        with patch.dict("os.environ", {"HA_CALL_CAPABILITY": "x", "HA_CALL_SOCKET": "/tmp/x"}), \
                patch.object(server, "read_options", return_value={}):
            env = server.codex_env()
        self.assertNotIn("HA_CALL_CAPABILITY", env)
        self.assertNotIn("HA_CALL_SOCKET", env)


class Launched(BaseException):
    """Stops run_task at the moment it starts Codex, so the test can read what it would have started."""


class RunTaskLaunchTests(unittest.TestCase):
    """run_task itself, not only build_app_server_args, gives the tool and its key to a page chat only."""

    def launch(self, task_source):
        task = {"task_id": "t", "turns": [], "status": "queued", "task_source": task_source,
                "chat_settings": server.DEFAULT_CHAT_SETTINGS}
        started = {}

        def popen(args, env, **_):
            started.update(args=args, env=env)
            raise Launched

        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (temp / server.BASELINE_FILE).write_text("{}", encoding="utf-8")
        replaced = {
            "tasks": {"t": task}, "ha_call": HomeAssistantCall(call_worker()), "get_run_dir": temp,
            "read_options": {}, "task_options": {}, "codex_backup_dir": None, "model_pin_error": "",
            "uses_user_approvals": True, "sandbox_readiness": {"required": False, "ready": True},
        }
        quiet = ("record_diagnostic", "write_task_log", "update_task", "note_task_sign_in", "start_activity",
                 "refresh_core_address", "start_phase", "prepare_ha_docs")
        with ExitStack() as stack:
            for name, value in replaced.items():
                if name in ("tasks", "ha_call"):
                    stack.enter_context(patch.object(server, name, value))
                else:
                    stack.enter_context(patch.object(server, name, return_value=value))
            for name in quiet:
                stack.enter_context(patch.object(server, name))
            stack.enter_context(patch.object(server.protected_policy, "policy_problem", return_value=""))
            stack.enter_context(patch.object(server.ha_data, "set_exchange_info"))
            stack.enter_context(patch.object(server.subprocess, "Popen", side_effect=popen))
            with self.assertRaises(Launched):
                server.run_task("t", "Turn off the kitchen ceiling.")
            started["key_held"] = "t" in server.ha_call.capabilities
        return started

    def test_a_scheduled_or_lead_run_gets_neither_the_tool_nor_its_key(self):
        for source in ("schedule", "lead"):
            with self.subTest(task_source=source):
                started = self.launch(source)
                self.assertFalse([value for value in configured(started["args"])
                                  if value.startswith("mcp_servers.home_assistant_call.")])
                self.assertNotIn("HA_CALL_CAPABILITY", started["env"])
                self.assertFalse(started["key_held"])

    def test_a_page_chat_gets_the_tool_and_its_key(self):
        for source in ("page", None):
            with self.subTest(task_source=source):
                started = self.launch(source)
                self.assertIn('mcp_servers.home_assistant_call.env_vars=["HA_CALL_CAPABILITY"]',
                              configured(started["args"]))
                self.assertTrue(started["env"]["HA_CALL_CAPABILITY"])
                self.assertTrue(started["key_held"])

    def test_the_key_is_withdrawn_when_the_exchange_ends(self):
        calls = HomeAssistantCall(call_worker())

        def run_task(task_id, *_):
            calls.begin(task_id)

        with patch.object(server, "ha_call", calls), patch.object(server, "run_task", side_effect=run_task), \
                patch.object(server, "record_diagnostic"), patch.object(server, "tasks", {"t": {"task_id": "t"}}), \
                patch.object(server, "running_processes", {}):
            server._run_background_task("t", "Turn off the kitchen ceiling.", None, None)
        self.assertNotIn("t", calls.capabilities)


    def test_only_a_page_chat_is_told_to_use_the_tool(self):
        for source, has_tool in (("page", True), ("schedule", False), ("lead", False)):
            task = {"task_id": "t", "turns": [], "task_source": source}
            with self.subTest(task_source=source), patch.object(server, "tasks", {"t": task}), \
                    patch.object(server, "read_options", return_value={"HA_TOKEN": "invented-token"}):
                text = server.exchange_info("t", None)["home_assistant_api"]
            self.assertEqual("home_assistant_call" in text, has_tool)


class InstructionTests(unittest.TestCase):

    def test_the_page_rules_send_a_change_through_the_tool(self):
        self.assertIn("home_assistant_call call tool", server.PAGE_INSTRUCTIONS)

    def test_the_token_text_points_changes_to_the_tool(self):
        text = server.home_assistant_api_instructions({"HA_TOKEN": "invented-token"})
        self.assertIn("a reload and a restart included, use the home_assistant_call call tool", text)
        self.assertNotIn("automation/reload", text)
        self.assertEqual(server.home_assistant_api_instructions({}), "")

    def test_the_app_keeps_its_token_text_until_it_has_the_tool(self):
        text = server.remote_control_instructions({"HA_TOKEN": "invented-token"})
        self.assertNotIn("home_assistant_call", text)
        self.assertIn("automation/reload", text)


if __name__ == "__main__":
    unittest.main()
