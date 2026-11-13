"""Body-free mailbox events and persistent deduplication in a real HA test instance."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import importlib

from test_integration_polling import integration


def message(number, parent=None, author="codex"):
    return {"message_id": f"{number:032x}", "thread_id": f"{parent or number:032x}",
            "author": author, "time": 1.0, "body": "UNTRUSTED_BODY_MUST_NOT_ESCAPE"}


async def test_mail_events_fire_once_per_message_and_reply_across_reload(hass):
    events = []
    hass.bus.async_listen("codex_cli_mailbox_message", lambda event: events.append(event.data))
    client = AsyncMock()
    client.status.return_value = {"codex_login": {"status_ok": True},
                                  "mailbox": {"unread": {"lead": 1, "owner": 1}, "messages": [message(1)]}}
    coordinator = integration.CodexCliCoordinator(hass, client, "synthetic-mail")
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1
    assert set(events[0]) == {"message_id", "thread_id", "author", "time"}
    assert "UNTRUSTED" not in str(events)
    # A new coordinator uses the saved ID set rather than replaying old messages.
    restored = integration.CodexCliCoordinator(hass, client, "synthetic-mail")
    await restored._async_update_data()
    valid = client.status.return_value
    client.status.return_value = {"mailbox": {"messages": [{**message(1), "author": "invalid"}]}}
    await restored._async_update_data()
    client.status.return_value = valid
    client.status.return_value["mailbox"]["messages"].append(message(2, 1, "owner"))
    await restored._async_update_data()
    await restored._async_update_data()
    await hass.async_block_till_done()
    assert [event["message_id"] for event in events] == [f"{1:032x}", f"{2:032x}"]
    assert events[1]["thread_id"] == events[0]["message_id"]


async def test_missing_or_invalid_metadata_never_fires_an_event(hass):
    events = []
    hass.bus.async_listen("codex_cli_mailbox_message", lambda event: events.append(event.data))
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client, "synthetic-invalid")
    for mailbox in ({"available": False}, {"messages": []},
                    {"messages": [{**message(1), "author": "untrusted author"}]},
                    {"messages": [{**message(1), "time": float("nan")}]},
                    {"messages": [{**message(1), "message_id": "malformed"}]}):
        client.status.return_value = {"codex_login": {"status_ok": True}, "mailbox": mailbox}
        await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []


def test_unread_sensor_uses_only_the_lead_count_and_preserves_unknown():
    sensors = importlib.import_module(integration.__name__ + ".sensor")
    coordinator = SimpleNamespace(data={"mailbox": {"unread": {"lead": 3, "owner": 9}}})
    entry = SimpleNamespace(entry_id="synthetic", data={})
    sensor = sensors.CodexMailboxUnreadSensor(coordinator, entry)
    assert sensor.native_value == 3
    coordinator.data = {"mailbox": {"available": False, "unread": {"lead": None}}}
    assert sensor.native_value is None
