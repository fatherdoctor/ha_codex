"""The protected permission policy: what the worker allows, read from a file Codex cannot edit.

The policy ships inside the app image (read-only, outside /config and outside
every sandbox's writable roots). The worker reads it at startup and before
every exchange. Nothing in /config, the app options or a chat message can
loosen it: a setting the policy does not allow stops the worker at startup and
refuses each task with a "needs support" state.

Owner instructions remain in /config/AGENTS.md; no house rules ship with the app.
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
    "approval_policy": "on-request",
    "approvals_reviewer": "user",
    "approvals_require_admin": True,
}

NEEDS_SUPPORT = "needs_support"


class PolicyError(ValueError):
    """The protected policy file is missing, unreadable or loosens a fixed rule."""


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Return the protected policy; raise PolicyError when it cannot be read or weakens a fixed rule."""
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
    # These hold whatever the file says: the fork never ships an approval-free mode.
    if "danger-full-access" in modes:
        raise PolicyError("The protected policy may not allow danger-full-access.")
    if policy["approval_policy"] == "never":
        raise PolicyError('The protected policy may not set approval_policy to "never".')
    if policy["approvals_reviewer"] != "user":
        raise PolicyError('The protected policy must keep approvals_reviewer set to "user".')
    if policy["approvals_require_admin"] is not True:
        raise PolicyError("The protected policy must keep approvals_require_admin on.")
    return policy


def effective_policy(options: dict[str, Any], policy: dict[str, Any]) -> dict[str, str]:
    """Return the sandbox mode and approval policy an exchange would run with under these options."""
    return {
        "sandbox": str(options.get("codex_sandbox") or "workspace-write"),
        "approval_policy": str(policy["approval_policy"]),
        "approvals_reviewer": str(policy["approvals_reviewer"]),
    }


def policy_problem(options: dict[str, Any], path: Path | None = None) -> str:
    """Return why these options may not run under the protected policy, or an empty string when they may."""
    try:
        policy = load_policy(path)
    except PolicyError as exc:
        return str(exc)
    effective = effective_policy(options, policy)
    if effective["sandbox"] not in policy["allowed_sandbox_modes"]:
        allowed = " or ".join(policy["allowed_sandbox_modes"])
        return (f'The sandbox mode "{effective["sandbox"]}" is not allowed in this app. '
                f"Choose {allowed} in the app's settings and restart it.")
    if effective["approval_policy"] == "never":
        return 'The effective approval policy is "never", which this app refuses.'
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
