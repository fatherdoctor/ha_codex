"""OpenAI's remote control for Codex, run by the app as one supervised foreground process.

The ChatGPT app can drive Codex on this machine through OpenAI's relay
(``codex remote-control``). The app runs it in the foreground
(``--no-daemon``), restarts it when it exits, and shows its state, its machine
name and its pairing code on the app page. Nothing starts until the owner turns
it on, and the choice survives restarts.

Commands that arrive from the ChatGPT app do not pass through this app's
approval buttons. So remote control can only be turned on when a command gate
that holds outside the model is installed in the image (``GATE_MARKER`` and the
files it names). Until then, Start is refused with a plain reason and the chat
page works as before.

Codex 0.166 has no machine-name setting and uses the host name, so the process
starts in its own user and UTS namespace (``unshare -Ur -u``) with the host name
set to the chosen name. A name the kernel or ``hostname`` refuses falls back to
``Home-Assistant``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_NAME = "Home Assistant"
FALLBACK_NAME = "Home-Assistant"
MAX_NAME_BYTES = 63
# Shipped in the image together with the command gate. It lists the files the gate needs; remote
# control can start only when the marker and every file it lists exist.
GATE_MARKER = Path("/usr/share/ha_codex/remote-control-gate.json")
GATE_MISSING = (
    "Remote control cannot be turned on in this version. Commands from the ChatGPT app skip this app's "
    "approval buttons, and the check that refuses locks, alarms, garage doors, cameras and messages to people "
    "for those commands is not installed yet. The chat page works as usual."
)
# Seconds to wait before restarting a process that exited on its own, doubling up to the cap.
BACKOFF_START = 5.0
BACKOFF_CAP = 300.0
# A process that ran this long before exiting counts as healthy: the next restart starts the backoff again.
HEALTHY_RUN = 600.0
STOP_GRACE = 10.0
PAIR_TIMEOUT = 60.0
LOG_LINES = 40
# Credentials the remote-control process must never see: commands from the ChatGPT app get no
# Home Assistant token and no Supervisor token, whatever the chat path is given.
STRIPPED_ENV = (
    "SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_TOKEN", "HA_URL",
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


def build_command(codex_binary: str, name: str, *args: str) -> list[str]:
    """Return the command that sets the host name in a private namespace and then runs Codex with these arguments."""
    script = 'hostname "$1" 2>/dev/null || hostname "$2" || exit 97; shift 2; exec "$@"'
    return ["unshare", "-Ur", "-u", "sh", "-c", script, "remote-control",
            name, fallback_name(name), codex_binary, *args]


def remote_env(base: dict[str, str]) -> dict[str, str]:
    """Return the environment for the remote-control process: the chat path's, without any credential."""
    env = dict(base)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    return env


