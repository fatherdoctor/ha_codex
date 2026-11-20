"""Home Assistant integration for the Codex CLI Worker add-on."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from functools import wraps
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import CodexCliApiClient, CodexCliApiError, CodexCliAuthError
from .const import (
    ATTR_PROMPT,
    ATTR_REPLY,
    ATTR_TASK_ID,
    ATTR_TURN_ID,
    ATTR_FORCE,
    CONF_BASE_URL,
    DOMAIN,
    SERVICE_CANCEL_TASK,
    SERVICE_CONTINUE_TASK,
    SERVICE_GET_LOGIN_STATUS,
    SERVICE_GET_TASK,
    SERVICE_LOGOUT,
    SERVICE_LIST_TASKS,
    SERVICE_REPLY_TASK,
    SERVICE_START_LOGIN,
    SERVICE_START_TASK,
)
from .coordinator import CodexCliCoordinator, async_delete_repair_issues
from .discovery import async_discover_worker
from .notification_actions import async_listen_for_choices
from .browser_auth import STORAGE_KEY, BrowserSessions, BrowserSessionView, DiagnosticView, loaded_runtime_data
from .kiosk import KioskScreenshotsView

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

START_TASK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PROMPT): str,
    }
)
TASK_ID_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): str})
REPLY_TASK_SCHEMA = vol.Schema(
    {vol.Required(ATTR_TASK_ID): str, vol.Required(ATTR_REPLY): str, vol.Optional(ATTR_TURN_ID): str}
)
CONTINUE_TASK_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): str, vol.Required("message"): str})
LIST_TASKS_SCHEMA = vol.Schema({
    vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
    vol.Optional("offset"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("status"): vol.In(["queued", "running", "waiting_for_input", "completed", "failed", "cancelled"]),
    vol.Optional("order"): vol.In(["created_asc", "updated_desc"]),
    vol.Optional("summary"): bool,
})
START_LOGIN_SCHEMA = vol.Schema({vol.Optional(ATTR_FORCE, default=False): bool})
# The key of the mailbox sensor that version 0.4 removed; installs before 0.4 have this entity.
REMOVED_MAILBOX_SENSOR_KEY = "mailbox_unread_lead"


@dataclass(slots=True)
class CodexCliRuntimeData:
    """Runtime objects for one Codex config entry."""

    client: CodexCliApiClient
    coordinator: CodexCliCoordinator
    browser_sessions: BrowserSessions


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Codex CLI integration."""
    _async_register_services(hass)
    hass.http.register_view(BrowserSessionView(hass))
    hass.http.register_view(DiagnosticView(hass))
    hass.http.register_view(KioskScreenshotsView(hass))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Codex CLI from a config entry."""
    session = async_get_clientsession(hass)
    try:
        worker = await async_discover_worker(hass, session)
    except CodexCliAuthError as exc:
        raise ConfigEntryAuthFailed("Worker authentication failed") from exc
    except CodexCliApiError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc

    _async_update_discovered_entry(hass, entry, worker.base_url)
    await _async_remove_mailbox_leftovers(hass, entry)
    client = CodexCliApiClient(session, worker.base_url, worker.api_token)
    coordinator = CodexCliCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    broker = BrowserSessions(hass, worker.api_token)
    # Setup can fail before Core owns the unload callbacks. Close all acquired
    # resources on failure, and transfer their cleanup to the entry on success.
    with ExitStack() as cleanup:
        cleanup.callback(broker.close)
        await broker.setup()
        entry.runtime_data = CodexCliRuntimeData(
            client=client, coordinator=coordinator, browser_sessions=broker
        )
        cleanup.callback(setattr, entry, "runtime_data", None)
        cleanup.callback(entry.add_update_listener(_async_update_listener))
        cleanup.callback(async_listen_for_choices(hass, client, coordinator))
        # Keep contact even when every entity is disabled, while respecting
        # the config entry's setting to disable polling.
        cleanup.callback(coordinator.async_add_listener(lambda: None))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(cleanup.pop_all().close)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Codex CLI config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Revoke browser sessions here, before Core marks the entry not loaded. The cleanup stack that setup hands
        # to entry.async_on_unload closes the broker again (closing twice is harmless): it also covers a failed setup.
        runtime_data = getattr(entry, "runtime_data", None)
        if isinstance(runtime_data, CodexCliRuntimeData):
            runtime_data.browser_sessions.close()
        entry.runtime_data = None
        async_delete_repair_issues(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the integration-owned identity, its credentials and its repair issues on uninstall."""
    async_delete_repair_issues(hass)
    store = Store(hass, 1, STORAGE_KEY)
    data = await store.async_load() or {}
    user = await hass.auth.async_get_user(data.get("user_id", ""))
    if user and user.system_generated:
        await hass.auth.async_remove_user(user)
    await store.async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_update_discovered_entry(hass: HomeAssistant, entry: ConfigEntry, base_url: str) -> None:
    """Persist only non-secret discovered connection metadata."""
    data = {CONF_BASE_URL: base_url}
    if dict(entry.data) != data or dict(entry.options):
        hass.config_entries.async_update_entry(entry, data=data, options={})


