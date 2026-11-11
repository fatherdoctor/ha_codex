"""Binary sensors for the Codex CLI integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BASE_URL, DOMAIN
from .coordinator import CodexCliCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Codex CLI binary sensors."""
    coordinator: CodexCliCoordinator = entry.runtime_data.coordinator
    async_add_entities([CodexRunningBinarySensor(coordinator, entry), CodexSignedInBinarySensor(coordinator, entry)])


class CodexRunningBinarySensor(CoordinatorEntity[CodexCliCoordinator], BinarySensorEntity):
    """Show whether a Codex task is running."""

    _attr_has_entity_name = True
    _attr_device_class = getattr(BinarySensorDeviceClass, "RUNNING", None)
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "task_running"

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Set the unique ID and attach the sensor to the Codex device of the config entry."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_task_running"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Codex",
            "manufacturer": "OpenAI",
            "model": "Codex CLI Worker",
            "configuration_url": entry.data.get(CONF_BASE_URL),
        }

    @property
    def is_on(self) -> bool:
        """Return true if the worker status names an active task."""
        return bool((self.coordinator.data or {}).get("active_task_id"))


class CodexSignedInBinarySensor(CoordinatorEntity[CodexCliCoordinator], BinarySensorEntity):
    """Show whether Codex is signed in to ChatGPT, so an expired sign-in can drive an alert or a card."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "signed_in"

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Set the unique ID and attach the sensor to the Codex device of the config entry."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_signed_in"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def is_on(self) -> bool:
        """Return true when the worker reports a working Codex sign-in."""
        return bool(((self.coordinator.data or {}).get("codex_login") or {}).get("status_ok"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return whether a sign-in is waiting for its code to be entered."""
        flow = (self.coordinator.data or {}).get("auth_flow") or {}
        return {"sign_in_waiting": bool(flow.get("pending"))}