def gate_problem(marker: Path | None = None) -> str:
    """Return why remote control may not start, or an empty string when the command gate is installed."""
    marker = marker or GATE_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GATE_MISSING
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files):
        return GATE_MISSING
    missing = [item for item in files if not Path(item).is_file()]
    if missing:
        return GATE_MISSING + " (missing: " + ", ".join(missing) + ")"
    return ""


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
    qr = QRCode(border=2)
    qr.add_data(text)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(image_factory=SvgPathFillImage).save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class Supervisor:
    """Own the one remote-control process: start, stop, restart with backoff, and report its state."""

    def __init__(
        self,
        codex_binary: str,
        state_path: Path,
        env_factory: Callable[[], dict[str, str]],
        name_factory: Callable[[], Any],
        gate: Callable[[], str] = gate_problem,
        popen: Callable[..., Any] = subprocess.Popen,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.codex_binary = codex_binary
        self.state_path = state_path
        self.env_factory = env_factory
        self.name_factory = name_factory
        self.gate = gate
        self.popen = popen
        self.run = run
        self.clock = clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.proc: Any = None
        self.thread: threading.Thread | None = None
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self.status: dict[str, Any] = {"state": "off", "message": "Remote control is off.", "pairing_code": "",
                                       "machine_name": "", "restarts": 0, "since": None}

    # Saved choice -------------------------------------------------------------------------------

    def enabled(self) -> bool:
        """Return whether the owner turned remote control on; off when nothing was saved."""
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("enabled") is True
        except (OSError, ValueError, AttributeError):
            return False

    def save_enabled(self, value: bool) -> None:
        """Save the owner's choice so it holds across app restarts."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": bool(value)}), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # Actions ------------------------------------------------------------------------------------

    def resume(self) -> dict[str, Any]:
        """At app start, run remote control again if the owner left it on and the gate allows it."""
        if not self.enabled():
            return self.snapshot()
        problem = self.gate()
        if problem:
            self.set_status("blocked", problem)
            return self.snapshot()
        return self._launch()

    def start(self) -> tuple[bool, dict[str, Any]]:
        """Turn remote control on; refused, and left off, while the command gate is missing."""
        problem = self.gate()
        if problem:
            self.set_status("blocked", problem)
            return False, self.snapshot()
        self.save_enabled(True)
        return True, self._launch()

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
            running = self.proc is not None and self.proc.poll() is None
        if not running:
            return False, {**self.snapshot(), "error": "Turn remote control on first."}
        name = machine_name(self.name_factory())
        try:
            result = self.run(build_command(self.codex_binary, name, "remote-control", "pair"),
                              env=remote_env(self.env_factory()), capture_output=True, text=True,
                              timeout=PAIR_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, {**self.snapshot(), "error": f"Codex could not start pairing: {exc}"}
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        code = ""
        for line in output.splitlines():
            code = self._parse(line) or code
        if not code:
            return False, {**self.snapshot(), "error": "Codex gave no pairing code. Its last lines are in the log below."}
        return True, self.snapshot()

    # Process ------------------------------------------------------------------------------------

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
            problem = self.gate()
            if problem:
                self.set_status("blocked", problem)
                return
            name = machine_name(self.name_factory())
            started = self.clock()
            try:
                proc = self.popen(build_command(self.codex_binary, name, "remote-control", "--no-daemon"),
                                  env=remote_env(self.env_factory()), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  start_new_session=True)
            except OSError as exc:
                self.set_status("error", f"Remote control could not start: {exc}")
                proc = None
            if proc is not None:
                with self.lock:
                    self.proc = proc
                self.set_status("connecting", f"Connecting as “{name}”…", machine_name=name)
                for line in iter(proc.stdout.readline, ""):
                    self._parse(line)
                code = proc.wait()
                with self.lock:
                    self.proc = None
                if self.wake.is_set() or not self.enabled():
                    return
                if code == 97:
                    self.set_status("error", "The machine name could not be set, not even as Home-Assistant.")
                else:
                    self.set_status("error", f"Remote control stopped (exit code {code}); restarting.")
            if self.clock() - started >= HEALTHY_RUN:
                backoff = BACKOFF_START
            with self.lock:
                self.status["restarts"] += 1
            if self.wake.wait(backoff):
                return
            backoff = min(backoff * 2, BACKOFF_CAP)

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
        code_match = PAIRING_RE.search(line)
        code = code_match.group(1) if code_match else ""
        with self.lock:
            self.log.append(redact(line, keep=code))
        if code:
            self.set_status("pairing", "Enter this pairing code in the ChatGPT app.", pairing_code=code)
            return code
        available = AVAILABLE_RE.search(line)
        if available:
            self.set_status("ready", f"Available in the ChatGPT app as “{available.group(1)}”.",
                            machine_name=available.group(1))
        elif ERROR_RE.search(line) and self.status.get("state") not in {"ready"}:
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
        problem = self.gate()
        code = status.get("pairing_code") or ""
        return {
            **status,
            "enabled": self.enabled(),
            "available": not problem,
            "blocked_reason": problem,
            "configured_name": machine_name(self.name_factory()),
            "pairing_qr": qr_data_url(code) if code else "",
            "log": log,
        }
