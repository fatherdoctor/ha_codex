"""Readiness checks for Codex's managed command-prefix rules.

The probe opens a new ephemeral thread, never a turn or an owner's thread.
Rule warnings are latched until the worker restarts; raw warning text may name
private files, so only a fixed explanation reaches the page or task output.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import threading
import time
from typing import Any

RULES_WARNING = "Codex could not load command rules. Fix the rule files and restart the app before turning on remote control."
_warning = ""
_lock = threading.Lock()


def exec_rules_enabled() -> bool:
    """Return whether the managed exec-rules kind is on."""
    # TODO(CX-CHECK-TOGGLES): use its shared saved setting and rebuild requirements
    # before starting Codex. The named sequencing ruling keeps this kind on now.
    return True


def warning_problem(message: Any) -> str:
    """Recognize the pinned server's rule-load warnings without exposing details."""
    if not isinstance(message, dict) or message.get("method") != "configWarning":
        return ""
    params = message.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("summary"), str):
        return ""
    # A private pathname containing "rules" does not classify the warning.
    text = params["summary"].lower()
    return RULES_WARNING if "rules" in text or "execpolicy" in text else ""


def record_warning(message: Any) -> str:
    """Keep a rule warning across tasks and connection attempts in this worker."""
    problem = warning_problem(message)
    if problem:
        global _warning
        with _lock:
            _warning = problem
    return problem


def current_problem() -> str:
    with _lock:
        return _warning


def requirements_problem(data: dict[str, Any]) -> str:
    """Reject a missing catalogue or a permissive/malformed managed rule."""
    if not exec_rules_enabled():
        return ""
    rules = data.get("rules")
    prefixes = rules.get("prefix_rules") if isinstance(rules, dict) else None
    if not isinstance(prefixes, list) or not prefixes:
        return "the managed command-prefix rules are missing"
    for rule in prefixes:
        if not isinstance(rule, dict) or rule.get("decision") not in {"forbidden", "prompt"}:
            return "a managed command rule has no explicit forbidden or prompt decision"
        pattern = rule.get("pattern")
        if not isinstance(pattern, list) or not pattern:
            return "a managed command rule has an empty pattern"
        for token in pattern:
            if not isinstance(token, dict):
                return "a managed command rule has an invalid token"
            literal, alternatives = token.get("token"), token.get("any_of")
            if (set(token) == {"token"} and isinstance(literal, str) and literal
                    or set(token) == {"any_of"} and isinstance(alternatives, list)
                    and alternatives and all(isinstance(t, str) and t for t in alternatives)):
                continue
            return "a managed command rule has an invalid token"
    return ""


def self_test(binary: str, env: dict[str, str], cwd: str = "/config", *,
              popen: Any = subprocess.Popen, timeout: float = 15.0) -> str:
    """Load the actual user/project stack in Codex, with no model request.

    Initialization reports user-rule warnings; thread/start also loads the
    working directory's trusted project rules. No relay is enabled here.
    """
    if not exec_rules_enabled():
        return ""
    if current_problem():
        return current_problem()
    proc = None
    selector = selectors.DefaultSelector()
    buffered = b""
    deadline = time.monotonic() + timeout
    try:
        proc = popen([binary, "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        selector.register(proc.stdout, selectors.EVENT_READ)

        def send(message: dict[str, Any]) -> None:
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()

        def reply(identifier: int) -> dict[str, Any]:
            nonlocal buffered
            while time.monotonic() < deadline:
                if b"\n" not in buffered:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    block = os.read(proc.stdout.fileno(), 65536)
                    if not block:
                        raise ValueError("closed")
                    buffered += block
                    if len(buffered) > 1024 * 1024:
                        raise ValueError("oversized")
                    continue
                line, buffered = buffered.split(b"\n", 1)
                message = json.loads(line)
                if record_warning(message):
                    raise ValueError("rules warning")
                if message.get("id") == identifier:
                    if not isinstance(message.get("result"), dict):
                        raise ValueError("refused")
                    return message["result"]
            raise TimeoutError("self-test")

        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "ha_codex_rules_check", "version": "0.166.0"},
            "capabilities": {"experimentalApi": True}}})
        reply(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "thread/start", "params": {
            "cwd": cwd, "ephemeral": True, "approvalPolicy": "on-request", "sandbox": "workspace-write"}})
        result = reply(2)
        if result.get("approvalPolicy") != "on-request" or not isinstance(result.get("thread"), dict):
            raise ValueError("unexpected thread")
        return ""
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return current_problem() or "The Codex command-rules self-test did not complete. Restart the app after checking its rule files."
    finally:
        selector.close()
        if proc is not None:
            # End the whole fresh probe group, including a CLI shim's children.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()
