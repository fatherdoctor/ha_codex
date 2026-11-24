"""What the Codex entities show to every Home Assistant user (CX-04-V8 F1, F3, F4, F5).

A state and its attributes reach every signed-in user and the recorder, so they must not carry the sign-in code,
the chat's own text or a piece of the status screen. All values here are invented.
"""

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "attributes_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)

SYNTHETIC_CODE = "TEST-0000"
SYNTHETIC_URL = "https://sign-in.example.invalid/device"
CHAT_TITLE = "Invented chat title"
CHAT_SUMMARY = "Invented chat summary"
STATUS_EXCERPT = "invented status screen excerpt"

WORKER_DATA = {
    "latest_task": {
        "task_id": "task-1", "status": "waiting_for_input", "title": CHAT_TITLE, "summary": CHAT_SUMMARY,
        "question": "Which one?", "choices": ["A", "B"], "current_turn_id": "0" * 32,
    },
    "codex_login": {"status_ok": False, "has_auth_file": False, "message": "Not signed in"},
    "auth_flow": {"status": "waiting_for_user", "user_code": SYNTHETIC_CODE, "verification_url": SYNTHETIC_URL,
                  "qr_url": SYNTHETIC_URL + "?qr=1"},
    "codex_usage": {"five_hour_percent": "40", "weekly_percent": "70", "raw_excerpt": STATUS_EXCERPT},
}


async def entities_of(hass, platform, data):
    module = importlib.import_module("attributes_ha_test." + platform)
    entry = MockConfigEntry(domain="codex_cli", data={"base_url": "http://worker"})
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data=data))
    found = []
    await module.async_setup_entry(hass, entry, found.extend)
    return found


@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
async def test_no_attribute_shows_the_sign_in_code_or_chat_text(hass, platform):
    """F1: the sign-in code, its addresses, the chat's title and summary and the status excerpt stay off entities."""
    for entity in await entities_of(hass, platform, WORKER_DATA):
        shown = json.dumps([entity.extra_state_attributes or {}, entity.device_info], default=str)
        for secret in (SYNTHETIC_CODE, SYNTHETIC_URL, CHAT_TITLE, CHAT_SUMMARY, STATUS_EXCERPT):
            assert secret not in shown, (type(entity).__name__, secret)


async def test_last_task_keeps_what_a_reply_automation_needs(hass):
    """F1: the question, its choices and the IDs that codex_cli.reply_task takes stay on the Last task sensor."""
    sensor = next(e for e in await entities_of(hass, "sensor", WORKER_DATA) if e._key == "last_task")
    attributes = sensor.extra_state_attributes
    assert attributes["task_id"] == "task-1"
    assert attributes["question"] == "Which one?"
    assert attributes["choices"] == ["A", "B"]
    assert attributes["turn_id"] == "0" * 32


@pytest.mark.parametrize("key", ["last_task", "auth_status"])
async def test_the_word_sensors_are_enums_with_translated_states(hass, key):
    """F3: each state is one of the sensor's options, and every option has an English name."""
    sensor = next(e for e in await entities_of(hass, "sensor", WORKER_DATA) if e._key == key)
    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.native_value in sensor.options
    english = json.loads((ROOT / "translations" / "en.json").read_text())
    assert sorted(english["entity"]["sensor"][key]["state"]) == sorted(sensor.options)


async def test_last_task_is_unknown_with_no_task(hass):
    """F3: with no task the state is None (Home Assistant's unknown), not a word that looks like a state."""
    sensor = next(e for e in await entities_of(hass, "sensor", {}) if e._key == "last_task")
    assert sensor.native_value is None


def test_the_last_task_options_are_the_worker_task_states():
    """F3: the options are the worker's task states, which the list_tasks action already names."""
    sensor_module = sys.modules["attributes_ha_test.sensor"]
    allowed = integration.LIST_TASKS_SCHEMA.schema[vol.Optional("status")].container
    assert sorted(sensor_module.TASK_STATES) == sorted(allowed)


# Each schema that takes a task ID, with the other fields it needs.
TASK_ID_SCHEMAS = [
    (integration.TASK_ID_SCHEMA, {}),
    (integration.REPLY_TASK_SCHEMA, {"reply": "yes"}),
    (integration.CONTINUE_TASK_SCHEMA, {"message": "more"}),
]


@pytest.mark.parametrize(("schema", "other"), TASK_ID_SCHEMAS)
@pytest.mark.parametrize("task_id", ["x/../../auth/logout#", "a?b", "a#b", "task-1\n", "", ".hidden"])
def test_a_task_id_that_is_not_the_worker_form_is_refused(schema, other, task_id):
    """F4: a task ID goes into the worker URL, so a "/", "?" or "#" never reaches another route."""
    with pytest.raises(vol.Invalid):
        schema({"task_id": task_id, **other})


@pytest.mark.parametrize(("schema", "other"), TASK_ID_SCHEMAS)
def test_a_worker_task_id_passes(schema, other):
    """F4: the worker's own ID form still passes every schema."""
    assert schema({"task_id": "20261124-abc_1.2", **other})["task_id"] == "20261124-abc_1.2"


def test_strings_json_is_gone():
    """F6: translations/en.json is the one text file; Home Assistant does not read strings.json for a custom integration."""
    assert not (ROOT / "strings.json").exists()
