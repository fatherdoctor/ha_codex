"""Refuse a pull request that changes the version, unless it is the release pull request for that version.

The add-on store offers an update as soon as `codex-cli-worker/config.yaml` on `main` names a newer version, so a
feature pull request that raises the version makes the store offer a release that does not exist yet. The version
changes only in a release pull request, whose title starts with `Release <version>` (for example
`Release 0.3.6` or `Release 0.3.6: roll back to 0.3.4`). Rules: CONTRIBUTING.md, "Releases".

Usage (the Version rule workflow runs it on each pull request to main):

  check_version_bump.py --base <rev> --head <rev> --title <pull request title>

The versions are compared between the merge base of the two revisions and the head, so a release merged to main
after the branch started does not count against the branch. Exit 0 when the pull request keeps the rule, 1 when it
breaks it, 2 when the versions cannot be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from fork_release import MANIFEST, ROOT, WORKER_CONFIG, ReleaseError, git, is_beta, versions_at

# "Release 0.3.6", "Release v0.3.6", "Release 0.3.6: roll back to 0.3.4". The word is case-sensitive on purpose:
# a title such as "release notes for the beta" is not a release pull request.
RELEASE_TITLE = re.compile(r"Release v?(?P<version>[^\s:]+)(?::|\s|$)")


class VersionRuleError(Exception):
    """The pull request changes the version and is not the release pull request for it."""


def release_version(title: str) -> str | None:
    """The version a release pull request title names, or None when the title is not a release title."""
    match = RELEASE_TITLE.match(title.strip())
    return match.group("version") if match else None


def check(base: str, head: str, title: str, root: Path = ROOT) -> str:
    """Return a one-line reason the pull request keeps the rule.

    Raise VersionRuleError when it breaks the rule, and ReleaseError when git or the version files cannot be read.
    """
    merge_base = git("merge-base", base, head, root=root).strip()
    if not merge_base:
        raise ReleaseError(f"no merge base between {base} and {head}; fetch more history")
    before = versions_at(merge_base, root)
    after = versions_at(head, root)
    if before == after:
        return f"ok: the version is unchanged ({after[0]})"

    named = release_version(title)
    files = f"{WORKER_CONFIG} {before[0]} -> {after[0]}, {MANIFEST} {before[1]} -> {after[1]}"
    if named is None:
        raise VersionRuleError(
            f"this pull request changes the version ({files}). The version changes only in the release pull "
            "request, whose title starts with 'Release <version>'. Set the version lines back to the ones on main."
        )
    if is_beta(named):
        raise VersionRuleError(f"{named} is a beta version; a beta is made on the beta branch, never on main")
    if after != (named, named):
        raise VersionRuleError(f"the title names {named}, but the head has {files}; both must be {named}")
    return f"ok: release pull request for {named} ({files})"


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the base revision (the pull request's base commit)")
    parser.add_argument("--head", required=True, help="the head revision (the pull request's head commit)")
    parser.add_argument("--title", required=True, help="the pull request title")
    args = parser.parse_args(argv)
    try:
        print(check(args.base, args.head, args.title, root))
    except VersionRuleError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
