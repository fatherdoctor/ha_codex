"""The home_assistant_call tool: what reaches Home Assistant, what never does, and who must ask first.

A fake Home Assistant records each request. The tests assert the exact request it received,
not only the tool's answer (plan CX-SIMPLE, section 6).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_server import server
import ha_call
import ha_call_mcp
from ha_call import HomeAssistantCall

WORKER_TOKEN = "invented-supervisor-token"


class FakeHomeAssistant:
    """An HTTP server that records each request and answers with a fixed status and body."""

    def __init__(self, status=200, answer=None):
        self.requests = []
        self.status = status
        self.answer = answer if answer is not None else []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append({"method": "POST", "path": self.path,
                                      "authorization": self.headers.get("Authorization"),
                                      "body": json.loads(self.rfile.read(length) or b"null")})
                payload = json.dumps(fake.answer).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/services/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def call_worker(status="running"):
    """A worker with one chat, t1, in the given status."""
    return SimpleNamespace(lock=threading.RLock(), tasks={"t1": {"task_id": "t1", "status": status}},
                           task_cancellation_requested=lambda task_id: False, ha_token=lambda: WORKER_TOKEN)


class CallTestCase(unittest.TestCase):
    """A call service with an audit file in a temporary folder and a fake Home Assistant."""

    answer = [{"entity_id": "light.kitchen_ceiling", "state": "off"}]
    status = 200

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.fake = FakeHomeAssistant(self.status, self.answer)
        self.addCleanup(self.fake.close)
        patcher = patch.object(ha_call, "CORE_SERVICES_URL", self.fake.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = call_worker()
        self.calls = HomeAssistantCall(self.worker, self.folder / "audit.jsonl")
        self.key = self.calls.begin("t1")

    def call(self, arguments, key=None):
        return self.calls.dispatch({"capability": self.key if key is None else key, "arguments": arguments})

    def audit_lines(self):
        path = self.folder / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ExactRequestTests(CallTestCase):

    def test_light_turn_off_sends_that_entity_and_nothing_else(self):
        result = self.call({"service": "light.turn_off", "target": {"entity_id": "light.kitchen_ceiling"}})
        self.assertEqual(self.fake.requests, [{"method": "POST", "path": "/api/services/light/turn_off",
                                               "authorization": f"Bearer {WORKER_TOKEN}",
                                               "body": {"entity_id": "light.kitchen_ceiling"}}])
        self.assertEqual(result, {"status": "ok", "service": "light.turn_off", "changed": ["light.kitchen_ceiling"]})

    def test_notify_keeps_its_target_and_its_full_message(self):
        message = "The garage door is still open. " * 200
        self.call({"service": "notify.mobile_app_example_phone", "data": {"title": "Garage", "message": message}})
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.fake.requests[0]["path"], "/api/services/notify/mobile_app_example_phone")
        self.assertEqual(self.fake.requests[0]["body"], {"title": "Garage", "message": message})

    def test_data_and_target_go_side_by_side(self):
        self.call({"service": "light.turn_on", "target": {"area_id": "kitchen", "entity_id": ["light.a", "light.b"]},
                   "data": {"brightness_pct": 10}})
        self.assertEqual(self.fake.requests[0]["body"],
                         {"brightness_pct": 10, "area_id": "kitchen", "entity_id": ["light.a", "light.b"]})

    def test_a_reload_is_a_service_too(self):
        self.call({"service": "automation.reload"})
        self.assertEqual(self.fake.requests[0]["path"], "/api/services/automation/reload")
        self.assertEqual(self.fake.requests[0]["body"], {})

    def test_every_call_writes_an_audit_line_without_the_data(self):
        self.call({"service": "notify.mobile_app_example_phone", "data": {"message": "invented private text"}})
        self.call({"service": "light.turn_off", "target": {"entity_id": "light.kitchen_ceiling"}})
        lines = self.audit_lines()
        self.assertEqual([(line["task_id"], line["service"], line["target"], line["result"]) for line in lines],
                         [("t1", "notify.mobile_app_example_phone", {}, "ok"),
                          ("t1", "light.turn_off", {"entity_id": "light.kitchen_ceiling"}, "ok")])
        self.assertNotIn("invented private text", (self.folder / "audit.jsonl").read_text())


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
        self.assertEqual(self.audit_lines(), [])

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

    def test_refused_services_send_nothing_and_write_an_audit_line(self):
        for service in ("hassio.addon_stop", "hassio.host_reboot", "codex_cli.run_task"):
            with self.subTest(service=service):
                self.assert_nothing_sent(self.call({"service": service}), status="refused")
        self.assertEqual([(line["service"], line["result"]) for line in self.audit_lines()],
                         [("hassio.addon_stop", "refused"), ("hassio.host_reboot", "refused"),
                          ("codex_cli.run_task", "refused")])

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
        # Hostile spellings of a refused or invalid service: none may reach Home Assistant.
        cases += [{"service": service} for service in (
            "hassio.addon_stop\n", " hassio.addon_stop", "HASSIO.addon_stop", "Hassio.addon_stop",
            "hassio .addon_stop", "hassio..addon_stop", "\u04bbassio.addon_stop", "hassio.addon_stop?x=1",
            "hassio/addon_stop", "../hassio.addon_stop", "codex_cli.run_task#", "codex_cli.\u0440un_task",
            "light.turn_off.extra", "light.", ".turn_off", "", "x" * 201)]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assert_nothing_sent(self.call(arguments), status="refused")


class FailedCallTests(CallTestCase):
    status = 400
    answer = {"message": "invented validation error"}

    def test_a_failed_call_is_reported_and_not_sent_again(self):
        result = self.call({"service": "light.turn_on", "data": {"brightness_pct": 500}})
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 400)
        self.assertEqual(self.audit_lines()[0]["result"], "failed")


class BridgeTests(CallTestCase):
    """The bridge forwards to the worker's socket; the socket, not the bridge, decides."""

    def setUp(self):
        super().setUp()
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
        self.assertEqual([(item["path"], item["body"]) for item in self.fake.requests],
                         [("/api/services/light/turn_off", {"entity_id": "light.kitchen_ceiling"})])

    def test_a_refused_service_through_the_bridge_sends_nothing(self):
        error, result = self.tool_call({"service": "hassio.addon_stop", "data": {"addon": "core_ssh"}})
        self.assertTrue(error)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.fake.requests, [])

    def test_a_shell_command_with_its_own_key_meets_the_same_checks(self):
        """A request written straight to the socket, as a command could, gets no further than the bridge's."""
        with patch.dict("os.environ", {"HA_CALL_CAPABILITY": "guessed"}):
            _, result = self.tool_call({"service": "light.turn_off"})
        self.assertEqual(result["status"], "unavailable")
        _, result = self.tool_call({"service": "codex_cli.run_task"})
        self.assertEqual(result["status"], "refused")
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
        call_values = {name: [value for value in configured(args) if value.startswith("mcp_servers.home_assistant_call.")]
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


GATE_HOOK = Path(__file__).resolve().parents[1] / "gate" / "gate_hook.py"
CALL_ARGUMENTS = {"domain": "light", "service": "turn_on", "target": {"entity_id": ["light.sample_room"]}}


class CommandCheckTests(unittest.TestCase):
    """The command check gives the call tool no decision: an allow would skip the person, a refusal every call."""

    def run_hook(self, event, remote_on, tool_name="mcp__home_assistant_call__call", tool_input=None):
        """Run the hook as Codex runs it (managed_config.toml), with the event on stdin."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "remote_control.json"
            state.write_text(json.dumps({"enabled": remote_on}), encoding="utf-8")
            payload = {"hook_event_name": event, "tool_name": tool_name,
                       "tool_input": CALL_ARGUMENTS if tool_input is None else tool_input}
            return subprocess.run([sys.executable, "-I", str(GATE_HOOK), "--event", event, "--state", str(state)],
                                  input=json.dumps(payload), capture_output=True, text=True, timeout=30,
                                  check=False)

    def test_the_hook_gives_the_call_tool_no_decision(self):
        for event in ("PermissionRequest", "PreToolUse"):
            for remote_on in (False, True):
                with self.subTest(event=event, remote_control=remote_on):
                    result = self.run_hook(event, remote_on)
                    self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_text_in_the_call_does_not_make_the_hook_refuse_it(self):
        data = {**CALL_ARGUMENTS, "data": {"message": "danger-full-access"}}
        result = self.run_hook("PreToolUse", True, tool_input=data)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_another_tool_with_the_same_input_is_still_checked(self):
        result = self.run_hook("PermissionRequest", True, tool_name="mcp__other__call")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["decision"]["behavior"], "deny")


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
