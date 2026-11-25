"""Tell the owner once where their own text still names the old mount point after the move (CX-HA-PATH).

Version 0.6 maps Home Assistant's configuration folder at CONFIG_ROOT instead of OLD_CONFIG_ROOT. The app never rewrites the owner's text:
a wrong rewrite inside a prompt is worse than a path that fails with "No such file". At the first start of 0.6 it
counts the lines that name a path under OLD_CONFIG_ROOT in the owner's workspace instructions and scheduled prompts, logs
the counts and shows one notification. The log and the notification hold counts, places and schedule names only,
never the text. A marker in /data stops a second scan.

The working folder's own notes (/media/.agents/codex) join the places when release/0.4's working folder reaches
release/0.6: one more entry in places().

Remove this module, its marker and its call in server.main() in the first release after 0.7.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import re

from app_paths import CONFIG_ROOT, OLD_CONFIG_ROOT

MARKER_NAME = "config-path-scan.json"
SCAN_VERSION = "0.6"
NOTIFICATION_ID = "codex_cli_config_path"
NOTIFICATION_TITLE = f"Codex: Home Assistant's configuration is now at {CONFIG_ROOT}"
# A path under the old mount point, but not Home Assistant's address of an app's pages (OLD_CONFIG_ROOT/app/...),
# and not a name that ends in config (api/config, lovelace/config, .git/config).
OLD_PATH = re.compile(r"(?<![\w}>.\-/])" + re.escape(OLD_CONFIG_ROOT) + r"/(?!app/)")


@dataclass(frozen=True)
class Finding:
    """How many lines in one place name an old path; for schedules, which ones (by name)."""

    place: str
    lines: int
    schedules: tuple[str, ...] = ()


def count_old_paths(text: str) -> int:
    """Return the number of lines in text that name a path under the old mount point."""
    return sum(1 for line in text.splitlines() if OLD_PATH.search(line))


def find_old_paths(agents_path: Path, schedules: list[dict]) -> list[Finding]:
    """Count the old paths in the workspace instructions file and in each scheduled prompt."""
    findings = []
    try:
        lines = count_old_paths(agents_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        lines = 0
    if lines:
        findings.append(Finding(f"workspace instructions ({agents_path})", lines))
    counted = [
        (str(item.get("name") or ""), count_old_paths(str(item.get("prompt") or "")))
        for item in schedules
    ]
    named = [(name, lines) for name, lines in counted if lines]
    if named:
        findings.append(
            Finding(
                "scheduled prompts",
                sum(lines for _, lines in named),
                tuple(n for n, _ in named),
            )
        )
    return findings


def describe(finding: Finding) -> str:
    """One line for the log and the notification: the place, the count and the schedule names."""
    text = f"{finding.place}: {finding.lines} line(s) with {OLD_CONFIG_ROOT}/"
    return text + (
        f" (schedules: {', '.join(finding.schedules)})" if finding.schedules else ""
    )


def notification_message(findings: list[Finding]) -> str:
    """The notification text: what moved, where the owner's text still says the old path, and what to change."""
    places = "\n".join(f"- {describe(finding)}" for finding in findings)
    return (
        f"Inside the Codex app, Home Assistant's configuration folder is now {CONFIG_ROOT}, as in the Claude add-on. "
        f"{OLD_CONFIG_ROOT} no longer exists inside the app. Home Assistant and your files did not move.\n\n"
        f"Your own text still names {OLD_CONFIG_ROOT}/ here:\n{places}\n\n"
        f"Open the Codex app and change these paths to {CONFIG_ROOT}/. Leave {OLD_CONFIG_ROOT}/ in Home Assistant's "
        "own YAML as it is."
    )


def scan_once(
    data_root: Path,
    agents_path: Path,
    read_schedules: Callable[[], list[dict]],
    notify: Callable[[str, str, str], bool],
) -> list[Finding] | None:
    """Scan at the first start of this version only; return the findings, or None when the marker says it ran.

    A failure to read the schedules counts as none found, so a broken database never stops the start. notify returns
    whether the notice reached Home Assistant: the marker is written only when nothing was found or the notice was
    sent, so a notice that failed (Home Assistant still starting) is tried again at the next start.
    """
    marker = data_root / MARKER_NAME
    if marker.exists():
        return None
    try:
        schedules = read_schedules()
    except Exception as exc:  # noqa: BLE001 - the start goes on; the scan only reports.
        print(
            f"Config path scan: could not read the schedules ({type(exc).__name__}).",
            flush=True,
        )
        schedules = []
    findings = find_old_paths(agents_path, schedules)
    for finding in findings:
        print(f"Config path scan: {describe(finding)}.", flush=True)
    if findings:
        if not notify(
            NOTIFICATION_TITLE, notification_message(findings), NOTIFICATION_ID
        ):
            print(
                "Config path scan: the notification was not sent; the scan runs again at the next start.",
                flush=True,
            )
            return findings
    else:
        print(
            f"Config path scan: no {OLD_CONFIG_ROOT}/ path in the workspace instructions or schedules.",
            flush=True,
        )
    marker.write_text(
        json.dumps({"version": SCAN_VERSION, "found": len(findings)}) + "\n",
        encoding="utf-8",
    )
    return findings
