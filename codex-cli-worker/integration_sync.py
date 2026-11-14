"""Install or update the Codex integration in /config from the copy bundled in this app's image.

The app image carries the integration of the same release under BUNDLED_DIR. At each start, before the
worker's server starts, this script:

- writes that copy to /config/custom_components/codex_cli when the folder is missing, or when the folder
  carries this app's marker file and the marker names a different bundle;
- leaves the folder untouched when it was installed another way (no marker file, or a symbolic link),
  and tells the owner once per app version that it did so;
- backs up the old folder to the app's private storage, writes the new copy to a temporary folder next
  to it, and swaps the two by renaming, so a failed copy leaves the old folder in place;
- asks the owner to restart Home Assistant after it writes the folder, because Home Assistant loads an
  integration's files only when it starts;
- announces the app to Home Assistant through Supervisor discovery, so the integration offers setup
  under Settings > Devices & services once it is loaded.

It writes nothing outside the integration's folder in /config, apart from the temporary folder and the
old folder during the swap, which it removes. The app option install_integration (default on) turns the
copy off; discovery is announced either way. The script never stops the app from starting: every
failure is logged and reported, and the worker starts as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

BUNDLED_DIR = Path("/usr/share/ha_codex/integration/codex_cli")
TARGET_DIR = Path("/config/custom_components/codex_cli")
BACKUP_DIR = Path("/data/integration_backup/codex_cli")
OPTIONS_PATH = Path("/data/options.json")
NOTICE_STATE_PATH = Path("/data/integration_sync.json")

# The marker file names the bundle that wrote the folder. Home Assistant loads only the integration's
# Python files and manifest, so the extra file does not change how the integration runs.
MARKER_NAME = ".installed_by_app.json"
IGNORED_NAMES = frozenset({MARKER_NAME, "__pycache__"})

SUPERVISOR_URL = "http://supervisor"
DISCOVERY_SERVICE = "codex_cli"
NOTICE_RESTART = "codex_cli_restart"
NOTICE_KEPT = "codex_cli_integration_kept"
NOTICE_FAILED = "codex_cli_install_failed"
REQUEST_TIMEOUT = (3, 10)
NOTICE_ATTEMPTS = 3
NOTICE_RETRY_SECONDS = 5


@dataclass(frozen=True)
class SyncResult:
    """What one sync did to the integration's folder.

    action is one of:
      "disabled"   the install_integration option is off; nothing was read or written;
      "no_bundle"  the image carries no integration (an image built without it); nothing was written;
      "current"    the folder already holds this bundle;
      "foreign"    the folder was installed another way and was left as it is;
      "installed"  there was no folder and the bundle was written;
      "updated"    the folder held an older bundle of this app and was replaced;
      "failed"     the copy failed; the old folder, if there was one, is still in place.
    """

    action: str
    version: str = ""
    installed_version: str = ""
    detail: str = ""


def manifest_version(folder: Path) -> str:
    """Return the version in the folder's manifest.json, or an empty string when it cannot be read."""
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) else ""


def tree_digest(folder: Path) -> str:
    """Return a SHA-256 digest of the folder's file names and contents, ignoring the marker and caches."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if IGNORED_NAMES.intersection(relative.parts) or not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_marker(folder: Path) -> dict | None:
    """Return the marker this app wrote in the folder, or None when there is no readable marker."""
    try:
        marker = json.loads((folder / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def write_bundle(bundled: Path, staging: Path, marker: dict) -> None:
    """Copy the bundle to the staging folder and add the marker file."""
    shutil.copytree(bundled, staging, ignore=shutil.ignore_patterns("__pycache__"))
    (staging / MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def back_up(target: Path, backup: Path) -> None:
    """Replace the previous backup with a copy of the folder as it is now."""
    if backup.exists():
        shutil.rmtree(backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target, backup, symlinks=True)


def swap_in(staging: Path, target: Path, old: Path) -> None:
    """Put the staging folder in the target's place by renaming, and restore the old folder on failure.

    All three folders are in the same parent, so each rename is atomic. Between the two renames the
    target is briefly missing; Home Assistant reads it only when it starts, after the owner restarts it.
    """
    if not target.exists():
        os.rename(staging, target)
        return
    os.rename(target, old)
    try:
        os.rename(staging, target)
    except OSError:
        os.rename(old, target)
        raise
    shutil.rmtree(old, ignore_errors=True)


def remove_leftover(folder: Path) -> None:
    """Remove a temporary folder left by an earlier start that stopped part way.

    shutil.rmtree refuses a symbolic link, so a link planted under that name fails the sync instead of
    deleting what it points to.
    """
    if folder.exists() or folder.is_symlink():
        shutil.rmtree(folder)


def sync(bundled: Path, target: Path, backup: Path, app_version: str) -> SyncResult:
    """Bring the integration's folder in line with the bundle, unless it was installed another way."""
    version = manifest_version(bundled)
    if not version:
        return SyncResult("no_bundle", detail=f"no integration manifest under {bundled}")

    installed_version = manifest_version(target)
    if target.is_symlink():
        return SyncResult("foreign", version, installed_version)
    marker = read_marker(target) if target.exists() else None
    if target.exists() and marker is None:
        return SyncResult("foreign", version, installed_version)

    digest = tree_digest(bundled)
    if marker is not None and marker.get("digest") == digest:
        return SyncResult("current", version, installed_version)

    action = "updated" if target.exists() else "installed"
    staging = target.with_name(f".{target.name}.new")
    old = target.with_name(f".{target.name}.old")
    try:
        remove_leftover(staging)
        remove_leftover(old)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bundle(bundled, staging, {"app_version": app_version, "version": version, "digest": digest})
        if action == "updated":
            back_up(target, backup)
        swap_in(staging, target, old)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return SyncResult("failed", version, installed_version, detail=str(exc))
    return SyncResult(action, version, installed_version)


