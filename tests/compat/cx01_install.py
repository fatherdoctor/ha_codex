#!/usr/bin/env python3
"""cx-01 install: the app installs and starts on the candidate Core and OS, and the integration is set up.

Passes when the app is started, the integration's entry is loaded and neither logged an ERROR line.

What it does, as a person would on a new install (DOCS.md, "The Codex integration"):
1. Add the app repository to the store, install the app with its default options and start it.
2. Wait until the app has written the integration under /config/custom_components.
3. Restart Home Assistant, which then discovers the app.
4. Confirm the discovered flow (Add, then Submit) and wait for the entry to load.
5. Read the app's log and Home Assistant's error log for new ERROR lines from either.

Which ERROR lines count (the design's rule is "no new ERROR line from either"):
- Home Assistant keeps its error log in memory, so the restart in step 3 clears it: every entry the step reads is
  from this run.
- On a new lab the app's log starts with this run. On a lab where the app is already installed, the step keeps the
  log from before the install and counts only the ERROR lines it did not have. If the Supervisor has dropped old
  lines from the log in the meantime, a repeat of an old error is not counted.

What the app-log check can see: only lines that carry the level word ERROR (`lab.error_lines`), such as the lines of
the Codex CLI, Flask and the Python logging module. The app prints many of its own problems with no level word, and
this check does not see those. The later steps catch them as failed chats, edits or checks.
"""

from __future__ import annotations

import argparse
from collections import Counter
import time

import lab as compat

INSTALL_TIMEOUT = 600
START_TIMEOUT = 120
RESTART_TIMEOUT = 300
DISCOVERY_TIMEOUT = 120
LOAD_TIMEOUT = 60


def installed_slug(lab: compat.Lab) -> str | None:
    """The app's slug when the app is already installed, else None."""
    slug = lab.addon_slug()
    if slug is None or not lab.supervisor("get", f"/store/addons/{slug}").get(
        "installed"
    ):
        return None
    return slug


def new_error_lines(before: str, after: str) -> list[str]:
    """The ERROR lines of `after` that `before` did not have, counted line by line, in their order."""
    earlier = Counter(compat.error_lines(before))
    new = []
    for line in compat.error_lines(after):
        if earlier[line]:
            earlier[line] -= 1
        else:
            new.append(line)
    return new


def install_app(lab: compat.Lab) -> str:
    """Add the repository if the store does not have it, install and start the app; return its slug."""
    if lab.addon_slug() is None:
        lab.supervisor("post", "/store/repositories", {"repository": lab.addon_repo})
    slug = lab.addon_slug()
    if slug is None:
        raise compat.failed(f"the store has no app from {lab.addon_repo}")
    if not lab.supervisor("get", f"/store/addons/{slug}").get("installed"):
        lab.supervisor("post", f"/store/addons/{slug}/install", timeout=INSTALL_TIMEOUT)
    lab.supervisor("post", f"/addons/{slug}/start", timeout=START_TIMEOUT)
    state = compat.wait_for(
        lambda: lab.addon_info(slug).get("state") == "started", START_TIMEOUT
    )
    if not state:
        raise compat.failed(f"the app did not start within {START_TIMEOUT} seconds")
    return slug


def wait_for_integration_files(worker: compat.Worker) -> None:
    """Wait until the app reports the integration's files under /config."""

    def written() -> bool:
        return worker.call("GET", "/status")["integration"]["state"] != "not_installed"

    if not compat.wait_for(written, START_TIMEOUT):
        raise compat.failed(
            "the app did not write the integration under /config/custom_components"
        )


def restart_core(lab: compat.Lab) -> None:
    """Restart Home Assistant and wait until its API answers again."""
    try:
        lab.supervisor("post", "/core/restart", timeout=RESTART_TIMEOUT)
    except compat.CONNECTION_ERRORS as exc:
        # The connection that asked for the restart usually closes with it; the wait below is the check.
        reason = f"the restart call ended with {type(exc).__name__}"
        print(f"{reason}; waiting for Home Assistant")  # noqa: T201
    time.sleep(10)
    if not compat.wait_for(lab.is_up, RESTART_TIMEOUT):
        raise compat.failed(
            f"Home Assistant did not come back within {RESTART_TIMEOUT} seconds of the restart"
        )


def confirm_discovery(lab: compat.Lab) -> None:
    """Confirm the flow that the app's discovery started, as the Add and Submit buttons do."""

    def discovered() -> list[dict]:
        flows = lab.ws("config_entries/flow/progress")
        return [
            flow for flow in flows if flow.get("handler") == compat.INTEGRATION_DOMAIN
        ]

    flows = compat.wait_for(discovered, DISCOVERY_TIMEOUT)
    if not flows:
        raise compat.failed(
            f"Home Assistant did not discover the app within {DISCOVERY_TIMEOUT} seconds"
        )
    answer = lab.rest(
        "POST", f"/api/config/config_entries/flow/{flows[0]['flow_id']}", {}
    )
    if answer.get("type") != "create_entry":
        raise compat.failed(
            f"the discovered flow did not create an entry: {answer.get('type')} "
            f"{answer.get('step_id') or answer.get('reason') or ''}".strip()
        )


def wait_for_entry(lab: compat.Lab) -> None:
    """Wait until the integration's entry is loaded."""

    def loaded() -> bool:
        return any(
            entry.get("state") == "loaded"
            for entry in lab.config_entries(compat.INTEGRATION_DOMAIN)
        )

    if not compat.wait_for(loaded, LOAD_TIMEOUT):
        states = [
            entry.get("state")
            for entry in lab.config_entries(compat.INTEGRATION_DOMAIN)
        ]
        raise compat.failed(
            f"the integration's entry is not loaded: {states or 'no entry'}"
        )


def integration_errors(lab: compat.Lab) -> list[str]:
    """Home Assistant's logged errors that come from the integration."""
    entries = lab.ws("system_log/list")
    return [
        f"{entry.get('name')}: {(entry.get('message') or [''])[0]}"
        for entry in entries
        if entry.get("level") == "ERROR"
        and compat.INTEGRATION_DOMAIN in str(entry.get("name", ""))
    ]


def step(lab: compat.Lab, args: argparse.Namespace) -> str:
    """Install, start, restart, confirm and check the logs; return the PASS line."""
    earlier_slug = installed_slug(lab)
    log_before = lab.addon_log(earlier_slug) if earlier_slug else ""
    slug = install_app(lab)
    wait_for_integration_files(lab.worker(slug))
    restart_core(lab)
    confirm_discovery(lab)
    wait_for_entry(lab)
    app_errors = new_error_lines(log_before, lab.addon_log(slug))
    core_errors = integration_errors(lab)
    if app_errors or core_errors:
        for line in (app_errors + core_errors)[:20]:
            print(line)  # noqa: T201 - the runner keeps stdout as the step's log
        raise compat.failed(
            f"{len(app_errors)} ERROR line(s) from the app, {len(core_errors)} from the integration"
        )
    version = lab.addon_info(slug).get("version")
    return f"app {version} started on Core {args.core}; the integration's entry is loaded; no ERROR lines"


if __name__ == "__main__":
    compat.run_step("cx-01 install", step)
