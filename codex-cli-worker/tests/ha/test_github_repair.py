"""The GitHub sign-in Repairs issue and event, against a mocked worker /status. All values are synthetic."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events
from test_integration_polling import integration

ISSUE = "github_access_lost"
EVENT = "codex_cli_github_access"
SYNTHETIC_TOKEN = "ghu_" + "S" * 36
SYNTHETIC_CODE = "TEST-0000"


def github_status(state, **extra):
    """The worker's section 2.5 status object, as the b1 worker reports it under "github" in /status."""
    connected = state not in ("disconnected", "connecting")
    status = {
        "state": state,
        "kind": "app" if connected else None,
        "account": {"login": "synthetic-owner"} if connected else None,
        "client_id_source": "built_in",
        "repositories": [
            {"full_name": "synthetic-owner/house", "private": True, "push": True}
        ]
        if connected
        else [],
        "expected_repositories": ["synthetic-owner/house"] if connected else [],
        "missing": [],
        "extra": [],
        "permissions": {"contents": "write", "metadata": "read"} if connected else None,
        "permission_problem": None,
        "expires_at": None,
        "connected_at": "2026-11-16T12:00:00+00:00" if connected else None,
        "last_check": {
            "at": "2026-11-16T13:00:00+00:00",
            "state": state,
            "reason": "synthetic reason",
        }
        if connected
        else None,
        "last_test": None,
        "revoke_url": "https://github.com/settings/apps/authorizations",
        "flow": {
            "status": "idle",
            "user_code": "",
            "verification_uri": "",
            "expires_at": None,
            "error": "",
        },
    }
    status.update(extra)
    return status


def worker_status(github):
    data = {
        "codex_login": {"status_ok": True},
        "account_access": {"needs_sign_in": False},
    }
    if github is not None:
        data["github"] = github
    return data


async def poll(coordinator, client, github):
    client.status.return_value = worker_status(github)
    await coordinator._async_update_data()


@pytest.mark.parametrize(
    ("state", "severity", "key"),
    [
        ("expired", ir.IssueSeverity.ERROR, "github_access_lost"),
        ("revoked", ir.IssueSeverity.ERROR, "github_access_lost"),
        ("failed", ir.IssueSeverity.ERROR, "github_access_check_failed"),
        ("near_expiry", ir.IssueSeverity.WARNING, "github_access_near_expiry"),
        ("access_changed", ir.IssueSeverity.WARNING, "github_access_changed"),
    ],
)
async def test_each_bad_state_raises_the_repair_and_ok_clears_it(
    hass, state, severity, key
):
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    registry = ir.async_get(hass)
    await poll(coordinator, client, github_status("ok"))
    assert registry.async_get_issue("codex_cli", ISSUE) is None
    await poll(coordinator, client, github_status(state))
    issue = registry.async_get_issue("codex_cli", ISSUE)
    assert issue is not None
    assert issue.severity == severity
    assert issue.translation_key == key
    assert issue.is_fixable is False
    assert issue.translation_placeholders["account"] == "synthetic-owner"
    assert issue.translation_placeholders["reason"] == "synthetic reason"
    await poll(coordinator, client, github_status("ok"))
    assert registry.async_get_issue("codex_cli", ISSUE) is None


@pytest.mark.parametrize(
    ("state", "key"),
    [
        ("expired", "github_access_lost_token"),
        ("revoked", "github_access_lost_token"),
        ("near_expiry", "github_access_near_expiry_token"),
        ("access_changed", "github_access_changed"),
        ("failed", "github_access_check_failed"),
    ],
)
async def test_a_token_connection_is_sent_to_the_token_form(hass, state, key):
    """Astro F4: Connect again starts an App sign-in, so a token's Repair names Use a token instead."""
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    await poll(coordinator, client, github_status(state, kind="token"))
    issue = ir.async_get(hass).async_get_issue("codex_cli", ISSUE)
    assert issue.translation_key == key


async def test_the_repair_text_fits_the_state(hass):
    """Astro F1: near expiry says GitHub still works and gives the date; a blocked state says work stops."""
    strings = json.loads(
        (Path(integration.__file__).parent / "translations" / "en.json").read_text()
    )["issues"]
    for key in ("github_access_near_expiry", "github_access_near_expiry_token"):
        text = strings[key]["description"]
        assert text.startswith("GitHub still works.")
        assert "cannot fetch" not in text
        assert "{expires}" in text
    for key in (
        "github_access_lost",
        "github_access_lost_token",
        "github_access_changed",
        "github_access_check_failed",
    ):
        assert "cannot fetch, pull or push" in strings[key]["description"]
    assert "Use a token instead" in strings["github_access_lost_token"]["description"]
    assert (
        "Use a token instead"
        in strings["github_access_near_expiry_token"]["description"]
    )
    assert "Connect again" not in strings["github_access_lost_token"]["description"]
    assert "Connect again" in strings["github_access_lost"]["description"]
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    await poll(
        coordinator,
        client,
        github_status("near_expiry", expires_at="2026-11-20T12:00:00+00:00"),
    )
    issue = ir.async_get(hass).async_get_issue("codex_cli", ISSUE)
    assert issue.translation_placeholders["expires"] == "on 2026-11-20"
    await poll(coordinator, client, github_status("near_expiry", expires_at=None))
    issue = ir.async_get(hass).async_get_issue("codex_cli", ISSUE)
    assert issue.translation_placeholders["expires"] == "within 7 days"


