"""Tag a release only after its lab pass, and roll back by releasing the last good code under a new version.

Commands (run from a clone of fatherdoctor/ha_codex):

  fork_release.py check-tag <version>
      Show whether <version> may be tagged, and which commit the tag would point at. Changes nothing.

  fork_release.py tag <version> [--push]
      Create the annotated tag v<version>. A normal release needs a lab-pass record,
      release/lab-passes/v<version>.json on HEAD, naming the exact commit that passed; the tag goes on that
      commit. A rollback release (see below) needs no new lab pass: its record names the version it restores,
      this script proves the code is that version's code, and the tag goes on the commit that added the record. --push sends the tag to origin, which starts the
      release workflow when it is enabled.

  fork_release.py rollback <good-version> [--to <new-version>] [--accept-stored-data]
      Prepare a roll-forward rollback in the working tree: the app (codex-cli-worker/) and the integration
      (custom_components/) as they were at tag v<good-version>, under the next version number, with a changelog
      entry and a rollback record. Home Assistant offers it as an ordinary update, so nobody at the house has to
      do anything. Review, commit, open a pull request and merge it; then run `tag` on main.

Docs: docs/updates-and-rollback.md.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_CONFIG = "codex-cli-worker/config.yaml"
MANIFEST = "custom_components/codex_cli/manifest.json"
CHANGELOG = "codex-cli-worker/CHANGELOG.md"
CODE_PATHS = ("codex-cli-worker", "custom_components")
RECORD_DIR = "release/lab-passes"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
CONFIG_VERSION = re.compile(r'^version:\s*"?(?P<version>[^"\n]+?)"?\s*$', re.MULTILINE)
MANIFEST_VERSION = re.compile(r'^(?P<lead>\s*"version":\s*")(?P<version>[^"]+)(?P<trail>")', re.MULTILINE)
# A changelog bullet that starts this way marks a release whose stored data an older build may not read.
STORED_DATA_MARK = "Stored data:"


class ReleaseError(Exception):
    """A release rule was not met; the message says which."""


def git(*args: str, root: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout if result.returncode == 0 else ""


def show(rev: str, path: str, root: Path = ROOT) -> str:
    result = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=root, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ReleaseError(f"{path} does not exist at {rev}")
    return result.stdout


def check_version_text(version: str) -> None:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseError(f"expected a version like 0.1.81.2, got {version!r}")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def next_version(current: str) -> str:
    """The fork's next build number: upstream's version plus one more fork build (0.1.81.1 -> 0.1.81.2)."""
    check_version_text(current)
    parts = current.split(".")
    if len(parts) == 3:
        return f"{current}.1"
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def versions_at(rev: str, root: Path = ROOT) -> tuple[str, str]:
    config = CONFIG_VERSION.search(show(rev, WORKER_CONFIG, root))
    manifest = json.loads(show(rev, MANIFEST, root)).get("version")
    if config is None or not manifest:
        raise ReleaseError(f"no version in {WORKER_CONFIG} or {MANIFEST} at {rev}")
    return config.group("version"), manifest


def changelog_sections(text: str) -> list[tuple[str, list[str]]]:
    """[(version, lines)] in file order, newest first by convention."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        heading = re.match(r"^## (\S+)", line)
        if heading:
            sections.append((heading.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def check_release_files(version: str, rev: str, root: Path = ROOT) -> None:
    """The app, the integration and the changelog all say <version> at <rev>."""
    config, manifest = versions_at(rev, root)
    if config != version or manifest != version:
        raise ReleaseError(
            f"at {rev[:12]} the app says {config} and the integration says {manifest}, not {version}"
        )
    for heading, lines in changelog_sections(show(rev, CHANGELOG, root)):
        if heading == version:
            if not "\n".join(lines).strip():
                raise ReleaseError(f"{CHANGELOG} has a heading for {version} but no notes")
            return
    raise ReleaseError(f"{CHANGELOG} has no heading for {version} at {rev[:12]}")


@dataclass
class Record:
    version: str
    commit: str
    result: str
    kind: str
    rollback_of: str | None
    raw: dict


def read_record(version: str, root: Path = ROOT, rev: str = "HEAD") -> Record:
    path = f"{RECORD_DIR}/v{version}.json"
    try:
        data = json.loads(show(rev, path, root))
    except ReleaseError:
        raise ReleaseError(f"no lab-pass record {path} on {rev}: run the lab test first") from None
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{path} is not valid JSON: {exc}") from None
    if data.get("version") != version:
        raise ReleaseError(f"{path} is for version {data.get('version')!r}, not {version}")
    kind = data.get("kind", "lab-pass")
    if kind not in ("lab-pass", "rollback"):
        raise ReleaseError(f"{path} has unknown kind {kind!r}")
    return Record(
        version=version,
        commit=str(data.get("commit", "")),
        result=str(data.get("result", "")),
        kind=kind,
        rollback_of=data.get("rollback_of"),
        raw=data,
    )


def resolve_commit(rev: str, root: Path = ROOT) -> str:
    sha = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", root=root, check=False).strip()
    if not sha:
        raise ReleaseError(f"{rev!r} is not a commit in this clone (fetch first?)")
    return sha


def is_ancestor(commit: str, of: str, root: Path = ROOT) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, of], cwd=root, check=False).returncode == 0


