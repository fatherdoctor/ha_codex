"""OpenAI's remote control for Codex, run by the app as one supervised foreground process.

The ChatGPT app can drive Codex on this machine through OpenAI's relay
(``codex app-server --remote-control``). The app runs it in the foreground
with its own private Unix control socket, restarts it when it exits, and shows its state, its machine
name and its pairing code on the app page. Nothing starts until the owner turns
it on, and the choice survives restarts.

Commands that arrive from the ChatGPT app do not pass through this app's
approval buttons. They are held by Codex's own approval mode and sandbox, the
same as on the chat page, and by Codex's managed requirements in
``/etc/codex/requirements.toml``. The app adds no command check of its own.

Codex 0.166 uses the host name. Bubblewrap sets it inside a private UTS
namespace through the existing capability-bearing child profile. If that
setup fails, Codex starts without the custom name and the page explains why.
"""

from __future__ import annotations

import base64
import io
import stat
import json
import urllib.parse
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import websocket

CONTROL_FOLDER = "remote_control_control"
DEFAULT_NAME = "Home Assistant"
FALLBACK_NAME = "Home-Assistant"
MAX_NAME_BYTES = 63
# Seconds to wait before restarting a process that exited on its own, doubling up to the cap.
BACKOFF_START = 5.0
BACKOFF_CAP = 300.0
# A process that ran this long before exiting counts as healthy: the next restart starts the backoff again.
HEALTHY_RUN = 600.0
STOP_GRACE = 10.0
LOG_LINES = 40
# Keep the chat path's explicitly configured Core API pair. Supervisor tokens
# and per-turn MCP capabilities never belong to a relay process.
STRIPPED_ENV = (
    "SUPERVISOR_TOKEN", "HASSIO_TOKEN",
    "HA_VERIFICATION_CAPABILITY", "HA_DATA_CAPABILITY", "HA_DATA_SOCKET",
)

PAIRING_RE = re.compile(r"Pairing code:\s*([A-Za-z0-9][A-Za-z0-9-]{3,31})")
AVAILABLE_RE = re.compile(r"available for remote control as\s+(.+?)\.?\s*$")
ERROR_RE = re.compile(r"connection is errored|pairing failed|auth failed|error", re.IGNORECASE)
# Long opaque strings (tokens, keys) are hidden from the log shown on the page.
SECRET_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{32,}")


