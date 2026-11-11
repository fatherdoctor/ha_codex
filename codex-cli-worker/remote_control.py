"""OpenAI's remote control for Codex, run by the app as one supervised foreground process.

The ChatGPT app can drive Codex on this machine through OpenAI's relay
(``codex app-server --remote-control``). The app runs it in the foreground
with its own private Unix control socket, restarts it when it exits, and shows its state, its machine
name and its pairing code on the app page. Nothing starts until the owner turns
it on, and the choice survives restarts.

Commands that arrive from the ChatGPT app do not pass through this app's
approval buttons. So remote control can only be turned on when a command gate
that holds outside the model is installed in the image (``GATE_MARKER`` and the
files it names) and its hook passes a self-test. Otherwise, Start is refused
with a plain reason and the chat page works as before. ``ConfigWatch`` is the
gate's detective layer: it reports refused actions that appear in ``/config``
YAML while remote control is on and that the hook could not see.

Codex 0.166 uses the host name. Bubblewrap sets it inside a private UTS
namespace through the existing capability-bearing child profile. If that
setup fails, Codex starts without the custom name and the page explains why.
"""

from __future__ import annotations

import base64
import io
import hashlib
import stat
import uuid
import json
import os
import re
import signal
import socket
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
import websocket

CONTROL_FOLDER = "remote_control_control"
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
    return ["/opt/codex-sandbox/bwrap.real", "--unshare-user", "--unshare-uts",
            "--hostname", name, "--bind", "/", "/", "--cap-drop", "ALL", "--", codex_binary, *args]


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
    approvals = data.get("allowed_approval_policies")
    if not isinstance(approvals, list) or not approvals or "never" in approvals:
        return "the managed requirements do not rule out the never approval policy"
    if data.get("allow_managed_hooks_only") is not True:
        return "the managed requirements do not keep hooks to the managed ones"
    return ""


