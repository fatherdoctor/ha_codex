"""Action, browser-session and discovery boundaries during entry transitions."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "quality_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)
discovery = sys.modules["quality_ha_test.discovery"]
auth = sys.modules["quality_ha_test.browser_auth"]

CALLS = [
    ("start_task", {"prompt": "fixture"}),
    ("start_login", {}),
    ("get_login_status", {}),
    ("logout", {}),
    ("get_task", {"task_id": "fixture"}),
    ("list_tasks", {}),
    ("cancel_task", {"task_id": "fixture"}),
    ("reply_task", {"task_id": "fixture", "reply": "fixture"}),
    ("continue_task", {"task_id": "fixture", "message": "fixture"}),
]
NOT_LOADED = [
    state for state in ConfigEntryState if state is not ConfigEntryState.LOADED
]


def add_entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS):
    """Use a synthetic entry without starting real discovery or platforms."""
    entry = MockConfigEntry(
        domain="codex_cli", data={"base_url": "http://worker"}, state=state
    )
    entry.add_to_hass(hass)
    return entry


def runtime(client=None, broker=None):
    """Allow the same guard regression to run before the broker field is added."""
    kwargs = {
        "client": client if client is not None else AsyncMock(),
        "coordinator": AsyncMock(),
    }
    if "browser_sessions" in integration.CodexCliRuntimeData.__dataclass_fields__:
        kwargs["browser_sessions"] = broker
    return integration.CodexCliRuntimeData(**kwargs)


@pytest.mark.parametrize("state", NOT_LOADED)
async def test_every_action_refuses_nonloaded_entry(hass, state):
    """Published runtime data alone must never authorize an action."""
    entry = add_entry(hass, state)
    client = AsyncMock()
    entry.runtime_data = runtime(client)
    integration._async_register_services(hass)
    for service, data in CALLS:
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call("codex_cli", service, data, blocking=True)
    assert not client.mock_calls


@pytest.mark.parametrize("force", [None, False, True])
async def test_loaded_action_passes_default_force(hass, force):
    """A loaded entry still accepts actions and defaults force to false."""
    entry = add_entry(hass, ConfigEntryState.LOADED)
    client = AsyncMock()
    client.start_login.return_value = {"status": "already_logged_in"}
    entry.runtime_data = runtime(client)
    integration._async_register_services(hass)
    result = await hass.services.async_call(
        "codex_cli",
        "start_login",
        {} if force is None else {"force": force},
        blocking=True,
        return_response=True,
    )
    assert result == {"status": "already_logged_in"}
    client.start_login.assert_awaited_once_with(False if force is None else force)


@pytest.mark.parametrize("state", NOT_LOADED)
async def test_browser_view_refuses_nonloaded_entry_and_legacy_global(hass, state):
    """Old global sessions cannot bypass the entry's state."""
    entry = add_entry(hass, state)
    broker = auth.BrowserSessions(hass, "fixture-pairing")
    entry.runtime_data = runtime(broker=broker)
    hass.data.setdefault("codex_cli", {})["browser_sessions"] = broker
    request = {KEY_HASS_USER: SimpleNamespace(is_admin=True)}
    with pytest.raises(web.HTTPServiceUnavailable):
        auth.BrowserSessionView(hass).broker(
            request, {"worker_token": "fixture-pairing"}
        )


@pytest.mark.parametrize("failure", [None, "refresh", "broker", "platforms"])
async def test_setup_publishes_after_refresh_and_cleans_failed_setup(hass, failure):
    """Failures leave no runtime or owned listeners, including platform forwarding."""
    entry = add_entry(hass)
    integration._async_register_services(hass)
    broker = SimpleNamespace(setup=AsyncMock(), close=Mock())
    remove_poll = Mock()
    remove_choices = Mock()
    coordinator = SimpleNamespace(async_add_listener=Mock(return_value=remove_poll))

    async def refresh():
        assert getattr(entry, "runtime_data", None) is None
        with pytest.raises(ServiceValidationError):
            integration._first_runtime(hass)
        if failure == "refresh":
            raise ConfigEntryNotReady("fixture refresh")

    async def setup_broker():
        if failure == "broker":
            raise RuntimeError("fixture broker")

    async def forward(*args):
        assert entry.runtime_data.browser_sessions is broker
        with pytest.raises(ServiceValidationError):
            integration._first_runtime(hass)
        if failure == "platforms":
            raise RuntimeError("fixture platforms")

    coordinator.async_config_entry_first_refresh = refresh
    broker.setup.side_effect = setup_broker
    worker = discovery.WorkerConnection("http://worker", "fixture-pairing")
    with (
        patch.object(integration, "async_discover_worker", return_value=worker),
        patch.object(integration, "CodexCliApiClient", return_value=AsyncMock()),
        patch.object(integration, "CodexCliCoordinator", return_value=coordinator),
        patch.object(integration, "BrowserSessions", return_value=broker),
        patch.object(
            integration, "async_listen_for_choices", return_value=remove_choices
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=forward
        ),
    ):
        context = current_entry.set(entry)
        try:
            if failure:
                with pytest.raises((ConfigEntryNotReady, RuntimeError)):
                    await integration.async_setup_entry(hass, entry)
                assert getattr(entry, "runtime_data", None) is None
                assert not entry.update_listeners
                if failure != "refresh":
                    broker.close.assert_called_once()
                if failure == "platforms":
                    remove_poll.assert_called_once()
                    remove_choices.assert_called_once()
            else:
                assert await integration.async_setup_entry(hass, entry)
                assert "browser_sessions" not in hass.data.get("codex_cli", {})
                await entry._async_process_on_unload(hass)
                broker.close.assert_called_once()
                remove_poll.assert_called_once()
                remove_choices.assert_called_once()
                assert entry.runtime_data is None
        finally:
            current_entry.reset(context)