async def _async_remove_mailbox_leftovers(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the mailbox sensor entity and the saved message IDs that an install before 0.4 kept.

    Without this step, the removed sensor stays in the entity registry as an orphan.
    """
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_{REMOVED_MAILBOX_SENSOR_KEY}"
    if entity_id := registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id):
        registry.async_remove(entity_id)
    store = Store(hass, 1, f"{DOMAIN}_mailbox_seen_{entry.entry_id}")
    try:
        await store.async_remove()
    except OSError as err:
        # A file left behind is harmless, since nothing reads it any more.
        # Do not let it stop the setup.
        _LOGGER.warning("Could not remove the old mailbox file %s: %s", store.path, err)


def _first_runtime(hass: HomeAssistant) -> CodexCliRuntimeData:
    """Return the first loaded Codex runtime."""
    for runtime_data in loaded_runtime_data(hass):
        if isinstance(runtime_data, CodexCliRuntimeData):
            return runtime_data
    raise ServiceValidationError("Codex integration is not configured or is not loaded")


def _admin_only(hass: HomeAssistant, handler: Callable[[ServiceCall], Awaitable[Any]]) -> Callable[[ServiceCall], Awaitable[Any]]:
    """Run a Codex action only for a Home Assistant administrator.

    A call that names a user (the UI, the API, a script started by a person)
    must come from an active administrator. A call with no user, such as an
    automation an administrator wrote, is allowed, as Home Assistant's own admin
    services do.
    """

    @wraps(handler)
    async def checked(call: ServiceCall) -> Any:
        """Refuse a caller who is not an administrator, then run the action."""
        user_id = call.context.user_id
        if user_id is not None:
            user = await hass.auth.async_get_user(user_id)
            if user is None or not user.is_active or not user.is_admin:
                raise Unauthorized(context=call.context, permission="admin")
        return await handler(call)

    return checked


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_START_TASK):
        return

    register_kwargs: dict[str, Any] = {}
    try:
        from homeassistant.core import SupportsResponse

        register_kwargs["supports_response"] = SupportsResponse.OPTIONAL
    except ImportError:
        pass

    async def handle_start_task(call: ServiceCall) -> dict[str, Any]:
        """Start a Codex task with the given prompt, refresh the coordinator and return the worker's response."""
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.start_task(call.data[ATTR_PROMPT])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_start_login(call: ServiceCall) -> dict[str, Any]:
        """Start the Codex login flow on the worker, even when logged in if forced, and refresh the coordinator."""
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.start_login(bool(call.data.get(ATTR_FORCE, False)))
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_get_login_status(call: ServiceCall) -> dict[str, Any]:
        """Return the worker's Codex login flow status."""
        runtime_data = _first_runtime(hass)
        try:
            return await runtime_data.client.login_status()
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_logout(call: ServiceCall) -> dict[str, Any]:
        """Remove the saved Codex credentials from the worker and refresh the coordinator."""
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.logout()
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_get_task(call: ServiceCall) -> dict[str, Any]:
        """Return the worker's record of the task with the given task ID."""
        runtime_data = _first_runtime(hass)
        try:
            return await runtime_data.client.get_task(call.data[ATTR_TASK_ID])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_list_tasks(call: ServiceCall) -> dict[str, Any]:
        """Return the worker's tasks, passing the service data on as list filters."""
        runtime_data = _first_runtime(hass)
        try:
            return await runtime_data.client.list_tasks(**call.data)
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_cancel_task(call: ServiceCall) -> dict[str, Any]:
        """Cancel the task with the given task ID on the worker and refresh the coordinator."""
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.cancel_task(call.data[ATTR_TASK_ID])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_reply_task(call: ServiceCall) -> dict[str, Any]:
        """Send a reply to a task that is waiting for input and refresh the coordinator.

        With a turn ID, the worker takes the reply only while that turn's question is the one waiting.
        """
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.reply_task(
                call.data[ATTR_TASK_ID], call.data[ATTR_REPLY], call.data.get(ATTR_TURN_ID)
            )
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_continue_task(call: ServiceCall) -> dict[str, Any]:
        """Continue a saved conversation with a new message and refresh the coordinator."""
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.continue_task(call.data[ATTR_TASK_ID], call.data["message"])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    hass.services.async_register(
        DOMAIN, SERVICE_CONTINUE_TASK, _admin_only(hass, handle_continue_task),
        schema=CONTINUE_TASK_SCHEMA, **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_TASK,
        _admin_only(hass, handle_start_task),
        schema=START_TASK_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_LOGIN,
        _admin_only(hass, handle_start_login),
        schema=START_LOGIN_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_LOGIN_STATUS,
        _admin_only(hass, handle_get_login_status),
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOGOUT,
        _admin_only(hass, handle_logout),
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TASK,
        _admin_only(hass, handle_get_task),
        schema=TASK_ID_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_TASKS,
        _admin_only(hass, handle_list_tasks),
        schema=LIST_TASKS_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_TASK,
        _admin_only(hass, handle_cancel_task),
        schema=TASK_ID_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPLY_TASK,
        _admin_only(hass, handle_reply_task),
        schema=REPLY_TASK_SCHEMA,
        **register_kwargs,
    )
