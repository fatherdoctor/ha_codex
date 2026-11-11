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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The image keeps gate_rules.py next to this script; a source checkout keeps it one folder up. Python runs
# with -I, which leaves the script's folder off the path. The import happens in main(), so that a missing
# or broken rules module refuses the request instead of failing with an exit code Codex would ignore.
HERE = Path(__file__).resolve().parent
RULES_DIR = HERE if (HERE / "gate_rules.py").is_file() else HERE.parent

DEFAULT_STATE = Path("/data/remote_control.json")
UNREADABLE = "The command check could not read this request, so it is refused."


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


def main(argv: list[str] | None = None, stdin=None, stdout=None, stderr=None) -> int:
    """Read one hook event, write a refusal or nothing, and return the exit code (2 refuses, 0 passes)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", default="", help="the hook event, when the input does not name it")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="the app's saved remote-control choice")
    parser.add_argument("--rules", default="", help="the refused-domain list (default: the image's)")
    args = parser.parse_args(argv)
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
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
    if not reason:
        return 0
    stdout.write(json.dumps(refusal(event, reason)) + "\n")
    stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as exc:  # argparse: a bad command line refuses too
        code = 0 if exc.code == 0 else 2
    except BaseException:  # noqa: BLE001 - whatever happens, a request the check cannot judge is refused
        sys.stderr.write(UNREADABLE + "\n")
        code = 2
    sys.exit(code)
