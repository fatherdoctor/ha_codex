"""The protected permission policy: what the worker allows, read from a file Codex cannot edit.

The policy ships inside the app image (read-only, outside /config and outside
every sandbox's writable roots). The worker reads it at startup and before
every exchange. Nothing in /config, the app options or a chat message can
loosen it: a setting the policy does not allow stops the worker at startup and
refuses each task with a "needs support" state.

Owner instructions live in the app's own folder, /addon_config/AGENTS.md; no house
rules ship with the app. The starter text a new install gets there (server.py: write_starter_agents_file)
is an owner file from then on and is not part of this policy.
"""

from __future__ import annotations

import json
import errno
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

IMAGE_DIR = Path("/usr/share/ha_codex")
SOURCE_DIR = Path(__file__).resolve().parent / "policy"


def _shipped(name: str, source_name: str) -> Path:
    """Return the image's copy of a policy file; a source checkout (tests, development) uses the one in policy/."""
    image_copy = IMAGE_DIR / name
    source = SOURCE_DIR / source_name
    return image_copy if image_copy.exists() or not source.exists() else source


POLICY_PATH = _shipped("policy.json", "policy.json")
# Public fingerprint of the removed bundled text, for upgrade cleanup only.
LEGACY_INSTRUCTIONS_SHA256 = frozenset({
    (
        '18215b691022cb4b'
        'c23dcc1fef71bf2a'
        '5ac60d6ce9a6bcd2'
        'f9754deb729662b2'
    ),
    (
        '9f75e46225425a42'
        'e9b543f5d19abe4b'
        '6c567657b1e53a6d'
        '7a28f20290bd17a7'
    ),
    (
        'ac4e2a269dcd7f0b'
        '3f0edd9f569cb39c'
        '95f02724b20221ec'
        'ae9fc98df217503f'
    ),
})

# Used only when the policy file is missing from the image, which is itself a
# "needs support" fault: the defaults are the strict ones.
STRICT_POLICY: dict[str, Any] = {
    "allowed_sandbox_modes": ["workspace-write", "read-only"],
    "approvals_require_admin": True,
}

NEEDS_SUPPORT = "needs_support"

# The four permission modes of the ChatGPT app, with the same names. Each one is
# only the Codex settings below, passed when the worker starts Codex for a chat
# turn. The worker adds no check of its own to any of them (David 2026-11-17:
# "just pass the flags"). The app option codex_sandbox can narrow the sandbox of
# Default and Auto-review to read-only (mode_settings).
MODES: dict[str, dict[str, str]] = {
    "default": {"sandbox": "workspace-write", "approval_policy": "on-request", "approvals_reviewer": "user"},
    "auto-review": {"sandbox": "workspace-write", "approval_policy": "on-request",
                    "approvals_reviewer": "auto_review"},
    "read-only": {"sandbox": "read-only", "approval_policy": "on-request", "approvals_reviewer": "user"},
    "full-access": {"sandbox": "danger-full-access", "approval_policy": "never", "approvals_reviewer": "user"},
}
DEFAULT_MODE = "default"
FULL_ACCESS_MODE = "full-access"


class PolicyError(ValueError):
    """The protected policy file is missing, unreadable or malformed."""


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Return the protected policy; raise PolicyError when it cannot be read."""
    path = path or POLICY_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"The protected policy file {path} is missing from the app image.") from None
    except (OSError, ValueError) as exc:
        raise PolicyError(f"The protected policy file {path} could not be read: {exc}") from None
    if not isinstance(data, dict):
        raise PolicyError(f"The protected policy file {path} is not a JSON object.")
    policy = {**STRICT_POLICY, **data}
    modes = policy["allowed_sandbox_modes"]
    if not isinstance(modes, list) or not modes or not all(isinstance(mode, str) for mode in modes):
        raise PolicyError("allowed_sandbox_modes must be a non-empty list of sandbox modes.")
    return policy


def mode_settings(mode: str, app_sandbox: str = "workspace-write") -> dict[str, str]:
    """Return the Codex settings of one permission mode; an unknown mode gets Default's.

    The app option codex_sandbox (app_sandbox) stays the limit for every chat:
    when it is read-only, a Default or Auto-review chat runs in the read-only
    sandbox too. Full access is the owner's choice for one chat (server.py
    allows it only while the Full access switch is on) and keeps its own
    settings.
    """
    settings = dict(MODES.get(mode, MODES[DEFAULT_MODE]))
    if app_sandbox == "read-only" and settings["sandbox"] == "workspace-write":
        settings["sandbox"] = "read-only"
    return settings


def policy_problem(options: dict[str, Any], path: Path | None = None) -> str:
    """Return why the app's sandbox option may not run under the protected policy, or an empty string."""
    try:
        policy = load_policy(path)
    except PolicyError as exc:
        return str(exc)
    sandbox = str(options.get("codex_sandbox") or "workspace-write")
    if sandbox not in policy["allowed_sandbox_modes"]:
        allowed = " or ".join(policy["allowed_sandbox_modes"])
        return (f'The sandbox mode "{sandbox}" is not allowed in this app. '
                f"Choose {allowed} in the app's settings and restart it.")
    return ""


def remove_legacy_instructions(codex_home: Path) -> bool:
    """Remove only an exact old bundled copy; preserve owner edits and symlinks."""
    target = codex_home / "AGENTS.md"
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError as error:
        if error.errno == errno.ELOOP:  # Never follow a symlink.
            return False
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            return False
        if hashlib.sha256(stream.read()).hexdigest() not in LEGACY_INSTRUCTIONS_SHA256:
            return False
    current = target.lstat()
    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
        return False
    target.unlink()
    return True
