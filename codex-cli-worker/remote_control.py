"""OpenAI's remote control for Codex, run by the app as one supervised foreground process.

The ChatGPT app can drive Codex on this machine through OpenAI's relay
(``codex remote-control``). The app runs it in the foreground
(``--no-daemon``), restarts it when it exits, and shows its state, its machine
name and its pairing code on the app page. Nothing starts until the owner turns
it on, and the choice survives restarts.

Commands that arrive from the ChatGPT app do not pass through this app's
approval buttons. So remote control can only be turned on when a command gate
that holds outside the model is installed in the image (``GATE_MARKER`` and the
files it names) and its hook passes a self-test. Otherwise, Start is refused
with a plain reason and the chat page works as before. ``ConfigWatch`` is the
gate's detective layer: it reports refused actions that appear in ``/config``
YAML while remote control is on and that the hook could not see.

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
import sys
import tempfile
import threading
import time
import tomllib
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gate_rules

DEFAULT_NAME = "Home Assistant"
FALLBACK_NAME = "Home-Assistant"
MAX_NAME_BYTES = 63
# Shipped in the image together with the command gate. It lists the files the gate needs; remote
# control can start only when the marker and every file it lists exist.
GATE_MARKER = Path("/usr/share/ha_codex/remote-control-gate.json")
GATE_MISSING = (
    "Remote control cannot be turned on in this version. Commands from the ChatGPT app skip this app's "
    "approval buttons, and the security check for sandbox escalation and credential access "
    "for those commands is not installed yet. The chat page works as usual."
)
GATE_BROKEN = (
    "Remote control cannot be turned on: the security check for sandbox escalation and "
    "credential access for commands from the ChatGPT app is installed but does not hold: "
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


def requirements_problem(path: Path) -> str:
    """Return what is wrong with the managed requirements file, or an empty string when it holds."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"the managed requirements could not be read ({exc})"
    modes = data.get("allowed_sandbox_modes")
    if not isinstance(modes, list) or not modes or "danger-full-access" in modes:
        return "the managed requirements do not rule out the full-access sandbox"
    if data.get("allow_managed_hooks_only") is not True:
        return "the managed requirements do not keep hooks to the managed ones"
    return ""


# What the hook must refuse, and let through, before remote control may start: (event, remote on, input, refused).
SELF_TEST_CASES = (
    ("PermissionRequest", True, {"hook_event_name": "PermissionRequest", "tool_name": "shell",
                                 "tool_input": {"command": ["curl", "http://homeassistant.invalid:8123/api/"]}}, True),
    ("PreToolUse", True, {"hook_event_name": "PreToolUse", "tool_name": "shell",
                          "tool_input": {"command": ["curl", "-s", "http://homeassistant.invalid/"],
                                         "sandbox_permissions": "require_escalated"}}, True),
    ("PreToolUse", False, {"hook_event_name": "PreToolUse", "tool_name": "shell",
                           "tool_input": {"command": ["cat", "/proc/1/environ"]}}, True),
    ("PreToolUse", False, {"hook_event_name": "PreToolUse", "tool_name": "shell",
                           "tool_input": {"command": ["ls", "/config"]}}, False),
)
_self_test_cache: dict[tuple[str, float], str] = {}


