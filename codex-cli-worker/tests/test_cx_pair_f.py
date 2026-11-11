"""The control socket is masked from commands and pairing belongs to administrators."""
from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remote_control as rc
import test_remote_control as base

WORKER = Path(__file__).resolve().parents[1]
FIXTURE_CODE = "TEST-0000"
CONTROL_NAME = "remote_control_control"


class PrivateControlTests(unittest.TestCase):
    def test_socket_has_one_fixed_private_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            commands = []
            def popen(command, **kwargs):
                commands.append(command)
                return base.FakeProc()
            sup = rc.Supervisor("codex", Path(tmp) / "state", dict, lambda: "",
                                gate=lambda: "", popen=popen, control_factory=base.FakeControl)
            self.addCleanup(sup.stop)
            sup.start()
            self.assertTrue(base.wait_for(lambda: sup.snapshot()["state"] == "ready"))
            parent = Path(tmp) / CONTROL_NAME
            self.assertEqual(sup.control_socket.parent.parent, parent)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sup.control_socket.parent.stat().st_mode & 0o777, 0o700)

    def test_pair_reports_starting_instead_of_telling_enabled_owner_to_turn_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = rc.Supervisor("codex", Path(tmp) / "state", dict, lambda: "", gate=lambda: "")
            sup.save_enabled(True)
            ok, result = sup.pair()
            self.assertFalse(ok)
            self.assertIn("starting", result["error"].lower())

    def test_enable_readiness_timeout_is_shorter_than_pair_generation(self):
        client = rc.ControlClient(Path("/fixture"))
        with patch.object(client, "_request", return_value={}) as request:
            client.enable()
        self.assertLessEqual(request.call_args.args[2], 5.0)

    def test_wrapper_masks_control_parent_after_every_caller_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden = root / CONTROL_NAME
            hidden.mkdir()
            record = root / "argv.json"
            real = root / "real"
            real.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n")
            real.chmod(0o755)
            wrapper = root / "wrapper"
            source = (WORKER / "codex-bwrap").read_text()
            source = source.replace("REAL_BWRAP=/opt/codex-sandbox/bwrap.real", f"REAL_BWRAP={real}")
            source = source.replace("/data/remote_control_control", str(hidden))
            wrapper.write_text(source)
            wrapper.chmod(0o755)
            subprocess.run([str(wrapper), "--ro-bind", "/", "/", "--bind", str(hidden), str(hidden),
                            "--", "/bin/true"], check=True, timeout=5)
            argv = json.loads(record.read_text())
            stop = argv.index("--")
            self.assertEqual(argv[stop - 2:stop], ["--tmpfs", str(hidden)])
            self.assertGreater(stop - 2, argv.index("--bind"))

    def test_real_sandbox_cannot_connect_to_control_socket(self):
        real = shutil.which("bwrap")
        if real is None:
            self.skipTest("Bubblewrap is unavailable; runtime/lab connect proof remains required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden = root / CONTROL_NAME
            hidden.mkdir(mode=0o700)
            path = hidden / "control.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            try:
                listener.bind(str(path))
            except PermissionError:
                self.skipTest("local Unix socket binding is blocked by the outer sandbox; run this test outside it")
            listener.listen(4)
            program = ("import socket,sys; s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); "
                       "s.connect(sys.argv[1]); s.close(); sys.exit(9)")
            args = ["--unshare-user", "--ro-bind", "/", "/", "--", sys.executable, "-c", program, str(path)]
            control = subprocess.run([real, *args], capture_output=True, timeout=10)
            if control.returncode != 9:
                self.skipTest("unmasked Bubblewrap connect is unavailable on this runtime; lab proof remains required")
            wrapper = root / "wrapper"
            source = (WORKER / "codex-bwrap").read_text()
            source = source.replace("REAL_BWRAP=/opt/codex-sandbox/bwrap.real", f"REAL_BWRAP={real}")
            source = source.replace("/data/remote_control_control", str(hidden))
            wrapper.write_text(source)
            wrapper.chmod(0o755)
            result = subprocess.run([str(wrapper), *args], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 9, "sandbox connected to the worker control socket")
            self.assertIn(b"No such file or directory", result.stderr)


class AdministratorPairingTests(unittest.TestCase):
    def setUp(self):
        self.server = base.server
        self.client = self.server.app.test_client()
        self.state = {"state": "ready", "pairing_code": FIXTURE_CODE, "pairing_qr": "data:image/svg+xml;base64,fixture"}
        self.patches = [
            patch.object(self.server, "api_token", return_value="fixture"),
            patch.object(self.server.remote_control_supervisor, "snapshot", return_value=self.state),
            patch.object(self.server, "lookup_ha_users", return_value=[
                {"id": "admin-fixture", "is_active": True, "system_generated": False, "group_ids": ["system-admin"]},
                {"id": "member-fixture", "is_active": True, "system_generated": False, "group_ids": []},
            ]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        with self.server.admin_cache_lock:
            self.server.admin_cache.clear()

    def ingress(self, user=""):
        headers = {"X-Ingress-Path": "/api/hassio_ingress/fixture"}
        if user:
            headers["X-Remote-User-Id"] = user
        return {"headers": headers, "environ_overrides": {"REMOTE_ADDR": self.server.INGRESS_PROXY_IP}}

    def test_each_mutator_refuses_missing_nonadmin_or_unverified_identity(self):
        for user in ("", "member-fixture", "unknown-fixture"):
            for action in ("pair", "start", "stop", "review"):
                with self.subTest(user=user, action=action), patch.object(self.server.remote_control_supervisor, action) as call:
                    call.return_value = (True, self.state) if action in ("pair", "start") else self.state
                    response = self.client.post("/remote-control/" + action, json={}, **self.ingress(user))
                    self.assertEqual(response.status_code, 403)
                    call.assert_not_called()

    def test_administrator_can_use_each_mutator(self):
        for action in ("pair", "start", "stop", "review"):
            with self.subTest(action=action), patch.object(self.server.remote_control_supervisor, action) as call:
                call.return_value = (True, self.state) if action in ("pair", "start") else self.state
                response = self.client.post("/remote-control/" + action, json={}, **self.ingress("admin-fixture"))
                self.assertEqual(response.status_code, 200)
                call.assert_called_once()

    def test_only_confirmed_ingress_administrator_reads_code_and_qr(self):
        for user in ("", "member-fixture", "unknown-fixture"):
            with self.subTest(user=user):
                state = self.client.get("/remote-control", **self.ingress(user)).get_json()["remote_control"]
                self.assertNotIn("pairing_code", state)
                self.assertNotIn("pairing_qr", state)
        state = self.client.get("/remote-control", **self.ingress("admin-fixture")).get_json()["remote_control"]
        self.assertEqual(state["pairing_code"], FIXTURE_CODE)
        self.assertTrue(state["pairing_qr"])

    def test_lookup_failure_never_reveals_code_or_changes_state(self):
        with patch.object(self.server, "lookup_ha_users", side_effect=RuntimeError("fixture unavailable")), patch.object(self.server.remote_control_supervisor, "pair") as pair:
            state = self.client.get("/remote-control", **self.ingress("admin-fixture")).get_json()["remote_control"]
            self.assertNotIn("pairing_code", state)
            self.assertEqual(self.client.post("/remote-control/pair", json={}, **self.ingress("admin-fixture")).status_code, 403)
            pair.assert_not_called()

    def test_token_read_never_calls_admin_lookup_or_shows_code(self):
        with patch.object(self.server, "lookup_ha_users") as lookup:
            response = self.client.get("/remote-control", headers={"Authorization": "Bearer fixture"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pairing_code", response.get_json()["remote_control"])
        lookup.assert_not_called()