async def test_real_broker_unload_and_reload_revoke_old_session(hass):
    """The view follows the loaded entry and cannot reuse a retired broker or pairing."""
    entry = add_entry(hass)
    client = AsyncMock()
    client.status.return_value = {"codex_login": {"status_ok": True}}
    worker = discovery.WorkerConnection("http://worker", "fixture-first")
    request = {KEY_HASS_USER: SimpleNamespace(is_admin=True)}
    view = auth.BrowserSessionView(hass)
    with (
        patch.object(integration, "async_discover_worker", return_value=worker) as find,
        patch.object(integration, "CodexCliApiClient", return_value=client),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", return_value=None
        ),
        patch.object(hass.config_entries, "async_unload_platforms", return_value=True),
        patch.object(auth, "get_supervisor_network_url", return_value="http://worker"),
    ):
        context = current_entry.set(entry)
        try:
            assert await integration.async_setup_entry(hass, entry)
            entry._async_set_state(hass, ConfigEntryState.LOADED, None)
            first = entry.runtime_data.browser_sessions
            assert view.broker(request, {"worker_token": "fixture-first"}) is first
            session = await first.issue()
            assert await integration.async_unload_entry(hass, entry)
            assert first.closed and not first.sessions
            assert (
                hass.auth.async_validate_access_token(session["access_token"]) is None
            )
            with pytest.raises(web.HTTPServiceUnavailable):
                view.broker(request, {"worker_token": "fixture-first"})
            await entry._async_process_on_unload(hass)
            entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
            find.return_value = discovery.WorkerConnection(
                "http://worker", "fixture-second"
            )
            assert await integration.async_setup_entry(hass, entry)
            entry._async_set_state(hass, ConfigEntryState.LOADED, None)
            second = entry.runtime_data.browser_sessions
            assert second is not first and not second.closed
            assert view.broker(request, {"worker_token": "fixture-second"}) is second
            with pytest.raises(web.HTTPForbidden):
                view.broker(request, {"worker_token": "fixture-first"})
            assert await integration.async_unload_entry(hass, entry)
            await entry._async_process_on_unload(hass)
        finally:
            current_entry.reset(context)


@pytest.mark.parametrize(
    "errors,expected",
    [
        (["auth", "network"], ConfigEntryAuthFailed),
        (["network", "auth"], ConfigEntryAuthFailed),
        (["auth", "auth"], ConfigEntryAuthFailed),
        (["network", "network"], ConfigEntryNotReady),
    ],
)
async def test_actual_discovery_to_setup_preserves_auth_failure(hass, errors, expected):
    """A reachable worker's rejection is not erased by another unreachable URL."""
    entry = add_entry(hass)
    failures = [
        discovery.CodexCliAuthError("fixture auth")
        if error == "auth"
        else discovery.CodexCliApiError("fixture network")
        for error in errors
    ]
    client = AsyncMock()
    client.status.side_effect = failures
    with (
        patch.object(
            discovery, "_async_worker_addons", return_value=[{"slug": "fixture_worker"}]
        ),
        patch.object(
            discovery, "_async_provision_worker_token", new_callable=AsyncMock
        ),
        patch.object(
            discovery,
            "_candidate_urls_from_addon",
            return_value=["http://one", "http://two"],
        ),
        patch.object(discovery, "CodexCliApiClient", return_value=client),
        patch.object(integration, "BrowserSessions") as broker,
    ):
        with pytest.raises(expected):
            await integration.async_setup_entry(hass, entry)
        assert getattr(entry, "runtime_data", None) is None
        broker.assert_not_called()


async def test_discovery_accepts_later_success_after_auth_rejection(hass):
    """A working alternative still wins over a rejected candidate."""
    client = AsyncMock()
    client.status.side_effect = [discovery.CodexCliAuthError("fixture auth"), {}]
    with (
        patch.object(
            discovery, "_async_worker_addons", return_value=[{"slug": "fixture_worker"}]
        ),
        patch.object(
            discovery, "_async_provision_worker_token", new_callable=AsyncMock
        ),
        patch.object(
            discovery,
            "_candidate_urls_from_addon",
            return_value=["http://one", "http://two"],
        ),
        patch.object(discovery, "CodexCliApiClient", return_value=client),
    ):
        result = await discovery.async_discover_worker(hass, Mock())
        assert result.base_url == "http://two"


def test_sign_in_force_metadata_matches_action_default():
    """The visible field explains both cases that otherwise look like a no-op."""
    import json

    import yaml

    source = json.loads((ROOT / "strings.json").read_text())
    english = json.loads((ROOT / "translations/en.json").read_text())
    action = source["services"]["start_login"]
    assert english["services"]["start_login"] == action
    field = action["fields"]["force"]["description"]
    assert "false" in field and "already signed in" in field
    assert "running" in field and "restart" in field
    assert "current" in action["description"]
    services = yaml.safe_load((ROOT / "services.yaml").read_text())
    assert services["start_login"]["fields"]["force"]["default"] is False
    assert integration.START_LOGIN_SCHEMA({}) == {"force": False}
