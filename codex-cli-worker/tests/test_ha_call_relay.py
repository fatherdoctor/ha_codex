"""The home_assistant_call tool in the ChatGPT app: how the relay starts it, its key, and the exact request.

The relay (remote control) is one long Codex process. It gets the call tool with its own key for
the whole run, and the tool is not approved ahead, so Codex asks the person in the app before each
call. The tests run the tool as the relay's command line starts it, against a fake Home Assistant
that records each request (plan CX-SIMPLE, step C, and CX-TEST-E2E.f). The fake stands in for
requests.post, so no test opens a network socket.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_server import server  # First: it puts the worker's folder on sys.path.

# isort: split
from test_ha_call import WORKER_TOKEN, FakeHomeAssistant, call_worker
import ha_call
import remote_control as rc
from ha_call import RELAY_TASK, HomeAssistantCall

ROOT = Path(__file__).resolve().parents[1]
CALL_PREFIX = "mcp_servers.home_assistant_call."


def configured(args):
    """Return every --config or -c value of a Codex command line."""
    return [value for flag, value in pairwise(args) if flag in ("--config", "-c")]


def server_settings(args, name):
    """Return the settings of one MCP server on a command line, as {setting: value}."""
    prefix = f"mcp_servers.{name}."
    return {key[len(prefix):]: json.loads(value) for key, _, value in
            (item.partition("=") for item in configured(args)) if key.startswith(prefix)}


def live_relay():
    """A relay process that runs and is not stopping."""
    return SimpleNamespace(poll=lambda: None), False


def allow_unix_sockets():
    """Allow Unix sockets under pytest, whose Home Assistant plugin blocks sockets; the network stays blocked."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest_socket
        pytest_socket.socket_allow_hosts([], allow_unix_socket=True)


def fake_home_assistant(test):
    """Put a fake Home Assistant in place of requests.post in ha_call for the test, and return it."""
    fake = FakeHomeAssistant(answer=[{"entity_id": "light.kitchen_ceiling", "state": "off"}])
    patcher = patch.object(ha_call.requests, "post", fake.post)
    patcher.start()
    test.addCleanup(patcher.stop)
    return fake


def sent(fake):
    """Return (path, authorization, body) of each request the fake Home Assistant received."""
    return [(item["url"].removeprefix("http://supervisor/core"), item["headers"]["Authorization"], item["json"])
            for item in fake.requests]


LIGHT_OFF = {"service": "light.turn_off", "target": {"entity_id": "light.kitchen_ceiling"}}
LIGHT_OFF_SENT = [("/api/services/light/turn_off", f"Bearer {WORKER_TOKEN}", {"entity_id": "light.kitchen_ceiling"})]


class RelayKeyTests(unittest.TestCase):
    """The relay's key holds while the relay runs, and only the latest launch's key holds."""

    def setUp(self):
        self.fake = fake_home_assistant(self)
        self.running = True
        # No page chat runs: the relay key must not depend on one.
        self.calls = HomeAssistantCall(call_worker(status="completed"), relay_running=lambda: self.running)
        self.key = self.calls.begin(RELAY_TASK)

    def call(self, key=None):
        return self.calls.dispatch({"capability": self.key if key is None else key, "arguments": LIGHT_OFF})

    def test_a_running_relay_sends_the_exact_request(self):
        result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sent(self.fake), LIGHT_OFF_SENT)

    def test_a_stopped_relay_sends_nothing(self):
        self.running = False
        self.assertEqual(self.call()["status"], "unavailable")
        self.assertEqual(self.fake.requests, [])

    def test_a_new_launch_replaces_the_old_key(self):
        old = self.key
        self.calls.begin(RELAY_TASK)
        self.assertEqual(self.call(key=old)["status"], "unavailable")
        self.assertEqual(self.fake.requests, [])

    def test_the_relay_key_is_not_a_page_chats_key(self):
        page_key = self.calls.begin("t1")
        self.calls.end("t1")
        self.assertEqual(self.call(key=page_key)["status"], "unavailable")
        self.assertEqual(self.call()["status"], "ok")

    def test_a_page_chat_id_never_takes_the_relay_name(self):
        # Page chat ids start with a UTC date, so no chat can be named like the relay.
        self.assertFalse(RELAY_TASK[:1].isdigit())