def tag_exists(tag: str, root: Path = ROOT) -> bool:
    return bool(git("tag", "--list", tag, root=root).strip())


def without_version(path: str, text: str) -> str:
    if path.endswith("config.yaml"):
        return CONFIG_VERSION.sub("version: <version>", text, count=1)
    if path.endswith("manifest.json"):
        return MANIFEST_VERSION.sub(r"\g<lead><version>\g<trail>", text, count=1)
    return text


def check_same_code(good_rev: str, new_rev: str, root: Path = ROOT) -> None:
    """Everything under the app and the integration is identical, apart from the version and the changelog."""
    changed = git("diff", "--name-only", good_rev, new_rev, "--", *CODE_PATHS, root=root).split()
    for path in changed:
        if path == CHANGELOG:
            continue
        if path in (WORKER_CONFIG, MANIFEST) and (
            without_version(path, show(good_rev, path, root)) == without_version(path, show(new_rev, path, root))
        ):
            continue
        raise ReleaseError(f"rollback differs from {good_rev} in {path}, not only in its version")


def plan_tag(version: str, root: Path = ROOT, base: str = "HEAD") -> tuple[str, str]:
    """Return (commit to tag, why it may be tagged), or raise ReleaseError."""
    check_version_text(version)
    tag = f"v{version}"
    if tag_exists(tag, root):
        raise ReleaseError(f"{tag} already exists; a release is never re-tagged (release the next version)")
    record = read_record(version, root, base)
    if record.result != "pass":
        raise ReleaseError(f"the lab-pass record for {version} says result {record.result!r}, not 'pass'")
    if record.kind == "rollback":
        # A rollback is tagged on the commit that added its record: the merged rollback itself.
        record.commit = git("log", "-1", "--format=%H", base, "--", f"{RECORD_DIR}/v{version}.json", root=root).strip()
    elif re.fullmatch(r"[0-9a-f]{40}", record.commit) is None:
        raise ReleaseError(f"the record for {version} must name the full 40-character commit it covers")
    commit = resolve_commit(record.commit, root)
    if not is_ancestor(commit, base, root):
        raise ReleaseError(f"{commit[:12]} is not on {base}: tag only merged commits")
    check_release_files(version, commit, root)
    newest = max((t[1:] for t in git("tag", "--list", "v*", root=root).split() if VERSION_PATTERN.fullmatch(t[1:])),
                 key=version_key, default=None)
    if newest is not None and version_key(version) <= version_key(newest):
        raise ReleaseError(f"{version} is not newer than {newest}; Home Assistant would not offer it as an update")
    if record.kind == "lab-pass":
        return commit, f"lab pass recorded in {RECORD_DIR}/v{version}.json"
    good = record.rollback_of
    if not good:
        raise ReleaseError(f"rollback record for {version} does not say which version it restores")
    good_record = read_record(good, root, base)
    if good_record.kind != "lab-pass" or good_record.result != "pass":
        raise ReleaseError(f"{good} has no lab pass of its own; roll back only to a build that passed")
    good_tag = f"v{good}"
    if not tag_exists(good_tag, root):
        raise ReleaseError(f"{good_tag} is not tagged here; fetch tags first")
    check_same_code(good_tag, commit, root)
    return commit, f"rollback: same app and integration code as {good_tag} (lab pass in {RECORD_DIR}/v{good}.json)"


