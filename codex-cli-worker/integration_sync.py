"""Install or update the Codex integration in /config from the copy bundled in this app's image.

The app image carries the integration of the same release under BUNDLED_DIR. At each start, before the
worker's server starts, this script:

- writes that copy to /config/custom_components/codex_cli when the folder is missing, or when the folder
  carries this app's marker file, still holds exactly the copy the marker names, and the marker names a
  different bundle;
- leaves the folder untouched when it was installed another way (no marker file, or a symbolic link), or
  when its files no longer match the marker (another installer or a hand edit changed them), and tells the
  owner once per app version that it did so;
- backs up the old folder to the app's private storage, writes the new copy to a work folder outside
  custom_components, and swaps the two by renaming, so a failed copy leaves the old folder in place;
- asks the owner to restart Home Assistant after it writes the folder, because Home Assistant loads an
  integration's files only when it starts, and asks again at later starts until the notice is shown;
- removes its 'left as installed' notice once the folder is the app's again.

Run with --announce (run.sh starts it in the background), it announces the app to Home Assistant through
Supervisor discovery, so the integration offers setup under Settings > Devices & services. It waits until
Home Assistant has loaded the integration's files: a discovery that arrives before then makes Home
Assistant log "Cannot find integration codex_cli" with a traceback.

It writes nothing in /config except the integration's folder and its own work folder
/config/.codex_cli_install, which Home Assistant never loads from and which is emptied at every start
while the copy is on. The app option install_integration (default on) turns the copy off; discovery is
announced either way, once Home Assistant has loaded the integration, however it was installed. The
script never stops the app from starting: every failure is logged and reported, and the worker starts as
before.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

import requests

BUNDLED_DIR = Path("/usr/share/ha_codex/integration/codex_cli")
TARGET_DIR = Path("/config/custom_components/codex_cli")
# The work folder is on the same file system as the target, so the renames in swap_in are atomic, but
# outside custom_components, so Home Assistant's loader never reads a half-written or old copy.
WORK_DIR = Path("/config/.codex_cli_install")
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
# Home Assistant lists the custom integrations it loaded at its last start under this path.
FLOW_HANDLERS_PATH = "/core/api/config/config_entries/flow_handlers"
ANNOUNCE_POLL_SECONDS = 60
# After this many reads in a row that fail (Core down for long, or an API that answers differently), the app
# announces itself anyway: at worst Home Assistant logs one error, and setup still works after a restart.
ANNOUNCE_UNREAD_LIMIT = 10


@dataclass(frozen=True)
class SyncResult:
    """What one sync did to the integration's folder.

    action is one of:
      "disabled"   the install_integration option is off; nothing was read or written;
      "no_bundle"  the image carries no integration (an image built without it); nothing was written;
      "current"    the folder already holds this bundle;
      "foreign"    the folder was installed another way, or changed after the app wrote it, and was
                   left as it is;
      "installed"  there was no folder and the bundle was written;
      "updated"    the folder held an older bundle of this app and was replaced;
      "failed"     the copy failed; the old folder, if there was one, is still in place.
    detail says why, for "foreign" and "failed", or notes a problem that did not stop the sync.
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
    (staging / MARKER_NAME).write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8"
    )


def back_up(target: Path, backup: Path) -> None:
    """Replace the previous backup with a copy of the folder as it is now."""
    if backup.exists():
        shutil.rmtree(backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target, backup, symlinks=True)