class RelayLaunchTests(unittest.TestCase):
    """What the relay's command line holds: the tool, not approved ahead, and the shell exclude."""

    def command(self):
        supervisor = rc.Supervisor("/synthetic/codex", Path("/nonexistent/state.json"), lambda: {},
                                   lambda: "Sample relay", gate=lambda: "",
                                   run=Mock(return_value=subprocess.CompletedProcess([], 0)),
                                   tool_args_factory=server.relay_tool_args)
        with patch.object(server, "read_options", return_value={}):
            return supervisor._command("app-server", "--listen", "unix:///tmp/x.sock", "--remote-control")

    def test_the_relay_registers_the_call_tool_with_its_own_key(self):
        settings = server_settings(self.command(), "home_assistant_call")
        self.assertEqual(settings["env_vars"], ["HA_CALL_CAPABILITY"])
        self.assertTrue(settings["args"][0].endswith("ha_call_mcp.py"))
        self.assertTrue(settings["required"])

    def test_the_relay_does_not_approve_the_call_tool_ahead(self):
        # A mode of "prompt" (ask each time) is allowed; "approve" would skip Codex's question.
        values = [value for value in configured(self.command()) if value.startswith(CALL_PREFIX)]
        self.assertTrue(values)
        for value in values:
            self.assertNotIn('"approve"', value)
        self.assertEqual([value for value in configured(self.command()) if "default_tools_approval_mode" in value], [])

    def test_the_read_tool_keeps_its_own_key(self):
        read = server_settings(self.command(), "home_assistant_relay")
        self.assertEqual(read["env_vars"], ["HA_RELAY_CAPABILITY"])
        self.assertEqual(read["tools.read.approval_mode"], "approve")

    def test_the_relay_keeps_the_tool_keys_out_of_shell_commands(self):
        self.assertIn('shell_environment_policy.exclude=["HA_*_CAPABILITY"]', configured(self.command()))

    def test_each_launch_gets_a_live_call_key_that_replaces_the_one_before(self):
        # relay_env is the launch environment only (Supervisor._loop, just before the process starts).
        with patch.object(server, "refresh_core_address"), \
                patch.object(server, "relay_process_state", side_effect=live_relay), \
                patch.object(server, "read_options", return_value={}):
            first = rc.remote_env(server.relay_env())
            second = rc.remote_env(server.relay_env())
            self.assertIsNone(server.ha_call.task_for(first["HA_CALL_CAPABILITY"]))
            self.assertEqual(server.ha_call.task_for(second["HA_CALL_CAPABILITY"]), RELAY_TASK)
        self.assertNotEqual(second["HA_CALL_CAPABILITY"], second["HA_RELAY_CAPABILITY"])
        self.assertNotIn("HA_CALL_SOCKET", second)

    def test_a_probe_environment_holds_no_tool_key(self):
        environment = rc.remote_env(server.relay_probe_env())
        self.assertEqual([name for name in environment if name.endswith("_CAPABILITY")], [])

    def test_the_app_supervisor_makes_keys_only_for_a_launch(self):
        supervisor = server.remote_control_supervisor
        with patch.object(server, "relay_env", return_value={"from": "launch"}), \
                patch.object(server, "relay_probe_env", return_value={"from": "probe"}):
            self.assertEqual(supervisor.launch_env_factory(), {"from": "launch"})
            self.assertEqual(supervisor.env_factory(), {"from": "probe"})

    def test_the_key_stops_when_the_relay_stops(self):
        with patch.object(server, "refresh_core_address"), patch.object(server, "read_options", return_value={}):
            key = server.relay_env()["HA_CALL_CAPABILITY"]
            stopped = ((None, False), (SimpleNamespace(poll=lambda: None), True),
                       (SimpleNamespace(poll=lambda: 1), False))
            for state in stopped:
                with self.subTest(state=state), patch.object(server, "relay_process_state", return_value=state):
                    self.assertIsNone(server.ha_call.task_for(key))