async def test_disconnect_clears_the_repair(hass):
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    registry = ir.async_get(hass)
    await poll(coordinator, client, github_status("revoked"))
    assert registry.async_get_issue("codex_cli", ISSUE) is not None
    await poll(coordinator, client, github_status("disconnected"))
    assert registry.async_get_issue("codex_cli", ISSUE) is None


async def test_owner_who_never_connected_gets_no_repair_and_no_event(hass):
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    events = async_capture_events(hass, EVENT)
    flow = {
        "status": "waiting_for_user",
        "user_code": SYNTHETIC_CODE,
        "verification_uri": "https://github.com/login/device",
        "expires_at": "2026-11-16T13:15:00+00:00",
        "error": "",
    }
    for status in (
        github_status("disconnected"),
        github_status("disconnected"),
        github_status("connecting", flow=flow),
        github_status("disconnected"),
    ):
        await poll(coordinator, client, status)
        assert ir.async_get(hass).async_get_issue("codex_cli", ISSUE) is None
    await hass.async_block_till_done()
    # connecting is a state change too, but it never carries the code into the event.
    assert all(SYNTHETIC_CODE not in str(event.data) for event in events)


async def test_a_worker_without_github_changes_nothing(hass):
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    events = async_capture_events(hass, EVENT)
    for _ in range(2):
        await poll(coordinator, client, None)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue("codex_cli", ISSUE) is None
    assert events == []


async def test_the_event_fires_once_per_state_change(hass):
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    events = async_capture_events(hass, EVENT)
    # The first poll after a start only learns the state: no event for a state the worker already announced.
    await poll(coordinator, client, github_status("ok"))
    await poll(coordinator, client, github_status("ok"))
    changed = github_status(
        "access_changed", extra=["synthetic-owner/new"], missing=["synthetic-owner/old"]
    )
    await poll(coordinator, client, changed)
    await poll(coordinator, client, changed)
    await poll(coordinator, client, github_status("ok"))
    await hass.async_block_till_done()
    assert [(e.data["previous_state"], e.data["state"]) for e in events] == [
        ("ok", "access_changed"),
        ("access_changed", "ok"),
    ]
    first = events[0].data
    assert first["account"] == "synthetic-owner"
    assert first["extra"] == ["synthetic-owner/new"]
    assert first["missing"] == ["synthetic-owner/old"]
    assert first["addon"] == "codex_cli_worker"
    assert first["source"] == "integration"
    assert set(first) == {
        "state",
        "previous_state",
        "reason",
        "account",
        "missing",
        "extra",
        "permission_problem",
        "expires_at",
        "addon",
        "source",
    }


async def test_no_token_or_code_reaches_the_repair_or_the_event(hass):
    """Even a worker that wrongly sent a secret-looking field gets only the allowlisted names copied."""
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    events = async_capture_events(hass, EVENT)
    await poll(coordinator, client, github_status("ok"))
    leaky = github_status(
        "revoked",
        token=SYNTHETIC_TOKEN,
        device_code=SYNTHETIC_CODE,
        flow={
            "status": "failed",
            "user_code": SYNTHETIC_CODE,
            "verification_uri": "",
            "expires_at": None,
            "error": "",
        },
    )
    await poll(coordinator, client, leaky)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue("codex_cli", ISSUE)
    assert issue is not None
    for text in (
        str(issue.translation_placeholders),
        str([event.data for event in events]),
    ):
        assert SYNTHETIC_TOKEN not in text
        assert SYNTHETIC_CODE not in text


WAIT_FOR_FIXTURE = """
import sys, time, urllib.request
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:9137/preview/", timeout=2).read(1)
        sys.exit(0)
    except Exception:
        time.sleep(0.5)
sys.exit(1)
"""


@pytest.mark.skipif(
    not os.environ.get("HA_BROWSER_NODE"),
    reason="The page check needs Node, Playwright and Chromium",
)
def test_the_github_card_in_a_real_browser():
    """Runs tests/browser_github.cjs: the page against mocked b1 routes, served by tests/web_fixture.py."""
    tests = Path(__file__).resolve().parents[1]
    fixture = subprocess.Popen(
        [sys.executable, str(tests / "web_fixture.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if (
            subprocess.run(
                [sys.executable, "-c", WAIT_FOR_FIXTURE], timeout=60
            ).returncode
            != 0
        ):
            pytest.fail("The web fixture did not start on 127.0.0.1:9137.")
        proc = subprocess.run(
            [os.environ["HA_BROWSER_NODE"], str(tests / "browser_github.cjs")],
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
        assert "PASS" in proc.stdout
    finally:
        fixture.terminate()
        try:
            fixture.wait(timeout=10)
        except subprocess.TimeoutExpired:
            fixture.kill()
