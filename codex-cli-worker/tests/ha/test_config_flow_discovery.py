"""Supervisor discovery offers Codex setup and adds nothing until the owner confirms."""

import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "flow_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
config_flow = importlib.import_module("flow_ha_test.config_flow")
api = importlib.import_module("flow_ha_test.api")

WORKER = {"base_url": "http://codex-worker:9123"}


def discovery_flow(hass):
    """Return a config flow started by Supervisor discovery."""
    flow = config_flow.CodexCliConfigFlow()
    flow.hass = hass
    flow.handler = "codex_cli"
    flow.flow_id = "discovery"
    flow.context = {"source": "hassio"}
    return flow


async def test_discovery_shows_a_confirm_form_before_adding_the_entry(hass):
    """The discovered flow shows a form first; the worker is contacted and the entry made only on confirm."""
    flow = discovery_flow(hass)
    with patch.object(config_flow, "_discover_input", AsyncMock(return_value=WORKER)) as discover:
        result = await flow.async_step_hassio({})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "hassio_confirm"
        discover.assert_not_awaited()

        result = await flow.async_step_hassio_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Codex"
    assert result["data"] == WORKER


async def test_confirm_shows_the_error_and_lets_the_owner_retry(hass):
    """A worker that cannot be reached on confirm keeps the form open with the connection error."""
    flow = discovery_flow(hass)
    await flow.async_step_hassio({})
    with patch.object(config_flow, "_discover_input", AsyncMock(side_effect=api.CodexCliApiError("down"))):
        result = await flow.async_step_hassio_confirm({})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_discovery_aborts_when_codex_is_already_set_up(hass):
    """The app announces itself at every start; an existing entry ends the discovered flow quietly."""
    MockConfigEntry(domain="codex_cli", unique_id="codex_cli", data=WORKER).add_to_hass(hass)
    with pytest.raises(AbortFlow) as aborted:  # the flow manager turns this into an abort result
        await discovery_flow(hass).async_step_hassio({})
    assert aborted.value.reason == "already_configured"


def test_the_confirm_step_has_text():
    """The confirm form has a title and description in the integration's translations."""
    import json

    strings = json.loads((ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
    step = strings["config"]["step"]["hassio_confirm"]
    assert step["title"] and step["description"]