class ManagedConfigTests(unittest.TestCase):
    """The managed config keeps the tool keys out of commands for every client, and remote control checks it."""

    def problem(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed_config.toml"
            path.write_text(text, encoding="utf-8")
            return rc.managed_config_problem(path)

    def test_the_shipped_managed_config_holds(self):
        self.assertEqual(rc.managed_config_problem(ROOT / "gate" / "managed_config.toml"), "")

    def test_the_pattern_is_the_one_each_launch_sets(self):
        self.assertEqual(server.SHELL_EXCLUDED_ENV, (rc.TOOL_KEY_EXCLUDE,))

    def test_a_managed_config_without_the_exclude_blocks(self):
        for text in ("", "[shell_environment_policy]\n", '[shell_environment_policy]\nexclude = ["OTHER"]\n',
                     '[shell_environment_policy]\nexclude = "HA_*_CAPABILITY"\n',
                     '[shell_environment_policy]\nexclude = ["HA_*_CAPABILITY"]\nfilters = {}\n',
                     'shell_environment_policy = "none"\n', "not = [toml"):
            with self.subTest(text=text):
                self.assertNotEqual(self.problem(text), "")

    def test_a_marker_without_the_managed_config_is_missing_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "gate.json"
            shipped = json.loads((ROOT / "gate" / "remote-control-gate.json").read_text(encoding="utf-8"))
            files = []
            for name in ("requirements", "hook"):
                path = Path(tmp) / Path(shipped[name]).name
                path.write_text("", encoding="utf-8")
                files.append(str(path))
            marker.write_text(json.dumps({"files": files, "requirements": files[0], "hook": files[1]}),
                              encoding="utf-8")
            self.assertEqual(rc.gate_problem(marker), rc.GATE_MISSING)

    def test_remote_control_does_not_start_without_the_exclude(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = ("requirements.toml", "managed.toml", "hook.py")
            requirements, managed, hook = (Path(tmp) / name for name in names)
            requirements.write_text((ROOT / "gate" / "requirements.toml").read_text(), encoding="utf-8")
            managed.write_text("[shell_environment_policy]\nexclude = []\n", encoding="utf-8")
            hook.write_text("", encoding="utf-8")
            marker = Path(tmp) / "gate.json"
            marker.write_text(json.dumps({"files": [str(requirements), str(managed), str(hook)],
                                          "requirements": str(requirements), "managed_config": str(managed),
                                          "hook": str(hook)}), encoding="utf-8")
            problem = rc.gate_problem(marker)
        self.assertTrue(problem.startswith(rc.GATE_BROKEN))
        self.assertIn("tool keys", problem)

    def test_the_shipped_marker_names_the_managed_config(self):
        marker = json.loads((ROOT / "gate" / "remote-control-gate.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["managed_config"], "/etc/codex/managed_config.toml")
        self.assertIn(marker["managed_config"], marker["files"])


class RelayInstructionTests(unittest.TestCase):

    def test_the_app_sends_a_change_through_the_tool(self):
        for options in ({}, {"HA_TOKEN": "invented-token"}):
            with self.subTest(token=bool(options)):
                text = server.remote_control_instructions(options)
                self.assertIn("use the home_assistant_call call tool", text)
                self.assertIn("Codex asks the owner before each call", text)
                self.assertNotIn("automation/reload", text)

    def test_without_a_token_the_app_still_has_both_tools(self):
        self.assertIn("The read tool and the call tool still work through the worker",
                      server.remote_control_instructions({}))


class RelayToolEndToEndTests(unittest.TestCase):
    """CX-TEST-E2E.f: start the tool exactly as the relay's command line says, and check what reaches Home Assistant."""

    def setUp(self):
        allow_unix_sockets()
        self.fake = fake_home_assistant(self)
        folder = Path(tempfile.mkdtemp(dir="/tmp"))  # A short path: a Unix socket path has a length limit.
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        self.socket = folder / "call.sock"
        for patcher in (patch.object(server, "ha_token", return_value=WORKER_TOKEN),
                        patch.object(server, "refresh_core_address"),
                        patch.object(server, "read_options", return_value={}),
                        patch.object(server, "relay_process_state", side_effect=live_relay)):
            patcher.start()
            self.addCleanup(patcher.stop)
        socket_server = server.ha_call.serve(self.socket)
        self.addCleanup(socket_server.server_close)
        self.addCleanup(socket_server.shutdown)
        settings = server_settings(server.relay_tool_args(), "home_assistant_call")
        environment = rc.remote_env(server.relay_env())
        # Codex gives a bridge a short fixed list plus its env_vars (Step 0 record, part 5); nothing else.
        self.bridge_env = {"PATH": os.environ.get("PATH", ""), "HA_CALL_SOCKET": str(self.socket),
                           **{name: environment[name] for name in settings["env_vars"]}}
        self.bridge_command = [settings["command"], *settings["args"]]

    def run_bridge(self, *requests):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        messages += [{"jsonrpc": "2.0", "id": 3 + index, "method": "tools/call",
                      "params": {"name": "call", "arguments": arguments}} for index, arguments in enumerate(requests)]
        done = subprocess.run(self.bridge_command, input="".join(json.dumps(item) + "\n" for item in messages),
                              env=self.bridge_env, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        answers = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual([tool["name"] for tool in answers[1]["result"]["tools"]], ["call"])
        return [json.loads(answer["result"]["content"][0]["text"]) for answer in answers[2:]]

    def test_light_turn_off_sends_that_entity_and_nothing_else(self):
        results = self.run_bridge(LIGHT_OFF)
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(sent(self.fake), LIGHT_OFF_SENT)

    def test_notify_keeps_its_target_and_its_full_message(self):
        message = "The garage door is still open. " * 200
        self.run_bridge({"service": "notify.mobile_app_example_phone", "data": {"title": "Garage", "message": message}})
        self.assertEqual([(path, body) for path, _, body in sent(self.fake)],
                         [("/api/services/notify/mobile_app_example_phone", {"title": "Garage", "message": message})])

    def test_a_stopped_relay_sends_nothing(self):
        with patch.object(server, "relay_process_state", return_value=(None, False)):
            results = self.run_bridge({"service": "light.turn_off"})
        self.assertEqual(results[0]["status"], "unavailable")
        self.assertEqual(self.fake.requests, [])

    def test_a_wrong_key_or_the_read_tools_key_sends_nothing(self):
        read_key = rc.remote_env(server.relay_env())["HA_RELAY_CAPABILITY"]
        for key in ("", "guessed", read_key):
            with self.subTest(key=key[:4]):
                self.bridge_env["HA_CALL_CAPABILITY"] = key
                self.assertEqual(self.run_bridge({"service": "light.turn_off"})[0]["status"], "unavailable")
        self.assertEqual(self.fake.requests, [])


class FakeRelayProcess:
    """A remote-control process that runs until the test releases it; it prints nothing."""

    pid = None

    def __init__(self):
        self.released = threading.Event()
        self.stdout = self

    def readline(self):
        self.released.wait(30)
        return ""

    def poll(self):
        return 0 if self.released.is_set() else None

    def wait(self, timeout=None):
        self.released.wait(timeout)
        return 0


class RepeatedStartTests(unittest.TestCase):
    """Review C2: a second Start request runs the probes again, and the running relay keeps both its keys."""

    def setUp(self):
        self.fake = fake_home_assistant(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.process = FakeRelayProcess()
        self.popen = Mock(return_value=self.process)
        self.probe_environments = []
        self.namespace_probe = Mock(return_value=subprocess.CompletedProcess([], 0))
        control = Mock()
        control.enable.return_value = {"status": "connecting"}
        self.supervisor = rc.Supervisor(
            "/synthetic/codex", Path(folder.name) / "state.json", server.relay_probe_env, lambda: "Sample relay",
            popen=self.popen, run=self.namespace_probe,
            control_factory=lambda path: control, launch_env_factory=server.relay_env)
        # Keep the native rules self-test on (it asks for a probe environment) but skip the files of the image.
        self.supervisor.gate = lambda: ""
        for patcher in (patch.object(server, "remote_control_supervisor", self.supervisor),
                        patch.object(server, "ha_token", return_value=WORKER_TOKEN),
                        patch.object(server, "refresh_core_address"),
                        patch.object(server, "read_options", return_value={}),
                        patch.object(rc, "exec_rules_self_test", side_effect=self.rules_self_test)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.end_relay)

    def rules_self_test(self, binary, environment):
        self.probe_environments.append(environment)
        return ""

    def end_relay(self):
        self.supervisor.save_enabled(False)
        self.supervisor.wake.set()
        self.process.released.set()
        if self.supervisor.thread is not None:
            self.supervisor.thread.join(timeout=10)

    def wait_for_launch(self):
        deadline = time.monotonic() + 10
        while self.supervisor.proc is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIs(self.supervisor.proc, self.process)

    def test_start_twice_launches_once_and_the_first_keys_still_hold(self):
        first_ok, _ = self.supervisor.start()
        self.wait_for_launch()
        launch = self.popen.call_args.kwargs["env"]
        second_ok, _ = self.supervisor.start()
        self.assertTrue(first_ok and second_ok)
        self.assertEqual(self.popen.call_count, 1)
        self.assertGreaterEqual(len(self.probe_environments), 3)  # one probe in start(), one in the loop, one more
        self.probe_environments += [call.kwargs["env"] for call in self.namespace_probe.call_args_list]
        for environment in self.probe_environments:
            self.assertNotIn("HA_CALL_CAPABILITY", environment)
            self.assertNotIn("HA_RELAY_CAPABILITY", environment)
        result = server.ha_call.dispatch({"capability": launch["HA_CALL_CAPABILITY"], "arguments": LIGHT_OFF})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sent(self.fake), LIGHT_OFF_SENT)
        self.assertTrue(server.relay_reads.active(launch["HA_RELAY_CAPABILITY"]))

    def test_the_keys_stop_when_the_relay_ends(self):
        self.supervisor.start()
        self.wait_for_launch()
        launch = self.popen.call_args.kwargs["env"]
        self.end_relay()
        self.assertIsNone(server.ha_call.task_for(launch["HA_CALL_CAPABILITY"]))
        self.assertFalse(server.relay_reads.active(launch["HA_RELAY_CAPABILITY"]))
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
