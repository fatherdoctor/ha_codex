"""Keep tests off the app's real folders: /data, /homeassistant and /addon_config.

On an install these folders hold the owner's chats, sign-in and Home Assistant configuration. A test that runs
server.main() calls the start-up steps that move and delete files there, so it must run against a temporary copy of
the layout. isolated_app_folders() gives a test that copy; snapshot() and changes() let conftest.py fail any test
that still writes under the real folders.
"""
import contextlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

# /config is Home Assistant's folder in versions before 0.6; it is kept here so that no test makes it either.
REAL_ROOTS = (Path("/data"), Path("/homeassistant"), Path("/addon_config"), Path("/config"))


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def isolated_app_folders(module, base: Path) -> contextlib.ExitStack:
    """Point every module-level path of module below a real root at the same place below base, from this call on.

    The replacements start at once and end when the returned stack closes (use it in a with statement, or pass its
    close method to addCleanup).

    /data/codex-home becomes base/data/codex-home, /homeassistant/www/codex_cli_auth becomes base/homeassistant/www/codex_cli_auth,
    and so on. Paths built later from DATA_ROOT or CONFIG_ROOT follow, because those two are replaced too.
    """
    stack = contextlib.ExitStack()
    worker = Path(module.__file__).resolve().parent
    for root in REAL_ROOTS:
        (base / root.name).mkdir(parents=True, exist_ok=True)
    for name, value in sorted(vars(module).items()):
        moved = _moved(value, base)
        if moved is not None:
            stack.enter_context(patch.object(module, name, moved))
            continue
        # Objects made at import keep their own copy of a path (the interaction log keeps /data/...json). Only
        # objects of the worker's own classes are read: a library object such as Flask's request proxy is left alone.
        if not _worker_object(value, worker):
            continue
        attributes = vars(value)
        for attribute, inner in sorted(attributes.items()):
            moved = _moved(inner, base)
            if moved is not None:
                stack.enter_context(patch.object(value, attribute, moved))
    return stack


def _worker_object(value, worker: Path) -> bool:
    """Return True for an instance of a class defined in a file of the worker folder."""
    if isinstance(value, type):
        return False
    defined_in = getattr(sys.modules.get(type(value).__module__), "__file__", None)
    if not defined_in or Path(defined_in).resolve().parent != worker:
        return False
    return isinstance(getattr(value, "__dict__", None), dict)


def _moved(value, base: Path):
    """Return value moved below base when it is a path below a real root, else None."""
    if not isinstance(value, Path):
        return None
    for root in REAL_ROOTS:
        if _under(value, root):
            return base / root.name / value.relative_to(root)
    return None


def snapshot(roots=REAL_ROOTS) -> dict[str, tuple]:
    """Return every file and folder below each root with its size and change time; a missing root maps to None."""
    found = {}
    for root in roots:
        if not os.path.lexists(root):
            found[str(root)] = None
            continue
        entries = []
        for folder, dirs, files in os.walk(root):
            for name in dirs + files:
                path = os.path.join(folder, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                entries.append((path, info.st_size, info.st_mtime_ns))
        found[str(root)] = tuple(sorted(entries))
    return found


def changes(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """Return the roots whose contents differ between two snapshots."""
    return [root for root in before if before[root] != after.get(root)]
