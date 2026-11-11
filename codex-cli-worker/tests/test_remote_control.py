"""Remote control: the machine name, the command and its environment, the gate, the supervisor and the routes."""
from __future__ import annotations

import importlib.util
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_control as rc


class FakeStdout:
    """A process output stream fed line by line from the test; an empty string ends it."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()

    def readline(self) -> str:
        return self.lines.get(timeout=5)


class FakeProc:
    """A stand-in for the remote-control process: it runs until the test or a signal ends it."""

    pid = 999999

    def __init__(self) -> None:
        self.stdout = FakeStdout()
        self.returncode: int | None = None
        self.done = threading.Event()
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, code: int) -> None:
        self.returncode = code
        self.stdout.lines.put("")
        self.done.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self.done.wait(5 if timeout is None else timeout):
            raise subprocess.TimeoutExpired("codex", timeout)
        return int(self.returncode or 0)

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.exit(-sig)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    """Poll until the predicate holds or the timeout passes."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class NameTests(unittest.TestCase):
    """The name shown in the ChatGPT app."""

    def test_default_is_home_assistant(self) -> None:
        self.assertEqual(rc.machine_name(None), "Home Assistant")
        self.assertEqual(rc.machine_name("   "), "Home Assistant")

    def test_keeps_spaces_and_drops_other_characters(self) -> None:
        self.assertEqual(rc.machine_name("Lake  House!"), "Lake House")
        self.assertEqual(rc.machine_name("$(reboot)"), "reboot")
        self.assertEqual(rc.machine_name("Café"), "Caf")

    def test_cut_to_63_bytes(self) -> None:
        self.assertEqual(len(rc.machine_name("a" * 100)), 63)

    def test_fallback_has_no_spaces(self) -> None:
        self.assertEqual(rc.fallback_name("Home Assistant"), "Home-Assistant")
        self.assertEqual(rc.fallback_name("..."), "Home-Assistant")


class CommandTests(unittest.TestCase):
    """The command line and the environment the process gets."""

    def test_name_is_an_argument_never_part_of_the_script(self) -> None:
        cmd = rc.build_command("/usr/local/bin/codex", "Home Assistant", "remote-control", "--no-daemon")
        self.assertEqual(cmd[:4], ["unshare", "-Ur", "-u", "sh"])
        self.assertNotIn("Home Assistant", cmd[5])
        self.assertEqual(cmd[7:], ["Home Assistant", "Home-Assistant", "/usr/local/bin/codex",
                                   "remote-control", "--no-daemon"])

    def test_environment_has_no_credentials(self) -> None:
        env = rc.remote_env({"SUPERVISOR_TOKEN": "s", "HASSIO_TOKEN": "h", "HA_TOKEN": "t", "HA_URL": "u",
                             "HA_DATA_SOCKET": "d", "CODEX_HOME": "/data/codex-home", "PATH": "/bin"})
        self.assertEqual(env, {"CODEX_HOME": "/data/codex-home", "PATH": "/bin"})


class GateTests(unittest.TestCase):
    """Remote control starts only when the command gate's files are in the image."""

    def test_missing_marker_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rc.gate_problem(Path(tmp) / "none.json"), rc.GATE_MISSING)

    def test_marker_naming_a_missing_file_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "gate.json"
            marker.write_text(json.dumps({"files": [str(Path(tmp) / "hook")]}), encoding="utf-8")
            self.assertIn("missing:", rc.gate_problem(marker))

    def test_empty_or_malformed_marker_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "gate.json"
            for text in ("{}", '{"files": []}', '{"files": [1]}', "not json"):
                marker.write_text(text, encoding="utf-8")
                self.assertEqual(rc.gate_problem(marker), rc.GATE_MISSING)

    def test_marker_with_every_file_present_allows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hook = Path(tmp) / "hook"
            hook.write_text("x", encoding="utf-8")
            marker = Path(tmp) / "gate.json"
            marker.write_text(json.dumps({"files": [str(hook)]}), encoding="utf-8")
            self.assertEqual(rc.gate_problem(marker), "")

    def test_redact_keeps_the_pairing_code_only(self) -> None:
        token = "A" * 40
        self.assertEqual(rc.redact(f"token {token}\n"), "token [hidden]")
        self.assertEqual(rc.redact("Pairing code: ABCD-1234", keep="ABCD-1234"), "Pairing code: ABCD-1234")