def machine_name(value: Any) -> str:
    """Return the name to show in the ChatGPT app: the option, cleaned, or "Home Assistant" when it is empty.

    Letters, digits, spaces, dots, dashes and underscores are kept; anything
    else becomes a space, runs of spaces collapse, and the result is cut to 63
    bytes, the longest host name Linux accepts.
    """
    text = re.sub(r"[^A-Za-z0-9 ._-]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text.encode("ascii")[:MAX_NAME_BYTES].decode("ascii").rstrip(" .-_") or DEFAULT_NAME


def fallback_name(name: str) -> str:
    """Return the host-name-safe spelling of a name: spaces and dots become dashes."""
    text = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")
    return text[:MAX_NAME_BYTES].rstrip("-") or FALLBACK_NAME


RELAY_LAUNCHER = "/opt/codex-sandbox/relay-launcher"
RELAY_BWRAP = "/opt/codex-sandbox/relay-bwrap"


def build_command(codex_binary: str, name: str, *args: str) -> list[str]:
    """Enter the relay label, preserve device nodes, then drop namespace setup caps."""
    return [RELAY_LAUNCHER, RELAY_BWRAP, "--unshare-user", "--uid", "1000", "--gid", "1000", "--unshare-uts",
            "--hostname", name, "--bind", "/", "/", "--dev", "/dev", "--bind-try", "/dev/shm", "/dev/shm",
            "--cap-drop", "ALL", "--", codex_binary, *args]


def prepare_control_parent(data_root: Path) -> Path:
    """Create the fixed mask anchor before any task can start, including with RC off."""
    parent = data_root / CONTROL_FOLDER
    parent.mkdir(mode=0o700, exist_ok=True)
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise OSError("The private control folder must be owned by the worker with mode 0700.")
    return parent


def remote_env(base: dict[str, str]) -> dict[str, str]:
    """Keep the configured Core API pair, stripping Supervisor and per-turn credentials."""
    env = dict(base)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    return env


# The tool keys a command must never see; server.SHELL_EXCLUDED_ENV sets the same pattern on each launch,
# and the managed config (/etc/codex/managed_config.toml) sets it above every client setting.
TOOL_KEY_EXCLUDE = "HA_*_CAPABILITY"


def redact(line: str, keep: str = "") -> str:
    """Hide long opaque strings in one output line, keeping the pairing code readable."""
    def hide(match: re.Match[str]) -> str:
        return match.group(0) if keep and match.group(0) == keep else "[hidden]"
    return SECRET_RE.sub(hide, line.rstrip("\r\n"))


def qr_data_url(text: str) -> str:
    """Return a QR code for the text as a data: URL of an SVG with a white background; empty if qrcode is missing."""
    try:
        from qrcode import QRCode
        from qrcode.image.svg import SvgPathFillImage
    except ImportError:
        return ""
    qr = QRCode(border=4)
    qr.add_data(text)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(image_factory=SvgPathFillImage).save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class ControlError(Exception):
    """An unavailable or unsupported control operation; never include a raw relay response."""


class ControlClient:
    """Codex 0.166 JSON-RPC over WebSocket on this process's private Unix socket.

    Upstream rust-v0.166.0: app-server-daemon/src/client.rs:63-140 and
    remote_control_client.rs:60-140. Initialize experimentalApi, then use the
    same ephemeral enable and manual-code request as the supported CLI client.
    """

    def __init__(self, path: Path, connector: Callable[..., Any] | None = None) -> None:
        self.path = path
        self.connector = connector or self._connect

    @staticmethod
    def _connect(path: Path) -> Any:
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(1.0)
        try:
            raw.connect(str(path))
            return websocket.create_connection("ws://localhost/", socket=raw, timeout=1.0,
                                               suppress_origin=True)
        except Exception:
            raw.close()
            raise

    def _request(self, method: str, params: dict[str, Any], timeout: float,
                 cancelled: Callable[[], bool]) -> dict[str, Any]:
        transport = None
        deadline = time.monotonic() + timeout
        try:
            if cancelled():
                raise ControlError("Remote control stopped during this request.")
            transport = self.connector(self.path)
            transport.settimeout(1.0)

            def request(identifier: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                transport.send(json.dumps({"id": identifier, "method": name, "params": arguments}))
                while time.monotonic() < deadline and not cancelled():
                    try:
                        frame = transport.recv()
                    except (TimeoutError, websocket.WebSocketTimeoutException):
                        continue
                    if not frame or len(frame) > 1024 * 1024:
                        raise ControlError("The Codex control connection closed or returned an oversized response.")
                    message = json.loads(frame)
                    if not isinstance(message, dict) or message.get("id") != identifier:
                        continue  # Status notifications are not replies to this request.
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise ControlError("This Codex control request was refused or is unsupported.")
                    return message["result"]
                raise ControlError("The Codex control request timed out or remote control stopped.")

            request(1, "initialize", {"clientInfo": {"name": "ha_codex", "version": "0.166.0"},
                                      "capabilities": {"experimentalApi": True}})
            transport.send(json.dumps({"method": "initialized"}))
            return request(2, method, params)
        except ControlError:
            raise
        except Exception:
            raise ControlError("Could not reach this process's Codex control socket.") from None
        finally:
            if transport is not None:
                transport.close(timeout=0)

    def enable(self, cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        return self._request("remoteControl/enable", {"ephemeral": True}, 5.0, cancelled)

    def status(self, cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        return self._request("remoteControl/status/read", {}, 5.0, cancelled)

    def pair(self, cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        reply = self._request("remoteControl/pairing/start", {"manualCode": True}, 35.0, cancelled)
        # The opaque pairingCode is for QR clients, not a code a person can type.
        return {"manualPairingCode": reply.get("manualPairingCode"), "pairingCode": reply.get("pairingCode")}


class Supervisor:
    """Own the one remote-control process: start, stop, restart with backoff, and report its state."""

    def __init__(
        self,
        codex_binary: str,
        state_path: Path,
        env_factory: Callable[[], dict[str, str]],
        name_factory: Callable[[], Any],
        popen: Callable[..., Any] = subprocess.Popen,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        control_factory: Callable[[Path], Any] = ControlClient,
        instructions_factory: Callable[[], str] = lambda: "",
        tool_args_factory: Callable[[], list[str]] = lambda: [],
        launch_env_factory: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        """Keep the factories and fakes the process needs.

        env_factory gives the environment of the probe (the namespace check), which
        runs for every Start request. launch_env_factory, when given, gives the environment of a real launch
        only; it may issue keys, because a probe or a second Start never calls it. Without it, env_factory
        serves both.
        """
        self.control_factory = control_factory
        self.instructions_factory = instructions_factory
        self.tool_args_factory = tool_args_factory
        self.control_socket: Path | None = None
        self.codex_binary = codex_binary
        self.state_path = state_path
        self.env_factory = env_factory
        self.launch_env_factory = launch_env_factory or env_factory
        self.name_factory = name_factory
        self.popen = popen
        self.run = run
        self.clock = clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.proc: Any = None
        self.thread: threading.Thread | None = None
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self.status: dict[str, Any] = {"state": "off", "message": "Remote control is off.", "pairing_code": "",
                                       "machine_name": "", "restarts": 0, "since": None,
                                       "name_state": "unchecked", "name_warning": "", "policy_warning": ""}
        self._name_supported: bool | None = None
        self._checked_name = ""

    # Saved choice -------------------------------------------------------------------------------

    def enabled(self) -> bool:
        """Return whether the owner turned remote control on; off when nothing was saved."""
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("enabled") is True
        except (OSError, ValueError, AttributeError):
            return False

    def save_enabled(self, value: bool) -> None:
        """Save the owner's choice so it holds across app restarts."""
        # Output and control warnings can arrive at the same time.
        with self.lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"enabled": bool(value)}), encoding="utf-8")
            os.replace(tmp, self.state_path)

    # Actions ------------------------------------------------------------------------------------

    def resume(self) -> dict[str, Any]:
        """At app start, run remote control again if the owner left it on."""
        if not self.enabled():
            return self.snapshot()
        return self._launch()

    def start(self) -> dict[str, Any]:
        """Turn remote control on and save the choice."""
        self._name_supported = None
        self.save_enabled(True)
        return self._launch()

    def stop(self) -> dict[str, Any]:
        """Turn remote control off and end the process; the choice is saved before the process is asked to stop."""
        self.save_enabled(False)
        self.wake.set()
        with self.lock:
            proc, thread = self.proc, self.thread
        self._terminate(proc)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STOP_GRACE + 10)
        self.set_status("off", "Remote control is off.", pairing_code="")
        return self.snapshot()

    def pair(self) -> tuple[bool, dict[str, Any]]:
        """Ask Codex for a new pairing code, for when the ChatGPT app has lost this machine."""
        with self.lock:
            proc, path = self.proc, self.control_socket
            self.status["pairing_code"] = ""
            self.status["pairing_url"] = ""
        if not self.enabled():
            return False, {**self.snapshot(), "error": "Turn remote control on first."}
        if proc is None or proc.poll() is not None or path is None:
            return False, {**self.snapshot(), "error": "Remote control is still starting or reconnecting. Wait for the connected state, then try again."}
        cancelled = lambda: self.wake.is_set() or proc.poll() is not None or not self.enabled()
        try:
            reply = self.control_factory(path).pair(cancelled=cancelled)
            code = reply.get("manualPairingCode")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,63}", code):
                raise ControlError("Codex returned no code that can be typed in the ChatGPT app.")
            with self.lock:
                if self.proc is not proc or self.control_socket != path or cancelled():
                    raise ControlError("Remote control restarted or stopped while pairing.")
                self.status["pairing_code"] = code
                opaque = reply.get('pairingCode')
                url = ('https://chatgpt.com/codex/pair?pairing_code=' + urllib.parse.quote(opaque, safe='')) \
                    if isinstance(opaque, str) and 0 < len(opaque) <= 2048 else ''
                self.status['pairing_url'] = url if len(url.encode('utf-8')) <= 2048 else ''
                self.log.append("A new pairing code is ready on this page.")
            return True, self.snapshot()
        except Exception:
            # Never echo a raw RPC error: it may contain a code or relay credential.
            return False, {**self.snapshot(), "error": "Codex could not provide a new pairing code. "
                           "Check that remote control is connected, then try again."}

    # Process ------------------------------------------------------------------------------------

    def _name_failed(self) -> None:
        self._name_supported = False
        warning = "Machine-name namespace unavailable; starting without a custom machine name."
        with self.lock:
            if self.status.get("name_warning") != warning:
                self.log.append(warning)
            self.status.update(name_state="namespace-unavailable", name_warning=warning, machine_name="")

    def _command(self, *args: str) -> list[str]:
        """Probe only the namespace setup, once per chosen name; never treat a Codex exit as the probe."""
        name = machine_name(self.name_factory())
        if self._name_supported is None or self._checked_name != name:
            self._checked_name = name
            try:
                probe = self.run(build_command("/bin/true", name), env=remote_env(self.env_factory()), cwd="/config",
                                 capture_output=True, text=True, timeout=10, check=False)
                self._name_supported = probe.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._name_supported = False
            if self._name_supported:
                with self.lock:
                    self.status.update(name_state="private-uts", name_warning="")
            else:
                self._name_failed()
        instructions = self.instructions_factory()
        config = ("-c", "developer_instructions=" + json.dumps(instructions)) if instructions else ()
        arguments = (*config, *self.tool_args_factory(), *args)
        return (build_command(self.codex_binary, name, *arguments) if self._name_supported
                else [RELAY_LAUNCHER, "--plain", self.codex_binary, *arguments])

    def _launch(self) -> dict[str, Any]:
        """Start the supervising thread unless one is already running."""
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return self.snapshot()
            self.wake.clear()
            self.thread = threading.Thread(target=self._loop, name="remote-control", daemon=True)
            self.set_status("starting", "Starting remote control…", pairing_code="")
            self.thread.start()
        return self.snapshot()

    def _loop(self) -> None:
        """Run the process, read its output, and restart it with a growing delay while remote control stays on."""
        backoff = BACKOFF_START
        while self.enabled() and not self.wake.is_set():
            name = machine_name(self.name_factory())
            with self.lock:
                self.log.append("Managed policy: a disallowed relay full-access request may be downgraded to read-only; "
                                "the relay does not expose every thread warning here.")
            started = self.clock()
            private = None
            done = threading.Event()
            monitor = None
            try:
                # The fixed parent is masked in every real Codex sandbox by codex-bwrap.
                control_parent = prepare_control_parent(self.state_path.parent)
                private = tempfile.TemporaryDirectory(prefix="rc-", dir=control_parent)
                path = Path(private.name) / "rc.sock"
                # Namespace probing and environment refresh can call Supervisor.
                # Keep both outside the lock used by snapshot, reads and stop.
                command = self._command("app-server", "--listen", f"unix://{path}", "--remote-control")
                # The keys of a launch are made here and nowhere else: a probe must not end the running relay's.
                environment = remote_env(self.launch_env_factory())
                with self.lock:
                    if self.wake.is_set() or not self.enabled():
                        private.cleanup()
                        return
                    proc = self.popen(command,
                                      env=environment, cwd="/config", stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      start_new_session=True)
                    self.proc = proc
                    self.control_socket = path
                    self.status["pairing_code"] = ""
            except OSError as exc:
                self.set_status("error", f"Remote control could not start: {exc}")
                proc = None
            if proc is not None:
                self.set_status("connecting", (f"Connecting as “{name}”…" if self._name_supported else
                                               "Connecting without a custom machine name…"),
                                machine_name=name if self._name_supported else "")
                monitor = threading.Thread(target=self._monitor_control, args=(proc, path, done),
                                           name="remote-control-status", daemon=True)
                monitor.start()
                namespace_error = False
                for line in iter(proc.stdout.readline, ""):
                    if line.lstrip().startswith("bwrap:") and re.search(
                            r"namespace|hostname|Operation not permitted|Permission denied", line, re.IGNORECASE):
                        namespace_error = True
                    self._parse(line)
                code = proc.wait()
                done.set()
                monitor.join(timeout=3.0)
                with self.lock:
                    self.proc = None
                    self.control_socket = None
                    self.status["pairing_code"] = ""
                private.cleanup()
                if self.wake.is_set() or not self.enabled():
                    return
                if namespace_error and self._name_supported:
                    self._name_failed()
                    continue  # One plain launch; no namespace exit-1/backoff loop.
                self.set_status("error", f"Remote control stopped (exit code {code}); restarting.")
            if proc is None and private is not None:
                private.cleanup()
            if self.clock() - started >= HEALTHY_RUN:
                backoff = BACKOFF_START
            with self.lock:
                self.status["restarts"] += 1
            if self.wake.wait(backoff):
                return
            backoff = min(backoff * 2, BACKOFF_CAP)

    def _monitor_control(self, proc: Any, path: Path, done: threading.Event) -> None:
        """Read readiness from the owned server, not human-oriented stdout or a daemon socket."""
        cancelled = lambda: done.is_set() or self.wake.is_set() or proc.poll() is not None or not self.enabled()
        enabled = False
        while not cancelled():
            try:
                client = self.control_factory(path)
                reply = client.status(cancelled=cancelled) if enabled else client.enable(cancelled=cancelled)
                enabled = True
                with self.lock:
                    if cancelled() or self.proc is not proc:
                        return
                    state = reply.get("status")
                    if state == "connected":
                        name = machine_name(reply.get("serverName") or self.name_factory())
                        self.set_status("ready", f"Available in the ChatGPT app as “{name}”.", machine_name=name)
                    elif state == "connecting":
                        self.set_status("connecting", "Connecting remote control…")
                    else:
                        enabled = False
                        self.set_status("error", "Codex remote control is not connected. Check sign-in and try again.")
            except Exception:
                if not cancelled():
                    self.set_status("connecting", "Waiting for this process's Codex control socket…")
            if done.wait(1.0 if not enabled else 5.0):
                return

    def _terminate(self, proc: Any) -> None:
        """End the process group: Ctrl-C first, then terminate, then kill."""
        if proc is None or proc.poll() is not None:
            return
        for sig, wait in ((signal.SIGINT, STOP_GRACE), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.send_signal(sig)
                except (ProcessLookupError, OSError):
                    return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def _parse(self, line: str) -> str:
        """Record one output line and update the state from it; return a pairing code found in it."""
        lower = line.lower()
        policy_downgrade = False
        if "disallowed by requirements" in lower and "falling back to required value" in lower:
            for setting in ("approval_policy", "permission_profile"):
                if setting not in lower:
                    continue
                target = "the required approval policy" if setting == "approval_policy" else "the managed restricted profile"
                warning = f"Codex reported that a {setting} request was downgraded to {target}."
                with self.lock:
                    self.status["policy_warning"] = warning
                    self.log.append(warning)
                policy_downgrade = True
        elif ("configwarning" in lower or "warning" in lower) and (
                "read-only" in lower or "readonly" in lower) and (
                "fall" in lower or "downgrad" in lower or "not allowed" in lower):
            warning = "Codex reported that a request was downgraded to read-only by the managed policy."
            with self.lock:
                self.status["policy_warning"] = warning
                self.log.append(warning)
            policy_downgrade = True
        code_match = PAIRING_RE.search(line)
        code = code_match.group(1) if code_match else ""
        with self.lock:
            self.log.append("A pairing code is ready on this page." if code else redact(line))
        if code:
            self.set_status("pairing", "Enter this pairing code in the ChatGPT app.", pairing_code=code, pairing_url="")
            return code
        available = AVAILABLE_RE.search(line)
        if available:
            self.set_status("ready", f"Available in the ChatGPT app as “{available.group(1)}”.",
                            machine_name=available.group(1))
        elif not policy_downgrade and ERROR_RE.search(line) and self.status.get("state") not in {"ready"}:
            self.set_status("error", redact(line))
        return ""

    # State --------------------------------------------------------------------------------------

    def set_status(self, state: str, message: str, **extra: Any) -> None:
        """Replace the reported state and message, keeping the other fields unless given."""
        with self.lock:
            if self.status.get("state") != state:
                self.status["since"] = time.time()
            self.status.update(state=state, message=message, **extra)

    def snapshot(self) -> dict[str, Any]:
        """Return the state the page shows: saved choice, process state, name, pairing code and QR, recent log."""
        with self.lock:
            status = dict(self.status)
            log = list(self.log)
        code = status.get("pairing_code") or ""
        return {
            **status,
            "enabled": self.enabled(),
            # Nothing of the app's own can block remote control; the page still reads these two keys.
            "available": True,
            "blocked_reason": "",
            "configured_name": machine_name(self.name_factory()),
            "pairing_url": status.get("pairing_url", "") if code else "",
            "pairing_qr": qr_data_url(status.get("pairing_url") or code) if code else "",
            "log": log,
        }
