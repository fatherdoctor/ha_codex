"""Full access: an owner switch that lets a chat run Codex without its sandbox.

Off by default. When it is on, a chat whose permission mode is Full access runs
Codex as the ChatGPT app's Full access does: the danger-full-access sandbox and
the "never" approval policy (policy.MODES). Its commands reach /config, the
network and everything the app's container can reach, with no sandbox around
them, and Codex does not ask before it runs them. The switch is a warning the
owner confirms, not a grant layer: it adds no rule to any mode.

Codex's managed requirements must allow that sandbox and that approval policy,
so the worker writes them at start, and again on each change, from the copy
that ships in the image. While the setting is off the written file is that
shipped copy, byte for byte.

The setting is kept in /data, which no sandboxed command can write, so a
sandboxed Codex cannot turn it on. It counts as on only when the file holds
exactly what set_enabled() writes after an administrator confirmed the warning.
A missing, unreadable, linked or edited file is off.
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any

SETTING_PATH = Path("/data/full_access.json")
REQUIREMENTS_PATH = Path("/etc/codex/requirements.toml")
SOURCE_REQUIREMENTS = Path(__file__).resolve().parent / "gate" / "requirements.toml"
IMAGE_REQUIREMENTS = Path("/usr/share/ha_codex/requirements.toml")

# The administrator's confirmation names this text. A new wording gets a new
# id, so a setting saved under an older warning is off until confirmed again.
WARNING_ID = "full-access-warning-2"
WARNING = (
    "Full access runs Codex's commands outside the sandbox. They can read and change everything in /config, "
    "use the network, and reach everything this app's container can reach, without asking first. "
    "Turn it on only if you accept that risk."
)

# The one line of the shipped requirements that names the allowed sandbox modes.
STRICT_MODES_LINE = 'allowed_sandbox_modes = ["read-only", "workspace-write"]'
OPEN_MODES_LINE = 'allowed_sandbox_modes = ["read-only", "workspace-write", "danger-full-access"]'
# The one line that names the allowed approval policies.
STRICT_APPROVALS_LINE = 'allowed_approval_policies = ["on-request"]'
OPEN_APPROVALS_LINE = 'allowed_approval_policies = ["on-request", "never"]'
SETTING_KEYS = {"enabled", "warning", "changed_at", "changed_by"}


def read_setting(path: Path | None = None) -> dict[str, Any]:
    """Return the saved setting, or the default (off) when the file is missing or not one set_enabled() wrote."""
    path = path or SETTING_PATH
    off = {"enabled": False, "changed_at": None, "changed_by": ""}
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return off
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return off
    if not isinstance(data, dict) or set(data) != SETTING_KEYS:
        return off
    if data["enabled"] is not True or data["warning"] != WARNING_ID:
        return off
    # bool is a kind of int in Python, and NaN is a float, so each is refused by name.
    changed_at = data["changed_at"]
    if isinstance(changed_at, bool) or not isinstance(changed_at, (int, float)) or not math.isfinite(changed_at):
        return off
    if not isinstance(data["changed_by"], str):
        return off
    return {"enabled": True, "changed_at": data["changed_at"], "changed_by": data["changed_by"]}


def is_on(path: Path | None = None) -> bool:
    """Return True only when an administrator turned Full access on and confirmed the current warning."""
    return read_setting(path)["enabled"]


def set_enabled(enabled: bool, changed_by: str, path: Path | None = None) -> dict[str, Any]:
    """Save the setting, readable and writable by the worker only, and return it as read_setting() sees it.

    The caller checks that the request comes from an administrator and that,
    to turn it on, the administrator confirmed WARNING_ID.
    """
    path = path or SETTING_PATH
    if not enabled:
        path.unlink(missing_ok=True)
        return read_setting(path)
    data = {"enabled": True, "warning": WARNING_ID, "changed_at": time.time(), "changed_by": changed_by}
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)
    return read_setting(path)


def requirements_text(enabled: bool, shipped: str) -> str:
    """Return the managed requirements for the setting: the shipped text, with full access added only when on.

    On, the text allows the danger-full-access sandbox and the "never" approval
    policy; nothing else changes. Raise ValueError when the shipped text does
    not hold each expected line exactly once, so a changed file is never edited
    by guesswork.
    """
    if not enabled:
        return shipped
    if shipped.count(STRICT_MODES_LINE) != 1:
        raise ValueError("The shipped requirements do not hold the expected sandbox modes line.")
    if shipped.count(STRICT_APPROVALS_LINE) != 1:
        raise ValueError("The shipped requirements do not hold the expected approval policies line.")
    return shipped.replace(STRICT_MODES_LINE, OPEN_MODES_LINE).replace(STRICT_APPROVALS_LINE, OPEN_APPROVALS_LINE)


def write_requirements(target: Path | None = None, shipped: Path | None = None, setting: Path | None = None) -> bool:
    """Write Codex's managed requirements for the current setting; return False when there is nothing to write.

    Outside the app image (tests, development) the target folder is missing and
    nothing is written. If the write fails while the setting is on, the old
    file stays and Codex keeps refusing full access, which is the safe side.
    """
    target = target or REQUIREMENTS_PATH
    shipped = shipped or (IMAGE_REQUIREMENTS if IMAGE_REQUIREMENTS.is_file() else SOURCE_REQUIREMENTS)
    if not target.parent.is_dir():
        return False
    text = requirements_text(is_on(setting), shipped.read_text(encoding="utf-8"))
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
    return True
