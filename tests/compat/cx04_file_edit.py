#!/usr/bin/env python3
"""cx-04 file edit: a chat adds one line to a scratch YAML file under /config/compat/.

Passes when the line is in the file, the rest of the file is unchanged, and the two checks the chat page promises
after an edit ran: the app saved a copy of the file's previous version, and Home Assistant's configuration check
answered valid (DOCS.md, "Backups" and "Validation").

The step writes the scratch file through COMPAT_LAB_SHELL first, so the chat changes a file that already exists
(only an existing file has a previous version to save), and reads it back the same way. The file is not part of
the lab's configuration; nothing loads it.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import lab as compat

SCRATCH = "/config/compat/scratch.yaml"
SCRATCH_RELATIVE = "compat/scratch.yaml"
SCRATCH_TEXT = "# Scratch file for the compatibility check cx-04. Safe to delete.\ncompat_scratch: true\n"
TURN_TIMEOUT = 210


def write_scratch(lab: compat.Lab) -> None:
    """Write the scratch file with its known text, so the chat changes a file that exists."""
    lab.sh(
        f"mkdir -p /config/compat && cat > {SCRATCH} <<'SCRATCH'\n{SCRATCH_TEXT}SCRATCH\n"
    )


def judge(task: dict[str, Any], text: str, line: str) -> tuple[int, str]:
    """Check the finished chat and the file against the pass rule."""
    if task.get("permission_required"):
        return (
            compat.FAIL,
            "the chat asked for permission to edit a file in /config in the default mode",
        )
    if task.get("status") != "completed":
        return (
            compat.FAIL,
            f"the turn ended as {task.get('status')}: {task.get('summary') or task.get('error')}",
        )
    changed = (task.get("changes") or {}).get("changed") or []
    if SCRATCH_RELATIVE not in changed:
        return (
            compat.FAIL,
            f"the chat does not list {SCRATCH_RELATIVE} as changed: {changed}",
        )
    backups = {
        entry.get("path"): entry.get("status") for entry in task.get("backups") or []
    }
    if backups.get(SCRATCH_RELATIVE) != "saved":
        return (
            compat.FAIL,
            f"no saved copy of the previous version: {backups.get(SCRATCH_RELATIVE) or 'no entry'}",
        )
    check = (task.get("config_check") or {}).get("result")
    if check != "valid":
        return (
            compat.FAIL,
            f"Home Assistant's configuration check did not answer valid: {check}",
        )
    lines = text.splitlines()
    if line not in lines:
        return compat.FAIL, "the line is not in the file"
    # Exactly the original lines and then the one line, as the prompt asks: not two copies, not at the start.
    if lines != SCRATCH_TEXT.splitlines() + [line]:
        return compat.FAIL, "the file is not the original lines with the one line added at the end"
    return (
        compat.PASS,
        "the line is in the file; the previous version was saved; the configuration check is valid",
    )


def step(lab: compat.Lab, args: argparse.Namespace) -> str:
    """Write the scratch file, ask a chat to add the line, then judge the chat and the file."""
    slug = lab.addon_slug()
    if slug is None:
        raise compat.failed("the app is not in the store; cx-01 installs it")
    worker = lab.worker(slug)
    write_scratch(lab)
    line = f"compat_marker_{int(time.time())}: added"
    prompt = (
        "This is an automated compatibility check. Add this exact line at the end of the file "
        f"{SCRATCH} and change nothing else: {line}"
    )
    started = worker.call(
        "POST", "/tasks", {"prompt": prompt, "title": "Compatibility check cx-04"}
    )
    if not started.get("ok"):
        raise compat.failed(f"the app did not start the chat: {started.get('error')}")
    task = worker.wait_for_task(started["task_id"], TURN_TIMEOUT)
    code, why = judge(task, lab.sh(f"cat {SCRATCH}"), line)
    if code != compat.PASS:
        raise compat.StepResult(code, why)
    return f"{why} (chat {started['task_id']})"


if __name__ == "__main__":
    compat.run_step("cx-04 file edit", step)