def cmd_check_tag(args: argparse.Namespace) -> int:
    commit, why = plan_tag(args.version)
    print(f"v{args.version} may be tagged on {commit}: {why}")
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    commit, why = plan_tag(args.version)
    tag = f"v{args.version}"
    git("tag", "-a", tag, commit, "-m", f"{tag}\n\n{why}")
    print(f"tagged {tag} on {commit}: {why}")
    if args.push:
        git("push", "origin", tag)
        print(f"pushed {tag}")
    else:
        print(f"not pushed; to publish: git push origin {tag}")
    return 0


def stored_data_changes(changelog: str, good: str) -> list[str]:
    """Versions newer than <good> whose notes say the stored data changed."""
    found = []
    for heading, lines in changelog_sections(changelog):
        if heading == good:
            break
        if any(STORED_DATA_MARK in line for line in lines):
            found.append(heading)
    return found


def rollback(good: str, new: str | None, accept_stored_data: bool, root: Path = ROOT) -> str:
    check_version_text(good)
    good_tag = f"v{good}"
    if not tag_exists(good_tag, root):
        raise ReleaseError(f"{good_tag} is not tagged here; fetch tags first")
    if git("status", "--porcelain", root=root).strip():
        raise ReleaseError("the working tree has changes; start the rollback from a clean checkout")
    good_record = read_record(good, root)
    if good_record.kind != "lab-pass" or good_record.result != "pass":
        raise ReleaseError(f"{good} has no lab pass of its own; roll back only to a build that passed")
    current, _ = versions_at("HEAD", root)
    newest_tag = max((t[1:] for t in git("tag", "--list", "v*", root=root).split() if VERSION_PATTERN.fullmatch(t[1:])),
                     key=version_key)
    new = new or next_version(max(current, newest_tag, key=version_key))
    check_version_text(new)
    if version_key(new) <= max(version_key(current), version_key(newest_tag)):
        raise ReleaseError(f"{new} must be newer than {current} and {newest_tag}")
    changelog = (root / CHANGELOG).read_text(encoding="utf-8")
    risky = stored_data_changes(changelog, good)
    if risky and not accept_stored_data:
        raise ReleaseError(
            f"releases {', '.join(risky)} changed stored data that {good} may not read; "
            "check that by hand, then pass --accept-stored-data"
        )

    git("restore", f"--source={good_tag}", "--staged", "--worktree", "--", *CODE_PATHS, root=root)
    (root / CHANGELOG).write_text(changelog, encoding="utf-8")
    config_path = root / WORKER_CONFIG
    config_path.write_text(CONFIG_VERSION.sub(f'version: "{new}"', config_path.read_text(encoding="utf-8"), count=1),
                           encoding="utf-8")
    manifest_path = root / MANIFEST
    manifest_path.write_text(
        MANIFEST_VERSION.sub(rf"\g<lead>{new}\g<trail>", manifest_path.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
    )
    entry = (
        f"## {new}\n\n"
        f"- Rollback: the app and the integration are exactly {good} again, released under a new number so "
        f"Home Assistant offers it as an ordinary update. Nothing needs to be done at the house; update as usual.\n\n"
    )
    first = changelog.find("\n## ")
    head, rest = (changelog[: first + 1], changelog[first + 1 :]) if first >= 0 else (changelog + "\n", "")
    (root / CHANGELOG).write_text(head + entry + rest, encoding="utf-8")
    record_path = root / RECORD_DIR / f"v{new}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "version": new,
                "kind": "rollback",
                "rollback_of": good,
                "result": "pass",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    git("add", "--", *CODE_PATHS, RECORD_DIR, root=root)
    return new


def cmd_rollback(args: argparse.Namespace) -> int:
    new = rollback(args.good, args.to, args.accept_stored_data)
    print(f"prepared {new}: the code of v{args.good}, staged. Commit, open a pull request and merge it; then")
    print(f"on main: python3 scripts/fork_release.py tag {new} --push")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-tag", help="show whether a version may be tagged")
    check.add_argument("version")
    check.set_defaults(func=cmd_check_tag)
    tag = sub.add_parser("tag", help="tag a version that passed the lab test, or a verified rollback")
    tag.add_argument("version")
    tag.add_argument("--push", action="store_true", help="push the tag to origin")
    tag.set_defaults(func=cmd_tag)
    back = sub.add_parser("rollback", help="prepare the last good code under the next version")
    back.add_argument("good", help="the version to restore, for example 0.1.81.1")
    back.add_argument("--to", help="the new version number (default: the next fork build)")
    back.add_argument("--accept-stored-data", action="store_true",
                      help="go ahead although a newer release changed stored data")
    back.set_defaults(func=cmd_rollback)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
