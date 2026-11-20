"""Setup removes what the mailbox signal left on an install before 0.4, and the poll no longer reads mail."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "mailbox_removed_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)
sensors = importlib.import_module(spec.name + ".sensor")

STATUS = {
    "codex_login": {"status_ok": True},
    "mailbox": {"unread": {"lead": 1}, "messages": [
        {"message_id": "0" * 31 + "1", "thread_id": "0" * 31 + "1", "author": "codex", "time": 1.0},
    ]},
}


async def _set_up(hass, entry, client):
    """Run the entry's setup with a stand-in worker and no entity platforms."""
    worker = SimpleNamespace(base_url="http://worker", api_token="paired-worker")
    broker = SimpleNamespace(setup=AsyncMock(), close=lambda: None)
    with (
        patch.object(integration, "async_discover_worker", return_value=worker),
        patch.object(integration, "CodexCliApiClient", return_value=client),
        patch.object(integration, "BrowserSessions", return_value=broker),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None),
    ):
        context = current_entry.set(entry)
        try:
            assert await integration.async_setup_entry(hass, entry)
        finally:
            current_entry.reset(context)


async def _stop_polling(entry):
    """Stop the coordinator's refresh timer, so the test leaves no timer behind."""
    await entry.runtime_data.coordinator.async_shutdown()


async def test_setup_removes_the_old_sensor_entity_and_saved_ids(hass, hass_storage):
    """The old sensor entity and its saved message IDs are gone; another entity of the entry stays."""
    entry = MockConfigEntry(domain="codex_cli", data={"base_url": "http://worker"},
                            state=ConfigEntryState.SETUP_IN_PROGRESS)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", "codex_cli", f"{entry.entry_id}_mailbox_unread_lead",
                                       config_entry=entry)
    kept = registry.async_get_or_create("sensor", "codex_cli", f"{entry.entry_id}_last_task",
                                        config_entry=entry)
    store_key = f"codex_cli_mailbox_seen_{entry.entry_id}"
    hass_storage[store_key] = {"version": 1, "data": ["0" * 31 + "1"]}
    client = AsyncMock()
    client.status.return_value = STATUS

    await _set_up(hass, entry, client)

    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(kept.entity_id) is not None
    assert store_key not in hass_storage
    await _stop_polling(entry)


async def test_setup_without_leftovers_and_a_status_with_mail_fires_no_event(hass, hass_storage):
    """A new install sets up as before, and mail on the worker status no longer fires an event or saves IDs."""
    entry = MockConfigEntry(domain="codex_cli", data={"base_url": "http://worker"},
                            state=ConfigEntryState.SETUP_IN_PROGRESS)
    entry.add_to_hass(hass)
    events = []
    hass.bus.async_listen("codex_cli_mailbox_message", lambda event: events.append(event.data))
    client = AsyncMock()
    client.status.return_value = STATUS

    await _set_up(hass, entry, client)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert client.status.await_count == 2
    assert events == []
    assert not any(key.startswith("codex_cli_mailbox_seen_") for key in hass_storage)
    await _stop_polling(entry)


async def test_setup_continues_when_the_saved_ids_file_cannot_be_removed(hass, caplog):
    """An error from deleting the old file logs one warning, and setup still finishes."""
    entry = MockConfigEntry(domain="codex_cli", data={"base_url": "http://worker"},
                            state=ConfigEntryState.SETUP_IN_PROGRESS)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.status.return_value = STATUS

    with patch.object(integration.Store, "async_remove", side_effect=PermissionError("read-only")):
        await _set_up(hass, entry, client)

    warnings = [record for record in caplog.records if record.levelname == "WARNING"
                and "old mailbox file" in record.getMessage()]
    assert len(warnings) == 1
    assert "read-only" in warnings[0].getMessage()
    assert entry.runtime_data.coordinator is not None
    await _stop_polling(entry)


def test_no_sensor_reads_the_mailbox():
    """The sensor platform has no mailbox sensor class."""
    assert not hasattr(sensors, "CodexMailboxUnreadSensor")
