"""Exercise actual Home Assistant Repairs, events and durable deduplication."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import issue_registry as ir

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location("wait_notice_ha_test", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)
C = sys.modules[spec.name + ".coordinator"]
START = datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp()


def row(kind="question", wait_id="a" * 32):
    return {"wait_id": wait_id, "task_id": "b" * 32, "kind": kind, "waiting_since": START}


def setup(hass, rows, minutes=30):
    client = AsyncMock()
    client.status.return_value = {"codex_login": {"status_ok": True},
                                  "task_waits": {"available": True, "minutes": minutes, "items": rows}}
    return C.CodexCliCoordinator(hass, client, "entry"), client


async def update(hass, coordinator, age):
    with patch.object(C.dt_util, "utcnow", return_value=datetime.fromtimestamp(START + age, timezone.utc)):
        await coordinator._async_update_data()
    await hass.async_block_till_done()


@pytest.mark.parametrize("kind", ["question", "approval", "mailbox"])
async def test_timer_once_restart_and_resolution(hass, kind):
    coordinator, client = setup(hass, [row(kind)])
    events = []

    async def record(event):
        events.append(event.data)

    hass.bus.async_listen("codex_cli_unanswered_wait", record)
    issue_id = f"{coordinator._wait_key}_{'a' * 32}"
    await update(hass, coordinator, 1799)
    assert not events
    await update(hass, coordinator, 1800)
    assert len(events) == 1 and events[0]["kind"] == kind and events[0]["active"] is True
    assert set(events[0]) == {"wait_id", "task_id", "kind", "waiting_since", "active"}
    issue = ir.async_get(hass).async_get_issue("codex_cli", issue_id)
    assert issue.translation_placeholders["task"] == "b" * 32
    assert issue.translation_key == f"unanswered_wait_{kind}"
    assert set(issue.translation_placeholders) == {"task"}
    await update(hass, coordinator, 7200)
    restarted = C.CodexCliCoordinator(hass, client, "entry")
    await update(hass, restarted, 8000)
    assert len(events) == 1
    client.status.return_value["task_waits"]["items"] = []
    await update(hass, restarted, 8100)
    assert len(events) == 2 and events[-1]["active"] is False
    assert ir.async_get(hass).async_get_issue("codex_cli", issue_id) is None
    await update(hass, restarted, 8200)
    assert len(events) == 2
    assert not client.reply_task.called and not client.continue_task.called


async def test_option_and_each_wait_are_independent(hass):
    coordinator, client = setup(hass, [row(), row("approval", "c" * 32)], minutes=2)
    await update(hass, coordinator, 119)
    assert coordinator._wait_notified == set()
    await update(hass, coordinator, 120)
    assert coordinator._wait_notified == {"a" * 32, "c" * 32}
    client.status.return_value["task_waits"]["items"] = [row("approval", "c" * 32)]
    await update(hass, coordinator, 130)
    assert coordinator._wait_notified == {"c" * 32}
    assert ir.async_get(hass).async_get_issue("codex_cli", f"{coordinator._wait_key}_{'c' * 32}")


async def test_bad_or_unavailable_snapshot_does_not_clear_existing_notice(hass):
    coordinator, client = setup(hass, [row()])
    await update(hass, coordinator, 3601)
    for value in ({"available": False}, {"available": True, "minutes": "60", "items": []},
                  {"available": True, "minutes": 60, "items": [row(), row()]},
                  {"available": True, "minutes": 60, "items": [{**row(), "waiting_since": float("nan")}]},
                  {"available": True, "minutes": 60, "items": [None]}):
        client.status.return_value["task_waits"] = value
        await update(hass, coordinator, 4000)
        assert coordinator._wait_notified == {"a" * 32}
        assert ir.async_get(hass).async_get_issue("codex_cli", f"{coordinator._wait_key}_{'a' * 32}")


async def test_failed_dedup_save_defers_event_and_notice(hass):
    coordinator, _ = setup(hass, [row()])
    events = []

    async def record(event):
        events.append(event.data)

    hass.bus.async_listen("codex_cli_unanswered_wait", record)
    with patch.object(coordinator._wait_store, "async_save", side_effect=OSError("synthetic storage failure")):
        await update(hass, coordinator, 3601)
    assert not events and coordinator._wait_notified == set()
    assert ir.async_get(hass).async_get_issue("codex_cli", f"{coordinator._wait_key}_{'a' * 32}") is None
    await update(hass, coordinator, 3602)
    assert len(events) == 1


async def test_interrupted_clear_survives_a_coordinator_restart(hass):
    coordinator, client = setup(hass, [row()])
    await update(hass, coordinator, 3601)
    issue_id = f"{coordinator._wait_key}_{'a' * 32}"
    assert ir.async_get(hass).async_get_issue("codex_cli", issue_id)
    client.status.return_value["task_waits"]["items"] = []
    delete = ir.async_delete_issue

    def interrupt(hass, domain, identity):
        if identity == issue_id:
            raise OSError("synthetic interruption")
        return delete(hass, domain, identity)

    with patch.object(C.ir, "async_delete_issue", side_effect=interrupt):
        with pytest.raises(OSError, match="synthetic interruption"):
            await update(hass, coordinator, 3700)
    restarted = C.CodexCliCoordinator(hass, client, "entry")
    await update(hass, restarted, 3800)
    assert ir.async_get(hass).async_get_issue("codex_cli", issue_id) is None
    assert restarted._wait_notified == set()


async def test_a_stale_registry_repair_is_recovered_after_the_saved_id_was_lost(hass):
    coordinator, client = setup(hass, [row()])
    await update(hass, coordinator, 1800)
    issue_id = f"{coordinator._wait_key}_{'a' * 32}"
    await coordinator._wait_store.async_save([])
    client.status.return_value["task_waits"]["items"] = []
    restarted = C.CodexCliCoordinator(hass, client, "entry")
    await update(hass, restarted, 1900)
    assert ir.async_get(hass).async_get_issue("codex_cli", issue_id) is None


@pytest.mark.parametrize(("timeout", "late_request"), [(3600, False), (40, False), (40, True)])
async def test_the_notice_precedes_the_unchanged_permission_timeout(hass, timeout, late_request):
    from types import SimpleNamespace
    from test_server import server

    elapsed = [0.0]
    notice = []
    request_age = timeout * .75 if late_request else 0
    stamp = datetime.fromtimestamp(START + request_age, timezone.utc).isoformat()
    task = {"task_id": "b" * 32, "current_turn_id": "turn", "status": "running",
            "started_at": datetime.fromtimestamp(START, timezone.utc).isoformat(),
            "approvals": [{"approval_id": "d" * 32, "status": "pending", "requested_at": stamp}]}
    seconds = server.wait_seconds(60, timeout)

    class Finished:
        def wait(self, timeout):
            elapsed[0] += timeout
            if request_age <= elapsed[0] < limit:
                items = server.task_waits([task], notice_seconds=seconds)
                if items and START + elapsed[0] >= items[0]["notice_at"] and not notice:
                    notice.append((elapsed[0], items))
            return False

    limit = timeout
    session = SimpleNamespace(finished=Finished(), waiting_for_user=lambda: True, last_activity=0)
    assert server.wait_for_exchange(session, timeout, 1, poll=5, clock=lambda: elapsed[0]) == "timeout"
    assert elapsed[0] == timeout
    assert notice and notice[0][0] < timeout
    age, items = notice[0]
    coordinator, client = setup(hass, items)
    client.status.return_value["task_waits"]["seconds"] = seconds
    await update(hass, coordinator, age)
    assert coordinator._wait_notified == {items[0]["wait_id"]}