def hook_problem(hook: Path, run: Callable[..., Any] = subprocess.run) -> str:
    """Run the hook on the self-test cases; return what it got wrong, or an empty string when it holds.

    The result is kept per hook file and modification time: the image's copy never changes while the app runs.
    """
    try:
        key = (str(hook), hook.stat().st_mtime)
    except OSError:
        return "the command check is missing"
    if key in _self_test_cache:
        return _self_test_cache[key]
    problem = ""
    with tempfile.TemporaryDirectory() as tmp:
        for event, remote_on, payload, refused in SELF_TEST_CASES:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"enabled": remote_on}), encoding="utf-8")
            try:
                result = run([sys.executable, "-I", str(hook), "--event", event, "--state", str(state)],
                             input=json.dumps(payload), capture_output=True, text=True, timeout=20, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                problem = f"the command check could not run ({exc})"
                break
            if (result.returncode == 2) != refused:
                problem = "the command check " + ("let through" if refused else "refused") + " a test request"
                break
    _self_test_cache[key] = problem
    return problem


def gate_problem(marker: Path | None = None) -> str:
    """Return why remote control may not start, or an empty string when the command gate is installed and holds.

    The marker must list the gate's files, every one must exist, the managed
    requirements it names must rule out full access and outside hooks, and the
    hook it names must refuse and pass the self-test requests.
    """
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
    hook, requirements = data.get("hook"), data.get("requirements")
    if not isinstance(hook, str) or not isinstance(requirements, str) or not {hook, requirements} <= set(files):
        return GATE_MISSING
    problem = requirements_problem(Path(requirements)) or hook_problem(Path(hook))
    if problem:
        return GATE_BROKEN + problem + "."
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


# Folders under /config the config watch does not read: Home Assistant's own stores, code, caches and media.
WATCH_SKIP_DIRS = {".storage", ".git", ".cache", "custom_components", "node_modules", "www", "deps", "tts",
                   "backups", "__pycache__", "blueprints_cache"}
WATCH_MAX_FILES = 3000
WATCH_MAX_BYTES = 2 * 1024 * 1024
WATCH_INTERVAL = 30.0
WATCH_SHOWN = 20
SCRIPT_KEY_RE = re.compile(r"^([a-z0-9_]+)\s*:\s*(?:#.*)?$")


class ConfigWatch:
    """Find actions on refused domains that appeared in /config YAML since remote control was turned on.

    The hook refuses such edits made with the patch tool. A command that writes
    a file directly (``python -c``, ``sed -i``) is not a patch, so this watch
    compares every YAML file under the config folder with a baseline taken when
    the owner turned remote control on. What it finds is shown on the app page
    and raised as a Home Assistant repair until the owner marks it reviewed.
    It detects; it does not prevent. Remote control cannot reload or restart
    Home Assistant, so the person who next reloads sees the repair first.

    The baseline is saved in the app's private storage, so a restart of the app
    compares with the same baseline and does not absorb a change.
    """

    def __init__(self, root: Path, baseline_path: Path,
                 rules: Callable[[], dict[str, Any]] = gate_rules.load_rules) -> None:
        self.root = root
        self.baseline_path = baseline_path
        self.rules = rules

    def files(self) -> list[Path]:
        """Return the YAML files the watch reads, sorted, skipping stores, code, caches and very large files."""
        found: list[Path] = []
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in WATCH_SKIP_DIRS and not d.startswith("."))
            for name in sorted(names):
                if not name.lower().endswith((".yaml", ".yml")) or name == "secrets.yaml":
                    continue
                path = Path(folder) / name
                try:
                    if path.is_symlink() or path.stat().st_size > WATCH_MAX_BYTES:
                        continue
                except OSError:
                    continue
                found.append(path)
                if len(found) >= WATCH_MAX_FILES:
                    return found
        return found

    def refused_scripts(self, texts: dict[str, str], rules: dict[str, Any]) -> set[str]:
        """Resolve script calls using the same active kinds as the line scan."""
        return gate_rules.refused_scripts(texts, rules)

    def scan(self) -> dict[str, list[gate_rules.Finding]]:
        """Return the refused actions in every YAML file, by path relative to the config folder."""
        return self._scan()[0]

    def _scan(self) -> tuple[dict[str, list[gate_rules.Finding]], dict[str, dict[str, tuple[int, int, str]]]]:
        """Return the refused lines and the refused blocks (see ``gate_rules.refused_blocks``) of every YAML file."""
        rules = self.rules()
        texts: dict[str, str] = {}
        for path in self.files():
            try:
                texts[path.relative_to(self.root).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        scripts = self.refused_scripts(texts, rules)
        lines = {rel: found for rel, text in texts.items() if (found := gate_rules.find_refused(text, rules, scripts))}
        blocks = {rel: found for rel, text in texts.items() if (found := gate_rules.refused_blocks(text, rules, scripts))}
        return lines, blocks

    def has_baseline(self) -> bool:
        """Return whether a baseline is saved, that is, remote control was on and its changes are not reviewed."""
        return self.baseline_path.is_file()

    def take_baseline(self) -> None:
        """Save what is in the config folder now as the baseline later scans compare with."""
        lines, blocks = self._scan()
        data = {rel: [list(f.key()) for f in found] for rel, found in lines.items()}
        hashes = {rel: sorted(found) for rel, found in blocks.items()}
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.baseline_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"taken": time.time(), "files": data, "blocks": hashes}), encoding="utf-8")
        os.replace(tmp, self.baseline_path)

    def clear(self) -> None:
        """Forget the baseline; the watch stops until remote control is turned on again."""
        try:
            self.baseline_path.unlink()
        except FileNotFoundError:
            pass

    def new_findings(self) -> list[dict[str, Any]]:
        """Report new security findings relative to the saved baseline.

        Calls are counted by reason and text, so moving one is not an addition.
        Definition, platform and templated-action blocks are hashed to catch
        changes to their bodies. Ordinary calls and templated targets do not
        cause a block finding. An unreadable baseline is treated as empty.
        """
        try:
            saved = json.loads(self.baseline_path.read_text(encoding="utf-8"))
            base = saved.get("files") or {}
            base_blocks = saved.get("blocks") or {}
        except (OSError, ValueError, AttributeError):
            base, base_blocks = {}, {}
        lines, blocks = self._scan()
        out: list[dict[str, Any]] = []
        for rel, found in sorted(lines.items()):
            left: dict[tuple[str, str], int] = {}
            for reason, text in base.get(rel, []):
                left[(reason, text)] = left.get((reason, text), 0) + 1
            for finding in found:
                if left.get(finding.key(), 0) > 0:
                    left[finding.key()] -= 1
                    continue
                out.append({"file": rel, "line": finding.line, "reason": finding.reason,
                            "text": redact(finding.text.strip())[:200]})
        new_lines = [(item["file"], item["line"]) for item in out]
        for rel, found in sorted(blocks.items()):
            known = set(base_blocks.get(rel, []))
            for digest, (first, last, text) in sorted(found.items(), key=lambda item: item[1][0]):
                if digest in known or any(f == rel and first <= n <= last for f, n in new_lines):
                    continue
                out.append({"file": rel, "line": first, "reason": gate_rules.BLOCK_CHANGED, "text": redact(text.strip())[:200]})
        return out


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
        watch: ConfigWatch | None = None,
        watch_interval: float = WATCH_INTERVAL,
    ) -> None:
        self.codex_binary = codex_binary
        self.state_path = state_path
        self.env_factory = env_factory
        self.name_factory = name_factory
        self.gate = gate
        self.popen = popen
        self.run = run
        self.clock = clock
        self.watch = watch
        self.watch_interval = watch_interval
        self.findings: list[dict[str, Any]] = []
        self.watch_thread: threading.Thread | None = None
        self.watch_wake = threading.Event()
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
        """At app start, run remote control again if the owner left it on and the gate allows it.

        The config watch carries on with its saved baseline, also while remote
        control is off with changes not yet reviewed.
        """
        if self.watch is not None and self.enabled() and not self.watch.has_baseline():
            self.watch.take_baseline()
            self.log.append("No saved baseline for the config watch; took one now.")
        self._start_watch()
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
        if self.watch is not None:
            self.watch.take_baseline()
            with self.lock:
                self.findings = []
        self.save_enabled(True)
        self._start_watch()
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
        if self.watch is not None:
            findings = self.watch.new_findings()
            with self.lock:
                self.findings = findings
            if not findings:
                self.watch.clear()
        self.set_status("off", "Remote control is off.", pairing_code="")
        return self.snapshot()

    def review(self) -> dict[str, Any]:
        """Mark the config changes found as reviewed: compare with what is there now from here on.

        While remote control is on, the current config becomes the new baseline;
        while it is off, the watch stops until remote control is turned on again.
        """
        if self.watch is not None:
            if self.enabled():
                self.watch.take_baseline()
            else:
                self.watch.clear()
        with self.lock:
            self.findings = []
        return self.snapshot()

    def _start_watch(self) -> None:
        """Start the config watch thread when there is a baseline to compare with and it is not running."""
        if self.watch is None or not self.watch.has_baseline():
            return
        with self.lock:
            if self.watch_thread is not None and self.watch_thread.is_alive():
                return
            self.watch_wake.clear()
            self.watch_thread = threading.Thread(target=self._watch_loop, name="remote-control-watch", daemon=True)
            self.watch_thread.start()

    def _watch_loop(self) -> None:
        """Scan the config folder against the baseline until the baseline is gone (reviewed after Turn off)."""
        while self.watch is not None and self.watch.has_baseline():
            try:
                findings = self.watch.new_findings()
            except Exception as exc:  # noqa: BLE001 - a scan error is shown, the watch keeps going
                findings = [{"file": "", "line": 0, "reason": f"the config watch could not scan: {exc}", "text": ""}]
            with self.lock:
                self.findings = findings
            if self.watch_wake.wait(self.watch_interval):
                self.watch_wake.clear()

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
            findings = list(self.findings)
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
            "config_findings": findings[:WATCH_SHOWN],
            "config_findings_count": len(findings),
        }