def notice_for(result: SyncResult, app_version: str) -> tuple[str, str, str] | None:
    """Return the notification to show for a sync result as (id, title, message), or None for none."""
    folder = "/config/custom_components/codex_cli"
    if result.action == "installed":
        return (
            NOTICE_RESTART,
            "Codex: restart Home Assistant to finish setup",
            (
                f"The Codex app installed its integration (version {result.version}) in `{folder}`. "
                "Restart Home Assistant once to load it: **Settings** > **System** > restart. "
                "After the restart, **Settings** > **Devices & services** shows **Discovered: Codex**; "
                "choose **Add** to finish setup."
            ),
        )
    if result.action == "updated":
        return (
            NOTICE_RESTART,
            "Codex: restart Home Assistant to finish the update",
            (
                f"The Codex app updated its integration from version {result.installed_version or 'unknown'} "
                f"to {result.version}. Home Assistant keeps running the old version until it restarts: "
                "**Settings** > **System** > restart. A copy of the old version is kept in the app's own storage."
            ),
        )
    if result.action == "foreign":
        return (
            NOTICE_KEPT,
            "Codex: integration left as installed",
            (
                f"The Codex integration in `{folder}` (version {result.installed_version or 'unknown'}) was not "
                "installed by the Codex app, for example it came from the Marketplace, HACS or a manual copy. "
                "The app left it as it is. Keep updating it the way you installed it, to the same version as "
                f"the app ({app_version}). To let the app install and update it instead, remove it the way you "
                "installed it, then restart the Codex app and Home Assistant."
            ),
        )
    if result.action == "failed":
        return (
            NOTICE_FAILED,
            "Codex: the integration could not be installed",
            (
                f"The Codex app could not write its integration (version {result.version}) to `{folder}`: "
                f"{result.detail}. Any earlier version was left in place. Check the free space and the "
                "permissions of the configuration folder, then restart the Codex app."
            ),
        )
    return None


def supervisor_post(path: str, payload: dict, token: str) -> bool:
    """POST JSON to a fixed Supervisor path with the app's own token; return whether it was accepted."""
    with requests.Session() as client:
        client.trust_env = False
        try:
            response = client.post(
                SUPERVISOR_URL + path,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            log(f"Supervisor request to {path} failed: {exc}")
            return False
    if response.status_code >= 400:
        log(f"Supervisor request to {path} failed: HTTP {response.status_code}")
        return False
    return True


def show_notice(notice: tuple[str, str, str], token: str) -> bool:
    """Show a persistent notification in Home Assistant, retrying while Core is still starting."""
    notification_id, title, message = notice
    payload = {"notification_id": notification_id, "title": title, "message": message}
    for attempt in range(NOTICE_ATTEMPTS):
        if supervisor_post("/core/api/services/persistent_notification/create", payload, token):
            return True
        if attempt + 1 < NOTICE_ATTEMPTS:
            time.sleep(NOTICE_RETRY_SECONDS)
    return False


def kept_notice_due(state_path: Path, app_version: str) -> bool:
    """Return whether the 'left as installed' notice has not yet been shown for this app version."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(state, dict) or state.get("kept_notice_version") != app_version


def record_kept_notice(state_path: Path, app_version: str) -> None:
    """Remember that the 'left as installed' notice was shown for this app version."""
    state_path.write_text(json.dumps({"kept_notice_version": app_version}) + "\n", encoding="utf-8")


def install_option_on(options_path: Path) -> bool:
    """Return the install_integration option; it is on unless the options say false."""
    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(options, dict) and options.get("install_integration") is False)


def log(message: str) -> None:
    """Write one line to the app log."""
    print(f"Integration install: {message}", flush=True)


def main() -> int:
    """Sync the integration, report the result, and announce the app. Always returns 0."""
    app_version = os.environ.get("ADDON_VERSION", "unknown")
    token = os.environ.get("SUPERVISOR_TOKEN", "")

    if install_option_on(OPTIONS_PATH):
        result = sync(BUNDLED_DIR, TARGET_DIR, BACKUP_DIR, app_version)
    else:
        result = SyncResult("disabled")
    log(f"{result.action} {result.version} {result.detail}".rstrip())

    notice = notice_for(result, app_version)
    if result.action == "foreign" and not kept_notice_due(NOTICE_STATE_PATH, app_version):
        notice = None
    if notice and token and show_notice(notice, token) and result.action == "foreign":
        record_kept_notice(NOTICE_STATE_PATH, app_version)

    if token:
        supervisor_post("/discovery", {"service": DISCOVERY_SERVICE, "config": {}}, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
