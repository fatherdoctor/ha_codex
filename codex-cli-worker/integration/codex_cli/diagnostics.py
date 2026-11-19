"""Diagnostics support for the Codex integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN
from .api import CodexCliApiError
from .diagnostics_scrub import sanitize_export

TO_REDACT = {
    CONF_API_TOKEN,
    "Authorization",
    "api_token",
    "details",
    "prompt",
    "qr_url",
    "user_code",
    "verification_url",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = getattr(entry, "runtime_data", None)
    payload = {"interactions": [], "availability": "unavailable"}
    if runtime_data is not None:
        try:
            payload = await runtime_data.client.interaction_diagnostics()
        except CodexCliApiError:
            pass  # Never export a server URL or arbitrary exception message.
    # Raw entry and coordinator data contain prompts, titles and addresses.
    # Both download paths use the same schema, followed by HA's redaction.
    return async_redact_data(sanitize_export(payload), TO_REDACT)
