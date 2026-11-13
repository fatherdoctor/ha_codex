"""The managed Codex hook of the remote-control command check (``/usr/share/ha_codex/gate_hook.py``).

``/etc/codex/managed_config.toml`` runs it for every ``PreToolUse`` and
``PermissionRequest`` event, from every client: the chat page and the ChatGPT
app alike. It reads the event as JSON on stdin and either refuses the call or
says nothing. It never asks.

A refusal is written both ways Codex hooks can take it (provisional until the
lab captures the 0.166 format): a JSON decision on stdout and the reason on
stderr with exit code 2. No opinion is exit code 0 with no output, and the
usual approval flow runs.

Input that is not JSON is refused: a check that cannot read a request does not
let it through.

Whether remote control is on is read from the app's saved choice
(``--state``, default ``/data/remote_control.json``), which no sandbox can
write. A file that exists but cannot be read counts as on.

The hook answers within its own deadline, ``DEADLINE_SECONDS`` from the start
of its process, well inside the timeout in ``managed_config.toml``. Codex 0.166
runs the action when a hook passes that timeout, so a late answer would let
the call through. The check itself runs in a forked child process; the parent
only waits. When the deadline passes, the parent stops the child and refuses
with a plain reason. A separate process is used because a thread or a signal
cannot interrupt Python while it is stuck inside one long call, such as a
regular expression that backtracks.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import time
from pathlib import Path

# Taken before anything else is imported, for when the kernel's own start time cannot be read.
IMPORTED_AT = time.monotonic()

# The image keeps gate_rules.py next to this script; a source checkout keeps it one folder up. Python runs
# with -I, which leaves the script's folder off the path. The import happens in main(), so that a missing
# or broken rules module refuses the request instead of failing with an exit code Codex would ignore.
HERE = Path(__file__).resolve().parent
RULES_DIR = HERE if (HERE / "gate_rules.py").is_file() else HERE.parent

DEFAULT_STATE = Path("/data/remote_control.json")
UNREADABLE = "The command check could not read this request, so it is refused."

# How long the hook may take, counted from the start of its process. Codex waits 10 seconds
# (managed_config.toml); this leaves the rest of that time for a slow machine to start Python.
DEADLINE_SECONDS = 1.5
TOO_SLOW = ("The command check did not finish in time, so this request is refused. "
            "Ask again, or make the change yourself from the app page.")


def remote_control_on(path: Path) -> bool:
    """Return whether the owner left remote control on; a missing file is off, an unreadable one is on."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        data = json.loads(text)
    except ValueError:
        return True
    return not isinstance(data, dict) or data.get("enabled") is not False


def refusal(event: str, reason: str) -> dict:
    """Return the JSON decision that refuses this event, in the shape Codex hooks read."""
    if event == "PermissionRequest":
        return {"hookSpecificOutput": {"hookEventName": event, "decision": {"behavior": "deny", "message": reason}}}
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {"hookEventName": event or "PreToolUse", "permissionDecision": "deny",
                               "permissionDecisionReason": reason},
    }


def process_started_at() -> float:
    """Return when this process started, on the ``time.monotonic()`` clock.

    The kernel records the start in ``/proc/self/stat`` (field 22, in clock
    ticks since boot), which includes the time Python took to start. Where that
    cannot be read, the time this module was imported is used instead.
    """
    try:
        stat = Path("/proc/self/stat").read_text(encoding="ascii")
        # The command name in field 2 may contain spaces, so count the fields after its closing parenthesis.
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        running_for = time.clock_gettime(time.CLOCK_BOOTTIME) - start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return IMPORTED_AT
    if running_for < 0:
        return IMPORTED_AT
    return time.monotonic() - running_for


def judge(args: argparse.Namespace, stdin) -> tuple[str, str]:
    """Read the event from stdin and return it with the reason to refuse it ("" lets it through)."""
    event = args.event
    try:
        if str(RULES_DIR) not in sys.path:
            sys.path.insert(0, str(RULES_DIR))
        import gate_rules

        payload = json.loads(stdin.read() or "null")
        if isinstance(payload, dict):
            event = str(payload.get("hook_event_name") or event)
        reason = gate_rules.check_tool_call(event, payload, remote_control_on(Path(args.state)),
                                            gate_rules.load_rules(Path(args.rules) if args.rules else None))
    except Exception:  # noqa: BLE001 - any failure to judge a request refuses it
        reason = UNREADABLE
    return event, reason


def judge_before_deadline(args: argparse.Namespace, stdin, deadline: float) -> tuple[str, str]:
    """Run ``judge`` in a forked child and return its answer, or a refusal if it is not back by ``deadline``.

    The child sends its answer as one JSON line on a pipe and exits. The parent
    waits on the pipe until the deadline, then kills the child if it is still
    running. A child that dies without an answer, or a fork that fails, refuses.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        read_end, write_end = os.pipe()
        child = os.fork()
    except OSError:
        return args.event, UNREADABLE
    if child == 0:
        # The child: judge, send the answer, and leave without running any of the parent's cleanup.
        exit_code = 1
        try:
            os.close(read_end)
            event, reason = judge(args, stdin)
            os.write(write_end, json.dumps({"event": event, "reason": reason}).encode("utf-8"))
            exit_code = 0
        finally:
            os._exit(exit_code)

    os.close(write_end)
    answer = b""
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([read_end], [], [], remaining)
            if not ready:
                continue  # the loop's next pass sees that the deadline has passed
            chunk = os.read(read_end, 65536)
            if not chunk:
                break  # the child closed the pipe: its answer is complete
            answer += chunk
    finally:
        os.close(read_end)
        if timed_out:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.waitpid(child, 0)

    if timed_out:
        return args.event, TOO_SLOW
    try:
        data = json.loads(answer)
        return str(data["event"]), str(data["reason"])
    except (ValueError, KeyError, TypeError):
        return args.event, UNREADABLE


def main(argv: list[str] | None = None, stdin=None, stdout=None, stderr=None, started: float | None = None) -> int:
    """Read one hook event, write a refusal or nothing, and return the exit code (2 refuses, 0 passes).

    ``started`` is when the deadline starts counting, on the ``time.monotonic()``
    clock; the script passes its process start, and a caller in the same
    process (the tests) gets the time of the call.
    """
    started = time.monotonic() if started is None else started
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", default="", help="the hook event, when the input does not name it")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="the app's saved remote-control choice")
    parser.add_argument("--rules", default="", help="the refused-domain list (default: the image's)")
    parser.add_argument("--timing", action="store_true",
                        help="also write the hook's own time on stderr (for tests/measure_gate.py; Codex never sets it)")
    args = parser.parse_args(argv)
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    event, reason = judge_before_deadline(args, stdin, started + DEADLINE_SECONDS)
    if args.timing:
        elapsed_ms = (time.monotonic() - started) * 1000
        stderr.write(f"gate_hook timing: {elapsed_ms:.1f} ms from process start, "
                     f"deadline {DEADLINE_SECONDS * 1000:.0f} ms\n")
    if not reason:
        return 0
    stdout.write(json.dumps(refusal(event, reason)) + "\n")
    stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    try:
        code = main(started=process_started_at())
    except SystemExit as exc:  # argparse: a bad command line refuses too
        code = 0 if exc.code == 0 else 2
    except BaseException:  # noqa: BLE001 - whatever happens, a request the check cannot judge is refused
        sys.stderr.write(UNREADABLE + "\n")
        code = 2
    sys.exit(code)
