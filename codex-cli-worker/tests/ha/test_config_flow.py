"""Exercise the real config flow with Home Assistant and synthetic worker discovery."""

import importlib.util
import json
import sys
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
    mock_platform,
)

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "config_flow_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)
flow = importlib.import_module(f"{spec.name}.config_flow")
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
DOMAIN = MANIFEST["domain"]
WORKER_DATA = {"base_url": "http://worker"}


@pytest.fixture
def discovery(hass: HomeAssistant) -> Generator[AsyncMock]:
    """Keep config-entry setup and discovery offline; run the actual flow manager."""
    mock_integration(
        hass,
        MockModule(
            DOMAIN,
            async_setup_entry=AsyncMock(return_value=True),
            partial_manifest={
                "config_flow": MANIFEST["config_flow"],
                "single_config_entry": MANIFEST["single_config_entry"],
            },
        ),
        top_level_files={"config_flow.py"},
    )
    mock_platform(hass, f"{DOMAIN}.config_flow", flow)
    with patch.object(
        flow,
        "async_discover_worker",
        return_value=SimpleNamespace(**WORKER_DATA),
    ) as discover:
        yield discover


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_USER, config_entries.SOURCE_HASSIO]
)
async def test_discovery_creates_one_entry(
    hass: HomeAssistant, discovery: AsyncMock, source: str
) -> None:
    """Both setup entry points discover the worker without requesting credentials."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": source}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Codex"
    assert result["data"] == WORKER_DATA
    assert result["result"].unique_id == DOMAIN
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    discovery.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        (flow.CodexCliApiError, "cannot_connect"),
        (flow.CodexCliAuthError, "invalid_auth"),
    ],
)
async def test_user_error_then_retry(
    hass: HomeAssistant, discovery: AsyncMock, error: type[Exception], error_key: str
) -> None:
    """Failed discovery shows an empty retry form and never stores credentials."""
    discovery.side_effect = error("synthetic discovery failure")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error_key}
    assert result["data_schema"]({}) == {}
    assert hass.config_entries.async_entries(DOMAIN) == []

    discovery.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == WORKER_DATA
    assert result["result"].unique_id == DOMAIN
    assert result["title"] == "Codex"
    assert discovery.await_count == 2


@pytest.mark.parametrize("error", [flow.CodexCliApiError, flow.CodexCliAuthError])
async def test_hassio_discovery_failure_aborts(
    hass: HomeAssistant, discovery: AsyncMock, error: type[Exception]
) -> None:
    """Supervisor discovery uses its fixed abort key for either API failure."""
    discovery.side_effect = error("synthetic discovery failure")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_USER, config_entries.SOURCE_HASSIO]
)
async def test_single_entry_aborts_before_discovery(
    hass: HomeAssistant, discovery: AsyncMock, source: str
) -> None:
    """An existing entry prevents either setup path from contacting the worker."""
    entry = MockConfigEntry(domain=DOMAIN, data=WORKER_DATA, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": source}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    discovery.assert_not_awaited()


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_USER, config_entries.SOURCE_HASSIO]
)
async def test_flow_unique_id_guard_also_refuses_duplicate(
    hass: HomeAssistant, discovery: AsyncMock, source: str
) -> None:
    """Exercise the flow's own guard independently of Core's single-entry shortcut."""
    entry = MockConfigEntry(domain=DOMAIN, data=WORKER_DATA, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    handler = flow.CodexCliConfigFlow()
    handler.hass = hass
    handler.handler = DOMAIN
    handler.context = {"source": source}
    with pytest.raises(AbortFlow) as abort:
        if source == config_entries.SOURCE_USER:
            await handler.async_step_user()
        else:
            await handler.async_step_hassio({})
    assert abort.value.reason == "already_configured"
    discovery.assert_not_awaited()
    assert hass.config_entries.async_entries(DOMAIN) == [entry]


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        (flow.CodexCliApiError, "cannot_connect"),
        (flow.CodexCliAuthError, "invalid_auth"),
    ],
)
async def test_existing_entry_form_error_and_success(
    hass: HomeAssistant,
    discovery: AsyncMock,
    source: str,
    error: type[Exception],
    error_key: str,
) -> None:
    """Confirm explicitly; preserve data on failure, then rediscover and reload once."""
    original = {"base_url": "http://previous-worker"}
    entry = MockConfigEntry(
        domain=DOMAIN, data=original, options={"base_url": "legacy"}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    step = "reauth_confirm" if source == config_entries.SOURCE_REAUTH else "reconfigure"
    with patch.object(
        hass.config_entries, "async_reload", return_value=True
    ) as reload_entry:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source, "entry_id": entry.entry_id},
            data=entry.data if source == config_entries.SOURCE_REAUTH else None,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step
        assert result["errors"] == {}
        assert result["data_schema"]({}) == {}
        discovery.assert_not_awaited()
        reload_entry.assert_not_awaited()

        discovery.side_effect = error("synthetic discovery failure")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step
        assert result["errors"] == {"base": error_key}
        assert entry.data == original
        assert entry.options == {"base_url": "legacy"}
        reload_entry.assert_not_awaited()

        discovery.side_effect = None
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == f"{source}_successful"
        assert entry.data == WORKER_DATA
        assert entry.options == {}
        reload_entry.assert_awaited_once_with(entry.entry_id)
        assert discovery.await_count == 2


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
async def test_missing_entry_aborts(
    hass: HomeAssistant, discovery: AsyncMock, source: str
) -> None:
    """A removed entry cannot be recreated by an old confirmation flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": source, "entry_id": "missing-entry"}, data={}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"
    assert hass.config_entries.async_entries(DOMAIN) == []
    discovery.assert_not_awaited()
