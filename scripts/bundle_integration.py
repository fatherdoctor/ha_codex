"""Copy the integration into the app's build folder, or check that the copy there is current.

The app image carries the integration of the same release (codex-cli-worker/integration/README.md). Its only source
is `custom_components/codex_cli`; the copy under `codex-cli-worker/integration/codex_cli` is made just before
`docker build` and is not kept in git. A local build that skips the copy, or reuses an old one, puts a stale
integration in the image even when both say the same version, and the app then installs that stale copy in /homeassistant.
This script is the one copy step, for the workflows and for a build by hand.

Usage:

  bundle_integration.py           replace the build folder's copy with the source
  bundle_integration.py --check   change nothing; report whether the copy matches the source

Exit 0 when the copy was made or matches, 1 when the check finds the copy missing or different (each different
file is printed), 2 when the script cannot run.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "codex_cli"
BUNDLE = ROOT / "codex-cli-worker" / "integration" / "codex_cli"

# Python caches differ between machines and never ship; the app's own sync ignores them too (integration_sync.py).
IGNORED_NAMES = frozenset({"__pycache__"})


def files_of(folder: Path) -> dict[str, bytes]:
    """Return each file under the folder by its relative path, without the ignored names."""
    found = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if IGNORED_NAMES.intersection(relative.parts) or not path.is_file():
            continue
        found[relative.as_posix()] = path.read_bytes()
    return found


def differences(source: Path, bundle: Path) -> list[str]:
    """Return one line for each file that is missing, extra or changed in the bundle; empty when they match."""
    if not bundle.is_dir():
        return [f"no bundled copy at {bundle}"]
    want, have = files_of(source), files_of(bundle)
    lines = [f"missing: {name}" for name in sorted(want.keys() - have.keys())]
    lines += [f"extra: {name}" for name in sorted(have.keys() - want.keys())]
    lines += [f"changed: {name}" for name in sorted(want.keys() & have.keys()) if want[name] != have[name]]
    return lines


def copy(source: Path, bundle: Path) -> None:
    """Replace the bundle with a fresh copy of the source, so no file of an older copy stays behind."""
    shutil.rmtree(bundle, ignore_errors=True)
    shutil.copytree(source, bundle, ignore=shutil.ignore_patterns(*IGNORED_NAMES))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="change nothing; compare the copy with the source")
    args = parser.parse_args(argv)
    if not (SOURCE / "manifest.json").is_file():
        print(f"bundle_integration: no integration source at {SOURCE}", file=sys.stderr)
        return 2
    try:
        if not args.check:
            copy(SOURCE, BUNDLE)
        lines = differences(SOURCE, BUNDLE)
    except OSError as exc:
        print(f"bundle_integration: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    if lines:
        print("bundle_integration: the bundled integration is not the source; run scripts/bundle_integration.py")
        return 1
    print(f"bundle_integration: {BUNDLE.relative_to(ROOT)} matches {SOURCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
