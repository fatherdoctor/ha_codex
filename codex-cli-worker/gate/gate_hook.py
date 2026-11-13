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
the call through.

How the deadline is kept: the hook's first lines fork a child process, and the
child does all of the work - importing modules, finding and reading files,
reading the request and checking it. The parent only waits for the child's
answer on a pipe, so nothing it does can block on a disk or on the check. When
the deadline passes, the parent kills the child, writes the refusal and exits
without waiting for the child to go away (whatever reaps the hook's orphans
collects it). An answer the parent finds only after the deadline - because the
parent itself was not scheduled while the child finished, say - is thrown away
and refused like a late one: the parent checks the time after every read of
the pipe, before it accepts anything.
A separate process is used because neither a thread nor a signal can interrupt
Python while it is stuck inside one long call, such as a regular expression
that backtracks, or a file read on a stalled disk.

So that the parent never touches the disk, this module imports only ``os``,
``sys`` and ``time`` at the top, which Python has already loaded before the
script's first line runs; everything else is imported inside the child.
Python's own start, before the first line, cannot be interrupted, but it
counts against the deadline.
"""

import os
import sys
import time

# Taken before anything else, for when the kernel's own start time cannot be read.
IMPORTED_AT = time.monotonic()

DEFAULT_STATE = "/data/remote_control.json"
UNREADABLE = "The command check could not read this request, so it is refused."

# How long the hook may take, counted from the start of its process. Codex waits 10 seconds
# (managed_config.toml); this leaves the rest of that time for a slow machine to start Python.
DEADLINE_SECONDS = 1.5
TOO_SLOW = ("The command check did not finish in time, so this request is refused. "
            "Ask again, or make the change yourself from the app page.")

# How often the parent looks for the child's answer while it waits.
POLL_SECONDS = 0.005
# The signal number of SIGKILL, the same on every Linux. Importing the signal module would read a file.
SIGKILL = 9

# Children killed at the deadline that have not been reaped yet. A hook run as a script exits right after
# its answer, and whatever reaps its orphans collects them. A caller that runs main() in its own process
# (the tests) keeps running, so collect_killed_children() reaps them for it, without waiting.
killed_children: list[int] = []


def remote_control_on(path) -> bool:
    """Return whether the owner left remote control on; a missing file is off, an unreadable one is on."""
    import json

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


def parent_refusal(event: str, reason: str) -> str:
    """Return the refusal the parent writes itself, as the JSON text ``refusal`` would give, without ``json``.

    The parent may not import the json module (that reads files), so it writes
    this text by hand. ``reason`` is always one of this module's own fixed
    texts, which need no escaping; any event other than the two Codex sends
    is answered in the PreToolUse shape. A test checks that the text is the
    same as ``json.dumps(refusal(event, reason))``.
    """
    if event == "PermissionRequest":
        return ('{"hookSpecificOutput": {"hookEventName": "PermissionRequest", '
                f'"decision": {{"behavior": "deny", "message": "{reason}"}}}}}}')
    return (f'{{"decision": "block", "reason": "{reason}", "hookSpecificOutput": {{"hookEventName": '
            f'"PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "{reason}"}}}}')


def process_started_at() -> float:
    """Return when this process started, on the ``time.monotonic()`` clock.

    The kernel records the start in ``/proc/self/stat`` (field 22, in clock
    ticks since boot), which includes the time Python took to start. That file
    is made by the kernel from memory, never read from a disk. Where it cannot
    be read, the time this module was imported is used instead.
    """
    try:
        # Read as bytes: decoding as text could import a codec module, which is a file read.
        with open("/proc/self/stat", "rb") as file:
            stat = file.read()
        # The command name in field 2 may contain spaces, so count the fields after its closing parenthesis.
        start_ticks = int(stat.rsplit(b")", 1)[1].split()[19])
        running_for = time.clock_gettime(time.CLOCK_BOOTTIME) - start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return IMPORTED_AT
    if running_for < 0:
        return IMPORTED_AT
    return time.monotonic() - running_for


def option_value(argv: list[str], name: str) -> str:
    """Return the value of ``--name value`` or ``--name=value`` in ``argv``, or "" when it is not there.

    The parent reads ``--event`` with this rather than argparse, which it may
    not import. The child's argparse is set to refuse shortened option names,
    so both read the command line the same way.
    """
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
    return ""


# judge(), answer() and send_from_child() run only in the child, so they may import and read files.

def judge(args, stdin) -> tuple[str, str]:
    """Read the event from stdin and return it with the reason to refuse it ("" lets it through)."""
    import json
    from pathlib import Path

    event = args.event
    try:
        # The image keeps gate_rules.py next to this script; a source checkout keeps it one folder up. Python
        # runs with -I, which leaves the script's folder off the path. A missing or broken rules module
        # refuses the request.
        here = Path(__file__).resolve().parent
        rules_dir = here if (here / "gate_rules.py").is_file() else here.parent
        if str(rules_dir) not in sys.path:
            sys.path.insert(0, str(rules_dir))
        import gate_rules

        payload = json.loads(stdin.read() or "null")
        if isinstance(payload, dict):
            event = str(payload.get("hook_event_name") or event)
        reason = gate_rules.check_tool_call(event, payload, remote_control_on(Path(args.state)),
                                            gate_rules.load_rules(Path(args.rules) if args.rules else None))
    except Exception:  # noqa: BLE001 - any failure to judge a request refuses it
        reason = UNREADABLE
    return event, reason


def answer(argv: list[str], stdin) -> tuple[int, str, str]:
    """Read the command line and the event, and return the exit code and the text for stdout and stderr."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--event", default="", help="the hook event, when the input does not name it")
    parser.add_argument("--state", default=DEFAULT_STATE, help="the app's saved remote-control choice")
    parser.add_argument("--rules", default="", help="the refused-domain list (default: the image's)")
    parser.add_argument("--timing", action="store_true",
                        help="also write the hook's own time on stderr (for tests/measure_gate.py; Codex never sets it)")
    try:
        args = parser.parse_args(argv)
    except SystemExit:  # a bad command line refuses
        return 2, json.dumps(refusal(option_value(argv, "--event"), UNREADABLE)) + "\n", UNREADABLE + "\n"
    event, reason = judge(args, stdin)
    if not reason:
        return 0, "", ""
    return 2, json.dumps(refusal(event, reason)) + "\n", reason + "\n"


