"""GitHub sensor behavior from the polled worker contract, with synthetic data."""
import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from test_github_repair import github_status, worker_status
from test_integration_polling import integration

binary_sensor = import_module(f"{integration.__name__}.binary_sensor")


def make_sensor(hass):
    coordinator = integration.CodexCliCoordinator(hass, AsyncMock())
    entry = MockConfigEntry(domain="codex_cli", data={})
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    sensor = binary_sensor.CodexGitHubConnectedBinarySensor(coordinator, entry)
    return coordinator, entry, sensor


@pytest.mark.parametrize("state", [
    "disconnected", "connecting", "ok", "near_expiry", "expired", "revoked", "access_changed", "failed",
])
async def test_only_ok_is_on(hass, state):
    coordinator, _, sensor = make_sensor(hass)
    coordinator.async_set_updated_data(worker_status(github_status(state)))
    assert sensor.is_on is (state == "ok")
    assert sensor.extra_state_attributes["state"] == state


@pytest.mark.parametrize("data", [None, {}, {"github": None}, {"github": {}}])
async def test_missing_github_status_is_off_with_an_unknown_summary(hass, data):
    coordinator, _, sensor = make_sensor(hass)
    coordinator.async_set_updated_data(data)
    assert sensor.is_on is False
    assert sensor.extra_state_attributes == {"state": "unknown", "account": None, "repository_count": 0}


async def test_poll_updates_the_sensor_without_an_extra_request(hass):
    coordinator, _, sensor = make_sensor(hass)
    for state in ("ok", "revoked", "disconnected", "ok"):
        coordinator.client.status.return_value = worker_status(github_status(state))
        await coordinator.async_refresh()
        assert sensor.is_on is (state == "ok")
    assert coordinator.client.status.await_count == 4


async def test_attributes_only_expose_the_state_login_and_repository_count(hass):
    coordinator, _, sensor = make_sensor(hass)
    # The account and flow carry extra fixture fields; the entity copies only the login.
    status = github_status("ok", account={"login": "synthetic-owner", "token": "SYNTHETIC-TOKEN"},
                           token="SYNTHETIC-TOKEN", device_code="TEST-0000",
                           flow={"user_code": "TEST-0000", "device_code": "TEST-0000"},
                           repositories=[{"full_name": "synthetic-owner/one"}, {"full_name": "synthetic-owner/two"}])
    coordinator.async_set_updated_data(worker_status(status))
    assert sensor.extra_state_attributes == {
        "state": "ok", "account": "synthetic-owner", "repository_count": 2,
    }
    status["account"]["login"] = "another-synthetic-owner"
    status["repositories"].pop()
    coordinator.async_set_updated_data(worker_status(status))
    assert sensor.extra_state_attributes == {
        "state": "ok", "account": "another-synthetic-owner", "repository_count": 1,
    }


async def test_setup_keeps_the_existing_sensors_and_attaches_the_new_one(hass):
    coordinator, entry, _ = make_sensor(hass)
    add_entities = Mock()
    await binary_sensor.async_setup_entry(hass, entry, add_entities)
    sensors = add_entities.call_args.args[0]
    assert [type(sensor) for sensor in sensors] == [
        binary_sensor.CodexRunningBinarySensor,
        binary_sensor.CodexSignedInBinarySensor,
        binary_sensor.CodexGitHubConnectedBinarySensor,
    ]
    sensor = sensors[-1]
    assert sensor.coordinator is coordinator
    assert sensor.unique_id == f"{entry.entry_id}_github_connected"
    assert sensor.device_info == {"identifiers": {("codex_cli", entry.entry_id)}}
    assert sensor.device_class is BinarySensorDeviceClass.CONNECTIVITY
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.has_entity_name is True
    assert sensor.translation_key == "github_connected"
    coordinator.async_set_updated_data(worker_status(github_status("ok")))
    assert sensor.available is True
    coordinator.last_update_success = False
    assert sensor.available is False


def test_the_source_and_english_catalogs_name_the_sensor():
    root = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
    for relative in ("strings.json", "translations/en.json"):
        catalog = json.loads((root / relative).read_text())
        assert catalog["entity"]["binary_sensor"]["github_connected"]["name"] == "GitHub connected"


def test_the_source_and_english_catalogs_match():
    root = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
    source = json.loads((root / "strings.json").read_text())
    english = json.loads((root / "translations/en.json").read_text())
    assert source == english
