"""Proof P8 of the long-lived Codex design: a command in Codex's sandbox cannot reach the worker's sockets.

The worker serves its tools on Unix sockets under /data (server.py, the `.serve(DATA_ROOT / ...)` lines). Today a
key in each tool program's environment proves that a request comes from a tool program. The design drops the
keys and relies on Codex's sandbox instead: with no network, the sandbox denies `connect` and `sendto`, also on
a Unix socket. This test runs that claim on the pinned Codex CLI, for every socket the worker serves:

- Read only, Default and Auto-review: a shell command cannot connect to any socket, and no socket accepts.
- Full access (no sandbox): the command can connect, when the managed requirements allow the mode at all.
- An escalated command (review F1): in Default and Auto-review, a command may ask to run outside the sandbox
  (`require_escalated`). The test approves it, as a person or Codex's reviewer could, and records what it can
  reach. Outside the sandbox there is no seccomp filter, so the expected answer is "every socket". This is the
  case that needs the owner's answer before the keys go (D-CX-KEYS); the test makes it visible and keeps it so.
- A control: the same probe run directly, outside Codex, reaches every socket, so a refusal above is the
  sandbox and not a broken probe.

It runs in the Verification checks container job, and by hand with HA_TEST_CODEX (SANDBOX_TESTING.md). It skips
without the pinned CLI, except where HA_TEST_REQUIRE_CODEX=1 (the container job) makes that a failure, so a later
move of the Codex pin must run it again.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import re
import shlex
import socketserver
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live_proofs import (
    THREAD,
    WORKER,
    AppServer,
    ScriptedModel,
    allow_local_sockets,
    needs_cli,
)

REQUIREMENTS = Path("/etc/codex/requirements.toml")


def worker_socket_names() -> list[str]:
    """Return the file name of every socket the worker serves, read from server.py's start-up lines."""
    source = (WORKER / "server.py").read_text(encoding="utf-8")
    names = re.findall(r'\.serve\(DATA_ROOT / "([^"]+\.sock)"\)', source)
    assert len(names) >= 6, names
    return names


PROBE = r"""
import errno, json, socket, sys
result = {}
for path in sys.argv[1:]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(path)
        client.sendall(b'{"probe": true}\n')
        result[path] = "connected"
    except OSError as error:
        result[path] = errno.errorcode.get(error.errno, str(error.errno))
    finally:
        client.close()
print("PROBE " + json.dumps(result))
"""


