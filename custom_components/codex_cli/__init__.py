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
    TASK_ID_PATTERN,
)
from .coordinator import CodexCliCoordinator, async_delete_repair_issues
from .discovery import async_discover_worker
from .notification_actions import async_listen_for_choices
from .core_logs import DiagnosticView, loaded_runtime_data

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

START_TASK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PROMPT): str,
    }
)
# The ID goes into the worker URL (/tasks/<id>/...), so only the worker's own ID form passes.
TASK_ID = vol.All(str, vol.Match(rf"^{TASK_ID_PATTERN}\Z"))
TASK_ID_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): TASK_ID})
REPLY_TASK_SCHEMA = vol.Schema(
    {vol.Required(ATTR_TASK_ID): TASK_ID, vol.Required(ATTR_REPLY): str, vol.Optional(ATTR_TURN_ID): str}
)
CONTINUE_TASK_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): TASK_ID, vol.Required("message"): str})
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
# The read-only system user that versions before 0.4.0 created for dashboard screenshots, and the file that holds
# its ID. Version 0.4.0 removed the screenshots; setup removes this user. Remove this step after version 0.5.
REMOVED_BROWSER_USER_NAME = "Dashboard verification"
REMOVED_BROWSER_STORE_KEY = f"{DOMAIN}.browser_identity"


@dataclass(slots=True)
class CodexCliRuntimeData:
    """Runtime objects for one Codex config entry."""

    client: CodexCliApiClient
    coordinator: CodexCliCoordinator
    # The paired worker's token, which the Core log view compares with the token in each request.
    worker_token: str


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Codex CLI integration."""
    _async_register_services(hass)
    hass.http.register_view(DiagnosticView(hass))
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
    await _async_remove_browser_user(hass)
    client = CodexCliApiClient(session, worker.base_url, worker.api_token)
    coordinator = CodexCliCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    # Setup can fail before Core owns the unload callbacks. Close all acquired
    # resources on failure, and transfer their cleanup to the entry on success.
    with ExitStack() as cleanup:
        entry.runtime_data = CodexCliRuntimeData(
            client=client, coordinator=coordinator, worker_token=worker.api_token
        )
        cleanup.callback(setattr, entry, "runtime_data", None)
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
        entry.runtime_data = None
        async_delete_repair_issues(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the repair issues, and a browser user that a version before 0.4.0 left, on uninstall."""
    async_delete_repair_issues(hass)
    await _async_remove_browser_user(hass)


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


async def _async_remove_browser_user(hass: HomeAssistant) -> None:
    """Remove the dashboard screenshot user that a version before 0.4.0 created, and the file that holds its ID.

    Only the user whose ID that file holds is removed, and only if Home Assistant marks it as a system user with
    the name the old version gave it, so a person's account is never touched; a kept user gets one log line. The
    file goes in every case.
    """
    store = Store(hass, 1, REMOVED_BROWSER_STORE_KEY)
    data = await store.async_load()
    if data is None:
        return
    user_id = data.get("user_id") if isinstance(data, dict) else None
    user = await hass.auth.async_get_user(user_id) if isinstance(user_id, str) else None
    if user is not None and user.system_generated and user.name == REMOVED_BROWSER_USER_NAME:
        await hass.auth.async_remove_user(user)
        _LOGGER.info("Removed the %s user that an earlier version created for dashboard screenshots",
                     REMOVED_BROWSER_USER_NAME)
    elif user is not None:
        # The file goes below, so this line is the only record of where a renamed leftover account came from.
        _LOGGER.info("Kept user %s, which the dashboard screenshot file of an earlier version names: it is not "
                     "a system user named %s", user.id, REMOVED_BROWSER_USER_NAME)
    await store.async_remove()


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
