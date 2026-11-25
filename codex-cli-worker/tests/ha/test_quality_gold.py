"""The Gold checklist claims `devices` and `repair-issues` in quality_scale.yaml, held by the code."""

import importlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "gold_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)
coordinator_module = sys.modules["gold_ha_test.coordinator"]
core_logs = sys.modules["gold_ha_test.core_logs"]

ISSUES = ("codex_not_signed_in", "remote_control_config_change", "github_access_lost")


def add_entry(hass, state=ConfigEntryState.LOADED):
    entry = MockConfigEntry(
        domain="codex_cli", data={"base_url": "http://worker"}, state=state
    )
    entry.add_to_hass(hass)
    return entry


def raise_every_issue(hass):
    for issue_id in ISSUES:
        ir.async_create_issue(
            hass,
            "codex_cli",
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id,
        )


async def entities_of(hass, platform):
    module = importlib.import_module("gold_ha_test." + platform)
    entry = add_entry(hass)
    entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data={}))
    found = []
    await module.async_setup_entry(hass, entry, found.extend)
    return found


@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
async def test_every_entity_is_on_a_service_device(hass, platform):
    """`devices: done`: the device is a service (a local worker, not hardware), on every entity that names it."""
    entities = await entities_of(hass, platform)
    assert entities
    for entity in entities:
        assert entity.device_info["entry_type"] is DeviceEntryType.SERVICE, type(
            entity
        ).__name__


async def test_unload_deletes_every_repair_issue(hass):
    """`repair-issues: done`: an unloaded integration leaves no repair behind."""
    entry = add_entry(hass)
    raise_every_issue(hass)
    registry = ir.async_get(hass)
    assert all(registry.async_get_issue("codex_cli", i) for i in ISSUES)
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        assert await integration.async_unload_entry(hass, entry)
    assert not any(registry.async_get_issue("codex_cli", i) for i in ISSUES)


async def test_failed_unload_keeps_the_repairs(hass):
    """A platform that refuses to unload leaves the integration running, so its repairs stay true."""
    entry = add_entry(hass)
    raise_every_issue(hass)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await integration.async_unload_entry(hass, entry)
    registry = ir.async_get(hass)
    assert all(registry.async_get_issue("codex_cli", i) for i in ISSUES)


async def test_removal_deletes_every_repair_issue(hass):
    """Removing an entry that never loaded (so never unloaded) still deletes its repairs."""
    entry = add_entry(hass, ConfigEntryState.SETUP_ERROR)
    raise_every_issue(hass)
    await integration.async_remove_entry(hass, entry)
    registry = ir.async_get(hass)
    assert not any(registry.async_get_issue("codex_cli", i) for i in ISSUES)


def test_the_list_names_every_issue_the_coordinator_raises():
    """A new repair added to the coordinator must join REPAIR_ISSUES, or unload would leave it behind."""
    assert (ROOT / "coordinator.py").read_text().count("ir.async_create_issue(") == len(
        ISSUES
    )
    assert set(coordinator_module.REPAIR_ISSUES) == set(ISSUES)


@pytest.mark.parametrize(
    "state", [s for s in ConfigEntryState if s is not ConfigEntryState.LOADED]
)
async def test_one_helper_picks_only_loaded_entries(hass, state):
    """Actions and the Core log view pick an entry through one helper, and it skips every entry not loaded."""
    idle = add_entry(hass, state)
    idle.runtime_data = "not loaded"
    loaded = add_entry(hass)
    loaded.runtime_data = "loaded"
    assert list(core_logs.loaded_runtime_data(hass)) == ["loaded"]
    assert "loaded_runtime_data(" in (ROOT / "__init__.py").read_text()
    assert (
        "async_entries("
        not in (ROOT / "__init__.py")
        .read_text()
        .split("def _first_runtime", 1)[1]
        .split("\ndef ", 1)[0]
    )
