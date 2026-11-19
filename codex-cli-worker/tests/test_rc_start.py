"""CX-RC-START regressions using disposable configuration and fake transports."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_control as rc


class StartRegressions(unittest.TestCase):
    def test_name_uses_child_profile_without_parent_capabilities(self):
        command = rc.build_command(
            "codex", "Home Assistant", "remote-control", "--no-daemon"
        )
        self.assertEqual(command[:2], [rc.RELAY_LAUNCHER, rc.RELAY_BWRAP])
        self.assertIn("--unshare-uts", command)
        self.assertEqual(command[command.index("--hostname") + 1], "Home Assistant")
        self.assertIn("--cap-drop", command)
        profile = (ROOT / "apparmor.txt").read_text()
        parent, child = profile.split("  profile /opt/codex-sandbox/bwrap.real", 1)
        self.assertNotIn("capability sys_admin,", parent)
        self.assertIn("/opt/codex-sandbox/bwrap rix,", child)

    def test_namespace_failure_falls_back_once_and_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = rc.Supervisor(
                "codex",
                Path(tmp) / "state",
                dict,
                lambda: "Home Assistant",
                run=lambda *a, **k: subprocess.CompletedProcess(
                    a, 1, "", "namespace denied"
                ),
            )
            self.assertEqual(
                sup._command("remote-control", "--no-daemon"),
                [
                    rc.RELAY_LAUNCHER,
                    "--plain",
                    "codex",
                    "remote-control",
                    "--no-daemon",
                ],
            )
            self.assertEqual(sup.snapshot()["name_state"], "namespace-unavailable")
            self.assertIn("without", sup.snapshot()["name_warning"])
            self.assertEqual(
                sup._command("remote-control", "pair"),
                [rc.RELAY_LAUNCHER, "--plain", "codex", "remote-control", "pair"],
            )

    def test_requirements_exclude_never(self):
        import tomllib

        text = (ROOT / "gate/requirements.toml").read_text()
        self.assertNotIn("never", tomllib.loads(text)["allowed_approval_policies"])

    def test_relay_fallback_is_honest_without_inventing_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = rc.Supervisor("codex", Path(tmp) / "state", dict, lambda: "")
            sup._parse(
                "configWarning: sandbox mode danger-full-access not allowed; falling back to read-only"
            )
            self.assertIn("downgraded", sup.snapshot()["policy_warning"])
            self.assertTrue(any("downgraded" in line for line in sup.snapshot()["log"]))
            self.assertTrue("read-only" in (ROOT / "web/index.html").read_text())


class FurtherRegressions(unittest.TestCase):
    def test_successful_name_probe_and_pair_share_namespace_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def probe(cmd, **kw):
                calls.append((cmd, kw))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            sup = rc.Supervisor(
                "codex",
                Path(tmp) / "state",
                lambda: {"HA_TOKEN": "synthetic", "PATH": "/bin"},
                lambda: "Home Assistant",
                run=probe,
            )
            command = sup._command("remote-control", "--no-daemon")
            pair = sup._command("remote-control", "pair")
            self.assertEqual(command[:-2], pair[:-2])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][-1], "/bin/true")
            self.assertEqual(calls[0][1]["env"]["HA_TOKEN"], "synthetic")
            self.assertEqual(sup.snapshot()["name_state"], "private-uts")

    def test_real_launch_namespace_failure_does_not_restart_in_name_loop(self):
        from test_remote_control import FakeControl, FakeProc, wait_for

        with tempfile.TemporaryDirectory() as tmp:
            commands = []
            procs = []

            def popen(cmd, **kw):
                commands.append(cmd)
                proc = FakeProc()
                procs.append(proc)
                if len(procs) == 1:
                    proc.stdout.lines.put(
                        "bwrap: Creating new namespace failed: Operation not permitted\n"
                    )
                    proc.exit(1)
                return proc

            sup = rc.Supervisor(
                "codex",
                Path(tmp) / "state",
                dict,
                lambda: "Home Assistant",
                popen=popen,
                control_factory=FakeControl,
                run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
            )
            try:
                sup.start()
                self.assertTrue(wait_for(lambda: len(commands) == 2))
                self.assertEqual(
                    commands[1][:5],
                    [rc.RELAY_LAUNCHER, "--plain", "codex", "app-server", "--listen"],
                )
                self.assertEqual(commands[1][-1], "--remote-control")
                self.assertEqual(sup.snapshot()["restarts"], 0)
                self.assertEqual(sup.snapshot()["name_state"], "namespace-unavailable")
            finally:
                sup.stop()