class Listener(socketserver.ThreadingUnixStreamServer):
    """A stream socket at one of the worker's paths that counts each connection it accepts."""

    def __init__(self, path: Path) -> None:
        self.accepted = 0
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            """Count the connection, read the probe's line, then close it.

            The probe writes one line after it connects. A close before that write makes the write fail with
            EPIPE, and the probe would report a connection that was accepted as not connected.
            """

            def handle(self) -> None:
                outer.accepted += 1
                self.request.settimeout(10)
                with (
                    contextlib.suppress(OSError),
                    self.request.makefile("rb") as stream,
                ):
                    stream.readline()

        super().__init__(str(path), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class ListenerReadsTheProbe(unittest.TestCase):
    """A connection the stand-in accepted always reads as connected, even when the probe writes late.

    On slow runners the listener used to close a connection before the probe wrote its line, and the probe then
    failed with EPIPE. Here the probe waits between connect and write, which made it fail every time before the
    listener read the line. No Codex CLI is needed.
    """

    def test_a_slow_probe_still_reads_as_connected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ha-socket-listener-") as folder:
            paths = [Path(folder) / f"stand-in-{number}.sock" for number in range(3)]
            listeners = [Listener(path) for path in paths]
            slow_probe = PROBE.replace(
                "client.connect(path)\n",
                "client.connect(path)\n        __import__('time').sleep(0.05)\n",
            )
            self.assertNotEqual(slow_probe, PROBE)
            try:
                output = subprocess.run(
                    [sys.executable, "-c", slow_probe, *map(str, paths)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                ).stdout
            finally:
                for listener in listeners:
                    listener.shutdown()
                    listener.server_close()
        result = json.loads(output.split("PROBE ", 1)[1])
        self.assertEqual(result, {str(path): "connected" for path in paths})
        self.assertEqual(sum(listener.accepted for listener in listeners), len(paths))


def full_access_allowed() -> bool:
    """Return False when the image's managed requirements leave out the full-access sandbox."""
    if not REQUIREMENTS.is_file():
        return True
    return "danger-full-access" in REQUIREMENTS.read_text(encoding="utf-8")


@needs_cli
class SandboxSockets(unittest.TestCase):
    """P8: run one probe command through Codex in each mode and read what it reached."""

    @classmethod
    def setUpClass(cls) -> None:
        allow_local_sockets()
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="ha-sandbox-sockets-", dir=cache
        )
        cls.root = Path(cls.temporary.name)
        (cls.root / "workspace").mkdir()
        data = cls.root / "data"
        data.mkdir()
        cls.listeners = {name: Listener(data / name) for name in worker_socket_names()}
        cls.paths = [str(data / name) for name in cls.listeners]
        probe = cls.root / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        cls.command = " ".join(
            shlex.quote(part) for part in [sys.executable, str(probe), *cls.paths]
        )
        cls.model = ScriptedModel()

    @classmethod
    def tearDownClass(cls) -> None:
        for listener in cls.listeners.values():
            listener.shutdown()
            listener.server_close()
        cls.model.close()
        cls.temporary.cleanup()

    def setUp(self) -> None:
        # The class's servers live through every test; their accepted sockets need the block lifted each time.
        allow_local_sockets()

    def accepted(self) -> int:
        """Return the connections all listeners accepted so far."""
        return sum(listener.accepted for listener in self.listeners.values())

    def run_probe(
        self,
        label: str,
        thread: dict[str, Any],
        turn: dict[str, Any],
        escalate: bool = False,
    ) -> tuple[dict[str, str] | None, list[str]]:
        """Run the probe as one shell command in a turn; return what it printed and the requests Codex sent."""
        app = AppServer(self.root, self.model)
        try:
            app.initialize()
            params = {**THREAD, "cwd": str(self.root / "workspace"), **thread}
            thread_id = app.request("thread/start", params)["thread"]["id"]
            name = "p8" + re.sub(r"\W", "", label)
            step: dict[str, Any] = {"shell": self.command}
            if escalate:
                step.update(
                    sandbox_permissions="require_escalated",
                    justification="Run the socket probe once",
                )
            self.model.scripts[name] = [step, {"final": label}]
            done = app.turn(thread_id, f"[script:{name}] probe", **turn)
            self.assertEqual(
                done["params"]["turn"]["status"], "completed", (label, done)
            )
            outputs = [
                item
                for item in self.model.requests_for(name)[-1]["input"]
                if str(item.get("type", "")).endswith("_output")
            ]
            text = json.dumps(outputs)
            match = re.search(r"PROBE (\{.*?\})", text.replace('\\"', '"'))
            return (json.loads(match.group(1)) if match else None), [
                method for method, _ in app.requests_seen
            ]
        finally:
            app.close()

    def sandboxed(
        self, label: str, thread: dict[str, Any], turn: dict[str, Any]
    ) -> None:
        """Assert that the probe ran in one sandbox mode and that the sandbox refused every socket with EPERM.

        A probe that printed nothing did not run, which proves nothing about the sandbox, so it fails the test.
        """
        before = self.accepted()
        result, _requests = self.run_probe(label, thread, turn)
        self.assertIsNotNone(
            result,
            f"{label}: the probe printed nothing, so it did not run in the sandbox",
        )
        self.assertEqual(set(result), set(self.paths), (label, result))
        self.assertEqual(set(result.values()), {"EPERM"}, (label, result))
        self.assertEqual(
            self.accepted(), before, f"{label}: a socket accepted a connection"
        )
        print(
            f"\nP8 {label}: the sandbox refused all {len(self.paths)} sockets with EPERM."
        )

    def workspace_policy(self) -> dict[str, Any]:
        """Return the workspaceWrite policy the worker sends, with the test's folder as the writable root."""
        return {
            "type": "workspaceWrite",
            "networkAccess": False,
            "writableRoots": [str(self.root / "workspace")],
        }

    def test_control_the_probe_reaches_every_socket_outside_codex(self) -> None:
        before = self.accepted()
        output = subprocess.run(
            self.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        result = json.loads(output.split("PROBE ", 1)[1])
        self.assertEqual(set(result.values()), {"connected"}, result)
        self.assertEqual(self.accepted() - before, len(self.paths))

    def test_read_only_reaches_no_socket(self) -> None:
        self.sandboxed(
            "Read only",
            {"sandbox": "read-only"},
            {
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
        )

    def test_default_reaches_no_socket(self) -> None:
        self.sandboxed(
            "Default",
            {"sandbox": "workspace-write"},
            {
                "sandboxPolicy": self.workspace_policy(),
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
        )

    def test_auto_review_reaches_no_socket(self) -> None:
        self.sandboxed(
            "Auto-review",
            {"sandbox": "workspace-write", "approvalsReviewer": "auto_review"},
            {
                "sandboxPolicy": self.workspace_policy(),
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
            },
        )

    @unittest.skipUnless(
        full_access_allowed(),
        "the managed requirements leave out the full-access sandbox",
    )
    def test_full_access_reaches_every_socket(self) -> None:
        before = self.accepted()
        result, _requests = self.run_probe(
            "Full access",
            {"sandbox": "danger-full-access", "approvalPolicy": "never"},
            {"sandboxPolicy": {"type": "dangerFullAccess"}, "approvalPolicy": "never"},
        )
        self.assertIsNotNone(result)
        self.assertEqual(set(result.values()), {"connected"}, result)
        self.assertEqual(self.accepted() - before, len(self.paths))
        print(
            f"\nP8 Full access: the probe reached all {len(self.paths)} sockets, as expected with no sandbox."
        )

    def test_escalated_command_in_default_reaches_every_socket(self) -> None:
        """Review F1: an approved escalated command runs outside the sandbox. Recorded for D-CX-KEYS."""
        before = self.accepted()
        result, requests = self.run_probe(
            "Default escalated",
            {"sandbox": "workspace-write"},
            {
                "sandboxPolicy": self.workspace_policy(),
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
            escalate=True,
        )
        self.assertIn("item/commandExecution/requestApproval", requests)
        self.assertIsNotNone(result, "the approved escalated command printed nothing")
        self.assertEqual(set(result.values()), {"connected"}, result)
        self.assertEqual(self.accepted() - before, len(self.paths))
        print(
            f"\nP8 Default, escalated and approved by the person: the command reached all {len(self.paths)} "
            "sockets. Without the keys, an approved escalated command can call any tool socket (D-CX-KEYS)."
        )

    def escalated_auto_review(self, outcome: str) -> tuple[dict[str, str] | None, int]:
        """Run the escalated probe in Auto-review with the reviewer's answer set; return its output and reach."""
        before = self.accepted()
        reviews = len(self.model.reviews)
        self.model.review_outcome = outcome
        try:
            result, requests = self.run_probe(
                f"Auto-review escalated {outcome}",
                {"sandbox": "workspace-write", "approvalsReviewer": "auto_review"},
                {
                    "sandboxPolicy": self.workspace_policy(),
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "auto_review",
                },
                escalate=True,
            )
        finally:
            self.model.review_outcome = "deny"
        # Codex's reviewer decides; the person is not asked.
        self.assertGreater(
            len(self.model.reviews), reviews, "Codex's reviewer was not asked"
        )
        self.assertNotIn("item/commandExecution/requestApproval", requests)
        return result, self.accepted() - before

    def test_escalated_command_in_auto_review_allowed_by_the_reviewer(self) -> None:
        """Review F1: in Auto-review, Codex's reviewer decides the escalation. Recorded for D-CX-KEYS."""
        result, reached = self.escalated_auto_review("allow")
        self.assertIsNotNone(result, "the allowed escalated command printed nothing")
        self.assertEqual(set(result.values()), {"connected"}, result)
        self.assertEqual(reached, len(self.paths))
        print(
            f"\nP8 Auto-review, escalated and allowed by Codex's reviewer: the command reached all "
            f"{len(self.paths)} sockets, with no person asked (D-CX-KEYS)."
        )

    def test_escalated_command_in_auto_review_denied_by_the_reviewer(self) -> None:
        result, reached = self.escalated_auto_review("deny")
        self.assertIsNone(result, "a denied escalated command ran")
        self.assertEqual(reached, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
