"""Every Codex action refuses a signed-in user who is not an administrator (CG-3)."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "admin_services_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)

# Each action with data its schema accepts.
CALLS = [
    ("start_task", {"prompt": "Which lights are on?"}),
    ("start_login", {}),
    ("get_login_status", {}),
    ("logout", {}),
    ("get_task", {"task_id": "chat"}),
    ("list_tasks", {}),
    ("cancel_task", {"task_id": "chat"}),
    ("reply_task", {"task_id": "chat", "reply": "Yes"}),
    ("continue_task", {"task_id": "chat", "message": "And the porch?"}),
]


@pytest.fixture
def runtime():
    """Register the actions against a fake worker client."""
    client = AsyncMock()
    for method in ("start_task", "start_login", "login_status", "logout", "get_task", "list_tasks",
                   "cancel_task", "reply_task", "continue_task"):
        getattr(client, method).return_value = {"ok": True}
    data = SimpleNamespace(client=client, coordinator=AsyncMock())
    with patch.object(integration, "_first_runtime", return_value=data):
        yield data


async def _call(hass, service, data, context):
    """Call one Codex action and wait for its answer."""
    return await hass.services.async_call(
        "codex_cli", service, data, blocking=True, context=context, return_response=True)


async def test_non_admin_is_refused_everywhere(hass, hass_read_only_user, runtime):
    """A signed-in ordinary user reaches no action, and nothing reaches the worker."""
    integration._async_register_services(hass)
    for service, data in CALLS:
        with pytest.raises(Unauthorized):
            await _call(hass, service, data, Context(user_id=hass_read_only_user.id))
    assert runtime.client.mock_calls == []


async def test_admin_and_automations_are_served(hass, hass_admin_user, runtime):
    """An administrator, and a call with no user such as an automation, run each action."""
    integration._async_register_services(hass)
    for service, data in CALLS:
        for context in (Context(user_id=hass_admin_user.id), Context()):
            assert await _call(hass, service, data, context) == {"ok": True}