def swap_in(staging: Path, target: Path, old: Path) -> None:
    """Put the staging folder in the target's place by renaming, and restore the old folder on failure.

    The work folder and the target are on the same file system, so each rename is atomic. Between the two
    renames the target is briefly missing; Home Assistant reads it only when it starts, after the owner
    restarts it. The old folder is left in the work folder; the caller removes it.
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


def clear_work_dir(work: Path) -> None:
    """Empty the work folder, removing whatever an earlier start that stopped part way left in it.

    shutil.rmtree refuses a symbolic link, so a link planted in place of the work folder fails the sync
    instead of deleting what it points to. An error is raised, not ignored, so the caller can report it.
    """
    if work.exists() or work.is_symlink():
        shutil.rmtree(work)


def is_outside_config(target: Path) -> bool:
    """Return whether the target's custom_components folder is a link, or resolves outside the config folder.

    The sync runs as root outside Codex's sandbox, so it writes only inside the real configuration folder.
    """
    components = target.parent
    config = components.parent
    # A link anywhere in the custom_components path resolves somewhere else, and is caught here.
    return (
        components.exists()
        and components.resolve() != config.resolve() / components.name
    )


def ownership(target: Path) -> str:
    """Return who the target folder belongs to: "none" (missing), "app", or why it is another install's.

    The folder is the app's only while it holds exactly the files the app's marker names. A folder with
    no marker, a linked folder, and a folder whose files were changed after the app wrote them (by
    another installer copying over it, or by hand) all belong to whoever changed them.
    """
    if is_outside_config(target):
        return "custom_components is a link or lies outside the configuration folder"
    if target.is_symlink():
        return "the folder is a symbolic link"
    if not target.exists():
        return "none"
    marker = read_marker(target)
    if marker is None:
        return "the folder has no marker from the app"
    if marker.get("digest") != tree_digest(target):
        return "the folder was changed after the app wrote it"
    return "app"


def sync(
    bundled: Path, target: Path, backup: Path, app_version: str, work: Path
) -> SyncResult:
    """Bring the integration's folder in line with the bundle, unless it is another install's."""
    version = manifest_version(bundled)
    if not version:
        return SyncResult(
            "no_bundle", detail=f"no integration manifest under {bundled}"
        )
    installed_version = manifest_version(target)
    try:
        # Every start empties the work folder first, so nothing an interrupted start left behind stays.
        clear_work_dir(work)
    except OSError as exc:
        return SyncResult(
            "failed",
            version,
            installed_version,
            detail=f"could not empty {work}: {exc}",
        )

    owner = ownership(target)
    if owner not in ("none", "app"):
        return SyncResult("foreign", version, installed_version, detail=owner)
    digest = tree_digest(bundled)
    if owner == "app" and read_marker(target).get("digest") == digest:
        return SyncResult("current", version, installed_version)

    action = "updated" if owner == "app" else "installed"
    staging = work / "new"
    old = work / "old"
    try:
        work.mkdir(parents=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bundle(
            bundled,
            staging,
            {"app_version": app_version, "version": version, "digest": digest},
        )
        if action == "updated":
            back_up(target, backup)
        swap_in(staging, target, old)
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        return SyncResult("failed", version, installed_version, detail=str(exc))
    try:
        clear_work_dir(work)
    except OSError as exc:
        # The new folder is in place; the old copy waits outside custom_components for the next start.
        return SyncResult(
            action,
            version,
            installed_version,
            detail=f"the old copy was not removed: {exc}",
        )
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
                "If Codex is not set up yet, **Settings** > **Devices & services** then shows "
                "**Discovered: Codex**; choose **Add** to finish setup."
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
                "installed by the Codex app, or was changed after the app installed it, for example by the "
                "Marketplace, HACS, a manual copy or an edit. The app left it as it is. Keep updating it the way you installed it, to the same version as "
                f"the app ({app_version}). To let the app install and update it instead, remove it the way you "
                "installed it, then restart the Codex app and Home Assistant."
            ),
        )
    if result.action == "failed":
        return (
            NOTICE_FAILED,
            "Codex: the integration could not be installed",
            (
                f"The Codex app could not write its integration (version {result.version or 'unknown'}) to `{folder}`: "
                f"{result.detail}. Any earlier version was left in place. Check the free space and the "
                "permissions of the configuration folder, then restart the Codex app."
            ),
        )
    return None


def supervisor_request(method: str, path: str, token: str, payload: dict | None = None):
    """Send one request to a fixed Supervisor path with the app's own token; return the response, or None.

    None means the request failed or was refused (HTTP 400 or above); the reason is logged.
    """
    with requests.Session() as client:
        client.trust_env = False
        try:
            response = client.request(
                method,
                SUPERVISOR_URL + path,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            log(f"Supervisor request to {path} failed: {exc}")
            return None
    if response.status_code >= 400:
        log(f"Supervisor request to {path} failed: HTTP {response.status_code}")
        return None
    return response


def supervisor_post(path: str, payload: dict, token: str) -> bool:
    """POST JSON to a fixed Supervisor path; return whether it was accepted."""
    return supervisor_request("POST", path, token, payload) is not None


def core_knows_integration(token: str) -> bool | None:
    """Return whether Home Assistant has loaded the integration's files, or None when it cannot tell.

    Home Assistant reads the custom integrations in /config only when it starts, and lists each one that
    has a config flow under flow_handlers. A folder the app wrote after that start is not listed until
    Home Assistant restarts.
    """
    response = supervisor_request("GET", FLOW_HANDLERS_PATH, token)
    if response is None:
        return None
    try:
        handlers = response.json()
    except ValueError:
        return None
    return DISCOVERY_SERVICE in handlers if isinstance(handlers, list) else None


def announce(token: str, sleep=time.sleep) -> None:
    """Post Supervisor discovery once Home Assistant has loaded the integration.

    Checks once a minute, so a restart of Home Assistant hours later is still followed by the announcement.
    When Home Assistant cannot be read ANNOUNCE_UNREAD_LIMIT times in a row, the app announces itself anyway.
    """
    failed_reads = 0
    waiting_logged = False
    while True:
        known = core_knows_integration(token)
        if known:
            break
        if known is None:
            failed_reads += 1
            if failed_reads >= ANNOUNCE_UNREAD_LIMIT:
                log(
                    "could not ask Home Assistant whether the integration is loaded; announcing the app anyway"
                )
                break
        else:
            failed_reads = 0
            if not waiting_logged:
                log(
                    "discovery waits until Home Assistant has loaded the integration (after its next restart)"
                )
                waiting_logged = True
        sleep(ANNOUNCE_POLL_SECONDS)
    supervisor_post("/discovery", {"service": DISCOVERY_SERVICE, "config": {}}, token)


def show_notice(notice: tuple[str, str, str], token: str) -> bool:
    """Show a persistent notification in Home Assistant, retrying while Core is still starting."""
    notification_id, title, message = notice
    payload = {"notification_id": notification_id, "title": title, "message": message}
    for attempt in range(NOTICE_ATTEMPTS):
        if supervisor_post(
            "/core/api/services/persistent_notification/create", payload, token
        ):
            return True
        if attempt + 1 < NOTICE_ATTEMPTS:
            time.sleep(NOTICE_RETRY_SECONDS)
    return False


def dismiss_notice(notification_id: str, token: str) -> bool:
    """Remove a persistent notification from Home Assistant; return whether the request was accepted."""
    payload = {"notification_id": notification_id}
    return supervisor_post(
        "/core/api/services/persistent_notification/dismiss", payload, token
    )


def read_state(state_path: Path) -> dict:
    """Return what earlier starts recorded about notices, or an empty record when there is none.

    Keys: "kept_notice_version", the app version the 'left as installed' notice was shown for; and
    "restart_notice", a restart notice [id, title, message] that has not been shown yet.
    """
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(state_path: Path, state: dict) -> None:
    """Save the notice record; a failure is logged, and at worst a notice is shown again."""
    try:
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"could not save {state_path}: {exc}")


def notice_due(
    result: SyncResult, app_version: str, state: dict
) -> tuple[str, str, str] | None:
    """Return the notice this start should show, given what earlier starts already showed.

    A restart notice stays due at later starts until Home Assistant accepts it once, so the owner always
    learns that Home Assistant runs an older integration than the app. The 'left as installed' notice is
    shown once for each app version.
    """
    notice = notice_for(result, app_version)
    if result.action == "foreign" and state.get("kept_notice_version") == app_version:
        return None
    if notice is None and result.action == "current":
        owed = state.get("restart_notice")
        # Only a restart notice is ever owed; any other record is a damaged file and is not shown.
        if (
            isinstance(owed, list)
            and len(owed) == 3
            and all(isinstance(part, str) for part in owed)
            and owed[0] == NOTICE_RESTART
        ):
            return (owed[0], owed[1], owed[2])
    return notice


def kept_notice_outdated(result: SyncResult, state: dict) -> bool:
    """Return whether the 'left as installed' notice was shown but the folder is the app's again."""
    return (
        result.action in ("current", "installed", "updated")
        and "kept_notice_version" in state
    )