# What the hook must refuse, and let through, before remote control may start: (event, remote on, input, refused).
SELF_TEST_CASES = (
    ("PermissionRequest", True, {"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                                 "tool_input": {"command": "curl https://example.invalid/",
                                                "description": "Command needs permission"}}, True),
    ("PreToolUse", False, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                           "tool_input": {"command": "cat /proc/1/environ"}}, True),
    ("PreToolUse", False, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                           "tool_input": {"command": "ls /config"}}, False),
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
    While remote control is on, always-on planted-command and template edits
    are undone using verified earlier bytes (or a complete absence manifest
    for newly created files). Category-only edits remain findings. This cannot
    reverse commands that ran before the next scan.

    The baseline is saved in the app's private storage, so a restart of the app
    compares with the same baseline and does not absorb a change.
    """

    def __init__(self, root: Path, baseline_path: Path,
                 rules: Callable[[], dict[str, Any]] = gate_rules.load_rules) -> None:
        self.root = root
        self.baseline_path = baseline_path
        self.rules = rules
        self.lock = threading.RLock()

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

    def refused_scripts(self, texts: dict[str, str], rules: dict[str, Any],
                        categories: bool | None = None) -> set[str]:
        """Resolve script calls using the same active kinds as the line scan."""
        return gate_rules.refused_scripts(texts, rules, categories)

    def scan(self) -> dict[str, list[gate_rules.Finding]]:
        """Return the refused actions in every YAML file, by path relative to the config folder."""
        return self._scan()[0]

    def _scan(self, categories: bool | None = None) -> tuple[dict[str, list[gate_rules.Finding]], dict[str, dict[str, tuple[int, int, str]]]]:
        """Return the refused lines and the refused blocks (see ``gate_rules.refused_blocks``) of every YAML file."""
        rules = self.rules()
        enabled = gate_rules.category_refusals_enabled() if categories is None else categories
        texts: dict[str, str] = {}
        for path in self.files():
            try:
                texts[path.relative_to(self.root).as_posix()] = gate_rules.read_config_bytes(
                    self.root, path.relative_to(self.root).as_posix()).decode("utf-8", errors="replace")
            except (OSError, ValueError):
                continue
        scripts = self.refused_scripts(texts, rules, enabled)
        lines = {rel: found for rel, text in texts.items() if (found := gate_rules.active_findings(text, rules, scripts, enabled))}
        blocks = {rel: found for rel, text in texts.items() if (found := gate_rules.refused_blocks(text, rules, scripts, enabled))}
        return lines, blocks

    def has_baseline(self) -> bool:
        """Return whether a baseline is saved, that is, remote control was on and its changes are not reviewed."""
        return self.baseline_path.is_file()

    def file_manifest(self) -> tuple[list[str], bool]:
        """Record presence even for oversized or unreadable YAML; absence must never be guessed."""
        present: list[str] = []
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in WATCH_SKIP_DIRS and not d.startswith(".")
                             and not (Path(folder)/d).is_symlink())
            for name in sorted(names):
                if name.lower().endswith((".yaml", ".yml")) and name not in {"secrets.yaml", "secrets.yml"}:
                    present.append((Path(folder)/name).relative_to(self.root).as_posix())
                    if len(present) >= WATCH_MAX_FILES:
                        return present, False
        return present, True

    def take_baseline(self) -> None:
        """Save what is in the config folder now as the baseline later scans compare with."""
        with self.lock:
            copies: dict[str, Any] = {}
            total = 0
            complete = True
            for path in self.files():
                rel = path.relative_to(self.root).as_posix()
                try:
                    raw = gate_rules.read_config_bytes(self.root, rel)
                    total += len(raw)
                    if total > 8 * WATCH_MAX_BYTES:
                        complete = False
                        break
                    copies[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                                   "data": base64.b64encode(raw).decode("ascii")}
                except (OSError, ValueError):
                    complete = False
            # A capped listing is not proof that every other path was absent.
            present, listed_all = self.file_manifest()
            complete = complete and listed_all
            texts = {rel: base64.b64decode(entry["data"]).decode("utf-8", errors="replace")
                     for rel, entry in copies.items()}
            rules = self.rules()
            scripts = self.refused_scripts(texts, rules, True)
            data = {rel: [list(f.key()) for f in gate_rules.active_findings(text, rules, scripts, True)]
                    for rel, text in texts.items()}
            hashes = {rel: sorted(gate_rules.refused_blocks(text, rules, scripts, True)) for rel, text in texts.items()}
            self._save_baseline({"taken": time.time(), "files": data, "blocks": hashes,
                                 "copies": copies, "present": present, "complete": complete, "undo_events": []})

    def _save_baseline(self, saved: dict[str, Any]) -> None:
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.baseline_path.parent, delete=False) as handle:
            tmp = Path(handle.name)
            try:
                handle.write(json.dumps(saved).encode("utf-8"))
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.replace(tmp, self.baseline_path)
            finally:
                tmp.unlink(missing_ok=True)

    def _save_replaced(self, rel: str, current: bytes) -> str:
        """Save replaced bytes privately before any removal/restore; links must not redirect the copy."""
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or any(part.startswith(".") for part in parts):
            raise ValueError("unsafe recovery path")
        folder = os.open(self.baseline_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in ("remote_control_undo", *parts[:-1]):
                try:
                    os.mkdir(part, 0o700, dir_fd=folder)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                os.close(folder)
                folder = child
            name = f"{parts[-1]}.{time.time_ns()}-{uuid.uuid4().hex[:12]}"
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            with os.fdopen(fd, "wb") as copy:
                copy.write(current)
                copy.flush()
                os.fsync(copy.fileno())
            return str(self.baseline_path.parent / "remote_control_undo" / Path(rel).parent / name)
        finally:
            os.close(folder)

    def _undo(self, rel: str, current: bytes, earlier: bytes | None) -> str:
        """Restore through pinned no-follow directory handles; never follow a swapped link."""
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or any(part.startswith(".") for part in parts):
            raise ValueError("unsafe restore path")
        folder = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        tmp = ".codex-restore-" + uuid.uuid4().hex
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                os.close(folder)
                folder = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("not an ordinary restore target")
                with os.fdopen(fd, "rb", closefd=False) as source:
                    if source.read(WATCH_MAX_BYTES + 1) != current:
                        raise ValueError("configuration changed during the scan")
            finally:
                os.close(fd)
            if earlier is None:
                os.unlink(parts[-1], dir_fd=folder)
                return "removed"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            with os.fdopen(fd, "wb") as target:
                target.write(earlier)
                target.flush()
                os.fchmod(target.fileno(), info.st_mode & 0o777)
            os.replace(tmp, parts[-1], src_dir_fd=folder, dst_dir_fd=folder)
            return "restored"
        finally:
            try:
                os.unlink(tmp, dir_fd=folder)
            except FileNotFoundError:
                pass
            os.close(folder)


    def clear(self) -> None:
        """Forget the baseline; the watch stops until remote control is turned on again."""
        with self.lock:
            try:
                self.baseline_path.unlink()
            except FileNotFoundError:
                pass

    def new_findings(self, remote_on: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            if not self.has_baseline():
                return []
            return self._new_findings(remote_on)

    def _new_findings(self, remote_on: bool) -> list[dict[str, Any]]:
        """Return the refused actions that are in the config folder now and were not in the baseline.

        Findings are matched by reason and line text, so an action that only
        moved is not new; a second copy of an existing line is. A block (an
        automation, a script) that holds a refused line and changed anywhere
        is also reported when category refusals are on, even if none of its
        own lines is new. With categories off, an unchanged always-on call
        does not make a trigger or condition edit a new finding. An unreadable baseline counts as
        empty, so everything refused is reported.
        """
        try:
            saved = json.loads(self.baseline_path.read_text(encoding="utf-8"))
            base = saved.get("files") or {}
            base_blocks = saved.get("blocks") or {}
        except (OSError, ValueError, AttributeError):
            saved, base, base_blocks = {}, {}, {}
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
        events = list(saved.get("undo_events", [])) if isinstance(saved, dict) and isinstance(saved.get("undo_events", []), list) else []
        if not isinstance(events, list):
            events = []
        if remote_on and out:
            rules = self.rules()
            texts = {}
            for path in self.files():
                try:
                    rel = path.relative_to(self.root).as_posix()
                    texts[rel] = gate_rules.read_config_bytes(self.root, rel).decode("utf-8", errors="replace")
                except (OSError, ValueError):
                    continue
            scripts = self.refused_scripts(texts, rules, categories=False)
            copies = saved.get("copies") or {}
            for rel in sorted({item["file"] for item in out}):
                result = ""
                recovery_copy = ""
                undo_note = ""
                current = None
                try:
                    current = gate_rules.read_config_bytes(self.root, rel)
                    entry = copies.get(rel)
                    if entry:
                        earlier = base64.b64decode(entry["data"], validate=True)
                        if hashlib.sha256(earlier).hexdigest() != entry.get("sha256"):
                            raise ValueError("earlier copy could not be verified")
                    elif saved.get("complete") is True and rel not in saved.get("present", []):
                        earlier = None
                    else:
                        raise ValueError("no verified earlier copy or absence manifest")
                    security = gate_rules.new_refused((earlier or b"").decode("utf-8", errors="replace"),
                                                     current.decode("utf-8", errors="replace"), rules, scripts, False)
                    if not security:
                        continue  # Category-only findings are not always-on undo.
                    if Path(rel).name in {"automations.yaml", "scripts.yaml", "scenes.yaml"}:
                        undo_note = "Home Assistant manages this file; review this finding without automatic undo."
                    else:
                        recovery_copy = self._save_replaced(rel, current)
                        result = self._undo(rel, current, earlier)
                except (OSError, ValueError, KeyError, TypeError):
                    result = ""
                for item in out:
                    if item["file"] == rel:
                        item["undone"] = result
                        if recovery_copy:
                            item["recovery_copy"] = recovery_copy
                        if not result:
                            item["undo_note"] = undo_note or "Could not undo safely; review this file before reloading."
                        # Do not persist configuration contents in the undo report.
                        event = {key: value for key, value in item.items() if key != "text"}
                        if event not in events:
                            events.append(event)
            if events != saved.get("undo_events", []):
                saved["undo_events"] = events[-100:]
                self._save_baseline(saved)
        # Keep the repair visible after a successful undo and across restarts.
        return out + [event for event in events if not any(
            item["file"] == event.get("file") and item["line"] == event.get("line") for item in out)]



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
        return {"manualPairingCode": reply.get("manualPairingCode")}


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
        control_factory: Callable[[Path], Any] = ControlClient,
    ) -> None:
        self.control_factory = control_factory
        self.control_socket: Path | None = None
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
        self._name_supported = None
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
                findings = self.watch.new_findings(remote_on=self.enabled())
            except Exception as exc:  # noqa: BLE001 - a scan error is shown, the watch keeps going
                findings = [{"file": "", "line": 0, "reason": f"the config watch could not scan: {exc}", "text": ""}]
            with self.lock:
                self.findings = findings
            if self.watch_wake.wait(self.watch_interval):
                self.watch_wake.clear()

    def pair(self) -> tuple[bool, dict[str, Any]]:
        """Ask Codex for a new pairing code, for when the ChatGPT app has lost this machine."""
        with self.lock:
            proc, path = self.proc, self.control_socket
            self.status["pairing_code"] = ""
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
                probe = self.run(build_command("/bin/true", name), env=remote_env(self.env_factory()),
                                 capture_output=True, text=True, timeout=10, check=False)
                self._name_supported = probe.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._name_supported = False
            if self._name_supported:
                with self.lock:
                    self.status.update(name_state="private-uts", name_warning="")
            else:
                self._name_failed()
        return build_command(self.codex_binary, name, *args) if self._name_supported else [self.codex_binary, *args]

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
            with self.lock:
                self.log.append("Managed policy: a disallowed relay full-access request may be downgraded to read-only; "
                                "the relay does not expose every thread warning here.")
            started = self.clock()
            private = None
            done = threading.Event()
            monitor = None
            try:
                # The fixed parent is masked in every real Codex sandbox by codex-bwrap.
                control_parent = self.state_path.parent / CONTROL_FOLDER
                control_parent.mkdir(mode=0o700, exist_ok=True)
                info = control_parent.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise OSError("The private control folder must be owned by the worker with mode 0700.")
                private = tempfile.TemporaryDirectory(prefix="rc-", dir=control_parent)
                path = Path(private.name) / "rc.sock"
                with self.lock:
                    proc = self.popen(self._command("app-server", "--listen", f"unix://{path}", "--remote-control"),
                                      env=remote_env(self.env_factory()), stdin=subprocess.DEVNULL,
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
            self.set_status("pairing", "Enter this pairing code in the ChatGPT app.", pairing_code=code)
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