class SupervisorTests(unittest.TestCase):
    """Start, stop, restart and pairing, with a fake process."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "remote_control.json"
        self.gate_reason = ""
        self.procs: list[FakeProc] = []
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

        def popen(cmd, **kwargs):
            self.commands.append(cmd)
            self.envs.append(kwargs["env"])
            proc = FakeProc()
            self.procs.append(proc)
            return proc

        self.sup = rc.Supervisor(
            codex_binary="/usr/local/bin/codex", state_path=self.state,
            env_factory=lambda: {"HA_TOKEN": "secret", "CODEX_HOME": "/data/codex-home"},
            name_factory=lambda: "Home Assistant", gate=lambda: self.gate_reason, popen=popen)
        patcher = patch.multiple(rc, BACKOFF_START=0.05, BACKOFF_CAP=0.1, STOP_GRACE=0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.sup.stop)

    def test_off_until_turned_on(self) -> None:
        self.assertFalse(self.sup.enabled())
        self.assertEqual(self.sup.resume()["state"], "off")
        self.assertEqual(self.procs, [])

    def test_start_refused_and_not_saved_while_gate_missing(self) -> None:
        self.gate_reason = rc.GATE_MISSING
        ok, state = self.sup.start()
        self.assertFalse(ok)
        self.assertEqual(state["state"], "blocked")
        self.assertFalse(state["available"])
        self.assertFalse(self.sup.enabled())
        self.assertEqual(self.procs, [])

    def test_resume_blocked_when_gate_removed_after_being_left_on(self) -> None:
        self.sup.save_enabled(True)
        self.gate_reason = rc.GATE_MISSING
        self.assertEqual(self.sup.resume()["state"], "blocked")
        self.assertEqual(self.procs, [])

    def test_start_runs_foreground_process_and_reports_name_and_code(self) -> None:
        ok, _ = self.sup.start()
        self.assertTrue(ok)
        self.assertTrue(self.sup.enabled())
        self.assertTrue(wait_for(lambda: self.procs))
        self.assertEqual(self.commands[0][-2:], ["remote-control", "--no-daemon"])
        self.assertNotIn("HA_TOKEN", self.envs[0])
        proc = self.procs[0]
        proc.stdout.lines.put("Pairing code: ABCD-1234\n")
        self.assertTrue(wait_for(lambda: self.sup.snapshot()["pairing_code"] == "ABCD-1234"))
        snap = self.sup.snapshot()
        self.assertEqual(snap["state"], "pairing")
        self.assertTrue(snap["pairing_qr"].startswith("data:image/svg+xml;base64,") or snap["pairing_qr"] == "")
        proc.stdout.lines.put("This machine is available for remote control as Home Assistant.\n")
        self.assertTrue(wait_for(lambda: self.sup.snapshot()["state"] == "ready"))
        self.assertEqual(self.sup.snapshot()["machine_name"], "Home Assistant")

    def test_second_start_does_not_start_a_second_process(self) -> None:
        self.sup.start()
        self.assertTrue(wait_for(lambda: self.procs))
        self.sup.start()
        time.sleep(0.05)
        self.assertEqual(len(self.procs), 1)

    def test_exit_restarts_with_backoff_while_on(self) -> None:
        self.sup.start()
        self.assertTrue(wait_for(lambda: self.procs))
        self.procs[0].exit(1)
        self.assertTrue(wait_for(lambda: len(self.procs) == 2))
        self.assertEqual(self.sup.snapshot()["restarts"], 1)

    def test_stop_saves_off_and_ends_the_process(self) -> None:
        self.sup.start()
        self.assertTrue(wait_for(lambda: self.procs))
        with patch.object(rc.os, "killpg", side_effect=ProcessLookupError):
            state = self.sup.stop()
        self.assertFalse(self.sup.enabled())
        self.assertEqual(state["state"], "off")
        self.assertTrue(self.procs[0].signals)
        time.sleep(0.2)
        self.assertEqual(len(self.procs), 1)

    def test_pair_refused_when_not_running(self) -> None:
        ok, state = self.sup.pair()
        self.assertFalse(ok)
        self.assertIn("Turn remote control on", state["error"])

    def test_pair_reads_new_code(self) -> None:
        self.sup.start()
        self.assertTrue(wait_for(lambda: self.procs))
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs["env"]))
            return types.SimpleNamespace(stdout="Pairing code: WXYZ-9876\n", stderr="")

        self.sup.run = run
        ok, state = self.sup.pair()
        self.assertTrue(ok)
        self.assertEqual(state["pairing_code"], "WXYZ-9876")
        self.assertEqual(calls[0][0][-2:], ["remote-control", "pair"])
        self.assertNotIn("HA_TOKEN", calls[0][1])


SERVER_PATH = ROOT / "server.py"
if importlib.util.find_spec("websocket") is None:
    sys.modules["websocket"] = types.ModuleType("websocket")
SPEC = importlib.util.spec_from_file_location("codex_worker_server_rc", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class RouteTests(unittest.TestCase):
    """Only the app page changes remote control; the integration's token can read it."""

    def setUp(self) -> None:
        self.client = server.app.test_client()
        self.ingress = {"headers": {"X-Ingress-Path": "/api/hassio_ingress/test"},
                        "environ_overrides": {"REMOTE_ADDR": server.INGRESS_PROXY_IP}}
        patcher = patch.object(server, "api_token", return_value="tok")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_token_reads_state(self) -> None:
        with patch.object(server.remote_control_supervisor, "snapshot", return_value={"state": "off"}):
            response = self.client.get("/remote-control", headers={"Authorization": "Bearer tok"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["remote_control"], {"state": "off"})

    def test_token_cannot_turn_it_on(self) -> None:
        with patch.object(server.remote_control_supervisor, "start") as start:
            response = self.client.post("/remote-control/start", headers={"Authorization": "Bearer tok"}, json={})
        self.assertEqual(response.status_code, 403)
        start.assert_not_called()

    def test_page_start_refused_reports_reason(self) -> None:
        state = {"state": "blocked", "blocked_reason": rc.GATE_MISSING}
        with patch.object(server.remote_control_supervisor, "start", return_value=(False, state)):
            response = self.client.post("/remote-control/start", json={}, **self.ingress)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], rc.GATE_MISSING)

    def test_page_stop_and_unknown_action(self) -> None:
        with patch.object(server.remote_control_supervisor, "stop", return_value={"state": "off"}):
            self.assertEqual(self.client.post("/remote-control/stop", json={}, **self.ingress).status_code, 200)
        self.assertEqual(self.client.post("/remote-control/reboot", json={}, **self.ingress).status_code, 404)


if __name__ == "__main__":
    unittest.main()