def send_from_child(argv: list[str], stdin, write_end: int) -> None:
    """In the child: work out the answer and write it to the pipe as "code NUL stdout NUL stderr"."""
    # Let go of the hook's stdout and stderr first, so that a child still stuck after the parent has
    # answered does not keep Codex waiting for those streams to close.
    os.close(1)
    os.close(2)
    null = os.open(os.devnull, os.O_WRONLY)
    for fd in (1, 2):
        if null != fd:
            os.dup2(null, fd)
    code, out, err = answer(argv, stdin)
    message = f"{code}\0{out}\0{err}".encode()
    while message:
        message = message[os.write(write_end, message):]


def wait_for_child(read_end: int, deadline: float) -> bytes | None:
    """Collect the child's message from the pipe; return None if it is not complete by ``deadline``.

    The pipe is read without blocking, so the parent never waits past the deadline.
    The time is checked after every read, before anything read is accepted: an
    answer that is already waiting in the pipe when the parent looks after the
    deadline is not used.
    """
    os.set_blocking(read_end, False)
    message = b""
    while True:
        try:
            chunk = os.read(read_end, 65536)
        except BlockingIOError:
            chunk = None  # nothing to read yet
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if chunk is None:
            time.sleep(min(POLL_SECONDS, remaining))
        elif chunk:
            message += chunk
        else:
            return message  # the child closed the pipe in time: its message is complete


def collect_child(child: int) -> None:
    """Reap the child, which has sent its answer and is exiting, without waiting for it.

    A child that has not exited yet is left to whatever reaps the hook's
    orphans once the hook exits; waiting here could delay the answer.
    """
    try:
        os.waitpid(child, os.WNOHANG)
    except ChildProcessError:
        pass


def collect_killed_children() -> None:
    """Reap the children killed at earlier deadlines that have gone, without waiting for the others.

    The hook calls this at the start of every call, so a caller that runs main()
    again and again in one process does not collect zombies; such a caller may
    also call it when it is done.
    """
    for child in list(killed_children):
        try:
            pid, _status = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            pid = child  # already reaped elsewhere
        if pid == child:
            killed_children.remove(child)


def answer_before_deadline(argv: list[str], stdin, deadline: float) -> tuple[int, str, str]:
    """Run ``answer`` in a forked child; return its answer, or a refusal if it is not back by ``deadline``.

    A child that dies without an answer, or a fork that fails, refuses.
    """
    collect_killed_children()
    event = option_value(argv, "--event")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        read_end, write_end = os.pipe()
        child = os.fork()
    except OSError:
        return 2, parent_refusal(event, UNREADABLE) + "\n", UNREADABLE + "\n"
    if child == 0:
        # The child: answer, and leave without running any of the parent's cleanup.
        exit_code = 1
        try:
            os.close(read_end)
            send_from_child(argv, stdin, write_end)
            exit_code = 0
        finally:
            os._exit(exit_code)

    os.close(write_end)
    try:
        message = wait_for_child(read_end, deadline)
    finally:
        os.close(read_end)
    if message is None:
        # Kill the child and answer at once. It is not reaped here: a killed child can take a while to
        # go (held up in the kernel, say). Whatever reaps the hook's orphans collects it after the hook
        # exits, or, for a caller that keeps running, collect_killed_children() does.
        try:
            os.kill(child, SIGKILL)
        except ProcessLookupError:
            pass
        killed_children.append(child)
        return 2, parent_refusal(event, TOO_SLOW) + "\n", TOO_SLOW + "\n"
    collect_child(child)
    parts = message.decode("utf-8", errors="replace").split("\0")
    if len(parts) != 3 or parts[0] not in ("0", "2"):
        return 2, parent_refusal(event, UNREADABLE) + "\n", UNREADABLE + "\n"
    return int(parts[0]), parts[1], parts[2]


def main(argv: list[str] | None = None, stdin=None, stdout=None, stderr=None, started: float | None = None) -> int:
    """Read one hook event, write a refusal or nothing, and return the exit code (2 refuses, 0 passes).

    ``started`` is when the deadline starts counting, on the ``time.monotonic()``
    clock; the script passes its process start, and a caller in the same
    process (the tests) gets the time of the call.
    """
    started = time.monotonic() if started is None else started
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    code, out, err = answer_before_deadline(argv, stdin, started + DEADLINE_SECONDS)
    if "--timing" in argv:
        elapsed_ms = (time.monotonic() - started) * 1000
        stderr.write(f"gate_hook timing: {elapsed_ms:.1f} ms from process start, "
                     f"deadline {DEADLINE_SECONDS * 1000:.0f} ms\n")
    stdout.write(out)
    stderr.write(err)
    return code


if __name__ == "__main__":
    try:
        code = main(started=process_started_at())
    except BaseException:  # noqa: BLE001 - whatever happens, a request the check cannot judge is refused
        sys.stdout.write(parent_refusal(option_value(sys.argv[1:], "--event"), UNREADABLE) + "\n")
        sys.stderr.write(UNREADABLE + "\n")
        code = 2
    sys.exit(code)