def record_notice(
    result: SyncResult, app_version: str, state: dict, notice, shown: bool
) -> dict:
    """Return the notice record after this start: what was shown, and which restart notice is still owed."""
    state = dict(state)
    if result.action == "foreign" and shown:
        state["kept_notice_version"] = app_version
    if notice is not None and notice[0] == NOTICE_RESTART:
        if shown:
            state.pop("restart_notice", None)
        else:
            state["restart_notice"] = list(notice)
    return state


def install_option_on(options_path: Path) -> bool:
    """Return the install_integration option; it is on unless the options say false."""
    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (
        isinstance(options, dict) and options.get("install_integration") is False
    )


def log(message: str) -> None:
    """Write one line to the app log."""
    print(f"Integration install: {message}", flush=True)


def main(argv: list[str] | None = None) -> int:
    """Sync the integration and report the result, or with --announce announce the app. Always returns 0."""
    args = sys.argv[1:] if argv is None else argv
    app_version = os.environ.get("ADDON_VERSION", "unknown")
    token = os.environ.get("SUPERVISOR_TOKEN", "")

    if args == ["--announce"]:
        if token:
            announce(token)
        return 0

    if not install_option_on(OPTIONS_PATH):
        result = SyncResult("disabled")
    else:
        try:
            result = sync(BUNDLED_DIR, TARGET_DIR, BACKUP_DIR, app_version, WORK_DIR)
        except (OSError, ValueError) as exc:
            # A read that fails before the copy starts (the manifest, a digest), or a file name that cannot be
            # encoded (ValueError), is reported like a failed copy.
            result = SyncResult("failed", detail=str(exc))
    log(f"{result.action} {result.version} {result.detail}".rstrip())

    state = read_state(NOTICE_STATE_PATH)
    notice = notice_due(result, app_version, state)
    shown = bool(notice and token and show_notice(notice, token))
    new_state = record_notice(result, app_version, state, notice, shown)
    if (
        kept_notice_outdated(result, new_state)
        and token
        and dismiss_notice(NOTICE_KEPT, token)
    ):
        del new_state["kept_notice_version"]
    if new_state != state:
        write_state(NOTICE_STATE_PATH, new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
