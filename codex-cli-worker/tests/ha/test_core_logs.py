"""The Core log view needs the paired worker token, and setup removes the browser user of versions before 0.5."""

import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "core_logs_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)
core_logs = sys.modules["core_logs_ha_test.core_logs"]

STORE_KEY = "codex_cli.browser_identity"


def loaded_entry(hass, worker_token="paired-worker"):
    """A loaded Codex entry whose runtime data holds only the worker token."""
    entry = MockConfigEntry(domain="codex_cli", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(worker_token=worker_token)
    return entry


async def test_caller_needs_an_administrator_and_the_paired_worker_token(hass):
    """A wrong or non-ASCII token, or a user who is not an administrator, is refused."""
    loaded_entry(hass)
    view = core_logs.DiagnosticView(hass)
    request = {KEY_HASS_USER: SimpleNamespace(is_admin=True)}
    for token in ("wrong", "שלום", None, 7):
        with pytest.raises(web.HTTPForbidden):
            view.check_caller(request, {"worker_token": token})
    view.check_caller(request, {"worker_token": "paired-worker"})
    request[KEY_HASS_USER] = SimpleNamespace(is_admin=False)
    with pytest.raises(web.HTTPForbidden):
        view.check_caller(request, {"worker_token": "paired-worker"})


async def test_no_loaded_entry_means_service_unavailable(hass):
    """Without a loaded entry there is no token to compare with."""
    view = core_logs.DiagnosticView(hass)
    with pytest.raises(web.HTTPServiceUnavailable):
        view.check_caller(
            {KEY_HASS_USER: SimpleNamespace(is_admin=True)},
            {"worker_token": "paired-worker"},
        )


async def test_http_reads_a_chunked_body_and_refuses_other_targets_and_large_bodies(
    hass, hass_client
):
    """Only the core target is read; the token can come in a chunked body; a body over 4096 bytes is refused."""
    assert await async_setup_component(hass, "http", {})
    loaded_entry(hass)
    hass.http.register_view(core_logs.DiagnosticView(hass))
    client = await hass_client()
    url = core_logs.DiagnosticView.url
    assert (await client.post(url, json={"target": "core"})).status == 403
    with patch.object(core_logs, "async_get_clientsession") as session:
        for target in (
            "core_mosquitto",
            "local_codex_cli_worker",
            "../core/logs",
            None,
        ):
            response = await client.post(
                url, json={"worker_token": "paired-worker", "target": target}
            )
            assert response.status == 400

        async def chunks():
            """Yield the request JSON in two pieces, as a chunked body."""
            yield b'{"worker_token":"paired-worker",'
            yield b'"target":"other"}'

        response = await client.post(
            url, data=chunks(), headers={"Content-Type": "application/json"}
        )
        assert response.status == 400  # the token was read: a bad token would be 403
        session.assert_not_called()
    assert (await client.post(url, data=b"x" * 5000)).status == 400


async def test_http_refuses_a_caller_without_a_session(hass, hass_client_no_auth):
    """Without a Home Assistant session the view answers 401 and calls nothing, even with the paired token."""
    assert await async_setup_component(hass, "http", {})
    loaded_entry(hass)
    hass.http.register_view(core_logs.DiagnosticView(hass))
    client = await hass_client_no_auth()
    with patch.object(core_logs, "async_get_clientsession") as session:
        response = await client.post(
            core_logs.DiagnosticView.url,
            json={"worker_token": "paired-worker", "target": "core"},
        )
    assert response.status == 401
    session.assert_not_called()


async def test_http_refuses_a_chunked_body_over_the_limit(hass, hass_client):
    """A chunked body has no length to check first, so the read stops once it passes 4096 bytes."""
    assert await async_setup_component(hass, "http", {})
    loaded_entry(hass)
    hass.http.register_view(core_logs.DiagnosticView(hass))
    client = await hass_client()

    async def chunks():
        """Yield a valid request whose padding takes the body past 4096 bytes, in pieces."""
        yield b'{"worker_token":"paired-worker","target":"core","pad":"'
        for _ in range(5):
            yield b"x" * 1000
        yield b'"}'

    with patch.object(core_logs, "async_get_clientsession") as session:
        response = await client.post(
            core_logs.DiagnosticView.url,
            data=chunks(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status == 400
    session.assert_not_called()


async def test_setup_registers_no_browser_session_or_screenshot_view(hass):
    """The browser session and kiosk screenshot endpoints are gone; the Core log view stays."""
    assert await async_setup_component(hass, "http", {})
    with patch.object(integration, "_async_register_services"):
        assert await integration.async_setup(hass, {})
    paths = {resource.canonical for resource in hass.http.app.router.resources()}
    assert "/api/codex_cli/diagnostic_logs" in paths
    assert "/api/codex_cli/browser_session" not in paths
    assert "/api/codex_cli/kiosk_screenshots" not in paths


async def test_old_browser_user_and_its_file_are_removed(hass, hass_storage):
    """The system user that the stored ID names, with the old name, is removed, and so is the file."""
    user = await hass.auth.async_create_system_user(
        "Dashboard verification", group_ids=[GROUP_ID_READ_ONLY], local_only=True
    )
    other = await hass.auth.async_create_system_user(
        "Another app", group_ids=[GROUP_ID_READ_ONLY]
    )
    hass_storage[STORE_KEY] = {"version": 1, "data": {"user_id": user.id}}
    await integration._async_remove_browser_user(hass)
    assert await hass.auth.async_get_user(user.id) is None
    assert await hass.auth.async_get_user(other.id) is not None
    assert STORE_KEY not in hass_storage


@pytest.mark.parametrize("kind", ["person", "renamed system user"])
async def test_a_person_or_renamed_user_is_never_removed(
    hass, hass_storage, caplog, kind
):
    """A stored ID that names a person's account, or a system user with another name, keeps its user."""
    if kind == "person":
        user = await hass.auth.async_create_user("Dashboard verification")
    else:
        user = await hass.auth.async_create_system_user(
            "Renamed", group_ids=[GROUP_ID_READ_ONLY]
        )
    hass_storage[STORE_KEY] = {"version": 1, "data": {"user_id": user.id}}
    caplog.set_level(logging.INFO)
    await integration._async_remove_browser_user(hass)
    assert await hass.auth.async_get_user(user.id) is not None
    assert STORE_KEY not in hass_storage
    kept = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Kept user")
    ]
    assert kept == [
        f"Kept user {user.id}, which the dashboard screenshot file of an earlier version names: it is "
        "not a system user named Dashboard verification"
    ]


async def test_stored_id_naming_no_user_removes_nobody_and_deletes_the_file(
    hass, hass_storage, caplog
):
    """A stored ID that names no user removes nobody, logs no "Kept user" line and still deletes the file."""
    other = await hass.auth.async_create_system_user(
        "Another app", group_ids=[GROUP_ID_READ_ONLY]
    )
    hass_storage[STORE_KEY] = {"version": 1, "data": {"user_id": "no-such-user-id"}}
    caplog.set_level(logging.INFO)
    with patch.object(hass.auth, "async_remove_user") as remove:
        await integration._async_remove_browser_user(hass)
    remove.assert_not_called()
    assert await hass.auth.async_get_user(other.id) is not None
    assert not [r for r in caplog.records if r.getMessage().startswith("Kept user")]
    assert STORE_KEY not in hass_storage


async def test_nothing_to_remove_on_a_new_install(hass, hass_storage):
    """Without the file, no user is looked up or removed."""
    with patch.object(hass.auth, "async_remove_user") as remove:
        await integration._async_remove_browser_user(hass)
    remove.assert_not_called()


async def test_uninstall_removes_the_old_browser_user(hass, hass_storage):
    """Removing the entry also removes a browser user that an earlier version left."""
    user = await hass.auth.async_create_system_user(
        "Dashboard verification", group_ids=[GROUP_ID_READ_ONLY]
    )
    hass_storage[STORE_KEY] = {"version": 1, "data": {"user_id": user.id}}
    entry = MockConfigEntry(domain="codex_cli")
    await integration.async_remove_entry(hass, entry)
    assert await hass.auth.async_get_user(user.id) is None
    assert STORE_KEY not in hass_storage


async def test_setup_removes_the_old_browser_user(hass, hass_storage):
    """Setting up the entry, as after an update, removes the browser user that an earlier version left."""
    user = await hass.auth.async_create_system_user(
        "Dashboard verification", group_ids=[GROUP_ID_READ_ONLY]
    )
    hass_storage[STORE_KEY] = {"version": 1, "data": {"user_id": user.id}}
    entry = MockConfigEntry(
        domain="codex_cli",
        data={"base_url": "http://worker"},
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.status.return_value = {"codex_login": {"status_ok": True}}
    worker = SimpleNamespace(base_url="http://worker", api_token="paired-worker")
    with (
        patch.object(integration, "async_discover_worker", return_value=worker),
        patch.object(integration, "CodexCliApiClient", return_value=client),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", return_value=None
        ),
    ):
        context = current_entry.set(entry)
        try:
            assert await integration.async_setup_entry(hass, entry)
        finally:
            current_entry.reset(context)
    assert await hass.auth.async_get_user(user.id) is None
    assert STORE_KEY not in hass_storage
    await entry.runtime_data.coordinator.async_shutdown()
