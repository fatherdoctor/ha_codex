"""Data coordinator for the Codex CLI integration."""

from __future__ import annotations

import logging
import math
import re
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import CodexCliApiClient, CodexCliApiError, CodexCliAuthError
from .const import DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_NOT_SIGNED_IN = "codex_not_signed_in"
ISSUE_REMOTE_CONTROL_CHANGE = "remote_control_config_change"


class CodexCliCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the local Codex CLI Worker."""

    def __init__(self, hass: HomeAssistant, client: CodexCliApiClient, entry_id: str = "default") -> None:
        """Set up polling of the worker status through the given client, every DEFAULT_SCAN_INTERVAL_SECONDS."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self.client = client
        self._mailbox_key = f"{DOMAIN}_mailbox_seen_{entry_id}"
        self._mailbox_store = Store(hass, 1, self._mailbox_key, atomic_writes=True)
        self._mailbox_seen: set[str] | None = None
        self._wait_key = f"{DOMAIN}_unanswered_{entry_id}"
        self._wait_store = Store(hass, 1, self._wait_key, atomic_writes=True)
        self._wait_notified: set[str] | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the worker status and update the sign-in repair issue.

        Rejected worker authentication raises ConfigEntryAuthFailed; any other failure raises UpdateFailed.
        """
        try:
            data = await self.client.status()
        except CodexCliAuthError as exc:
            raise ConfigEntryAuthFailed("Worker authentication failed") from exc
        except CodexCliApiError as exc:
            raise UpdateFailed(str(exc)) from exc
        except Exception as exc:
            raise UpdateFailed(f"Unexpected worker status error: {exc}") from exc

        await self._async_update_mailbox(data)
        self._update_repair_issue(data)
        self._update_remote_control_issue(data)
        await self._async_update_wait_notices(data)
        return data

    async def _async_update_wait_notices(self, data: dict[str, Any]) -> None:
        """Raise one body-free event per wait and keep its Repair until resolution."""
        snapshot = data.get("task_waits")
        if not isinstance(snapshot, dict) or snapshot.get("available") is not True:
            return
        minutes, rows = snapshot.get("minutes"), snapshot.get("items")
        if (type(minutes) is not int or not 1 <= minutes <= 10080
                or not isinstance(rows, list) or len(rows) > 1000):
            return
        active = {}
        for row in rows:
            if not isinstance(row, dict):
                return
            wait_id, task_id, kind, since = (row.get(k) for k in ("wait_id", "task_id", "kind", "waiting_since"))
            if (not isinstance(wait_id, str) or not re.fullmatch(r"[0-9a-f]{32}", wait_id)
                    or not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id)
                    or kind not in ("question", "approval", "mailbox")
                    or type(since) not in (int, float) or not math.isfinite(since) or since < 0
                    or wait_id in active):
                return  # An incomplete snapshot must not clear notices or deduplication.
            active[wait_id] = {"wait_id": wait_id, "task_id": task_id, "kind": kind, "waiting_since": since}
        if self._wait_notified is None:
            saved = await self._wait_store.async_load()
            self._wait_notified = set(saved) if isinstance(saved, list) and all(
                isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) for v in saved) else set()
        now = dt_util.utcnow().timestamp()
        due = {key for key, row in active.items() if now - row["waiting_since"] > minutes * 60}
        previous = self._wait_notified
        notified = (previous & active.keys()) | due
        if notified != previous:
            try:
                await self._wait_store.async_save(sorted(notified))
                saved = await Store(self.hass, 1, self._wait_key).async_load()
            except (HomeAssistantError, OSError, ValueError):
                return
            if saved != sorted(notified):
                return
            self._wait_notified = notified
        requests = {"question": "Answer the question in the task's chat.",
                    "approval": "Review the permission request in the task's chat.",
                    "mailbox": "Reply to the task's question in the owner mailbox."}
        for wait_id in previous - active.keys():
            ir.async_delete_issue(self.hass, DOMAIN, f"{self._wait_key}_{wait_id}")
            self.hass.bus.async_fire("codex_cli_unanswered_wait", {"wait_id": wait_id, "active": False})
        for wait_id in notified:
            row = active[wait_id]
            ir.async_create_issue(
                self.hass, DOMAIN, f"{self._wait_key}_{wait_id}",
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key="unanswered_wait",
                translation_placeholders={"task": row["task_id"], "request": requests[row["kind"]]},
            )
            if wait_id not in previous:
                self.hass.bus.async_fire("codex_cli_unanswered_wait", {**row, "active": True})

    async def _async_update_mailbox(self, data: dict[str, Any]) -> None:
        """Persist deduplication before emitting body-free arrival events."""
        mailbox = data.get("mailbox")
        if not isinstance(mailbox, dict) or mailbox.get("available") is False:
            return
        rows = mailbox.get("messages")
        if not isinstance(rows, list) or len(rows) > 500:
            return
        events = {}
        for row in rows:
            if not isinstance(row, dict):
                return
            message_id, thread_id = row.get("message_id"), row.get("thread_id")
            author, timestamp = row.get("author"), row.get("time")
            if (not isinstance(message_id, str) or not re.fullmatch(r"[0-9a-f]{32}", message_id)
                    or not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-f]{32}", thread_id)
                    or author not in ("codex", "lead", "owner")
                    or type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0):
                return  # A partial/malformed snapshot must not reset deduplication.
            # Copy only this allowlist, even if a future worker adds body fields.
            events[message_id] = {"message_id": message_id, "thread_id": thread_id,
                                  "author": author, "time": timestamp}
        if self._mailbox_seen is None:
            saved = await self._mailbox_store.async_load()
            self._mailbox_seen = set(saved) if isinstance(saved, list) and all(isinstance(v, str) for v in saved) else set()
        current = set(events)
        fresh = current - self._mailbox_seen
        if current != self._mailbox_seen:
            # HA consumes some write errors inside async_save. Read through a new
            # Store (not the writer's pending data) before treating IDs as saved.
            # Saving invalidates HA's shared preload cache, so this reads the file.
            expected = sorted(current)
            try:
                await self._mailbox_store.async_save(expected)
                saved = await Store(self.hass, 1, self._mailbox_key).async_load()
            except (HomeAssistantError, OSError, ValueError):
                saved = None
            if saved != expected:
                _LOGGER.warning("Mailbox arrival events deferred: saved IDs could not be verified")
                return  # Keep the previous set so the next poll retries the save.
            self._mailbox_seen = current
        for message_id, event in events.items():
            if message_id in fresh:
                self.hass.bus.async_fire("codex_cli_mailbox_message", event)

    def _update_repair_issue(self, data: dict[str, Any]) -> None:
        """Create a repair issue if Codex CLI needs sign-in."""
        login = data.get("codex_login") or {}
        access = data.get("account_access")
        needs_sign_in = access.get("needs_sign_in") if isinstance(access, dict) else not login.get("status_ok")
        if needs_sign_in is not True:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NOT_SIGNED_IN)
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_NOT_SIGNED_IN,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NOT_SIGNED_IN,
        )

    def _update_remote_control_issue(self, data: dict[str, Any]) -> None:
        """Raise a repair while the worker reports config changes made with remote control on, not yet reviewed."""
        remote = data.get("remote_control") or {}
        findings = remote.get("config_findings") or []
        if not findings:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_REMOTE_CONTROL_CHANGE)
            return
        first = findings[0] if isinstance(findings[0], dict) else {}
        where = f"{first.get('file') or 'the config folder'}, line {first.get('line') or '?'}"
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_REMOTE_CONTROL_CHANGE,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_REMOTE_CONTROL_CHANGE,
            translation_placeholders={
                "count": str(remote.get("config_findings_count") or len(findings)),
                "where": where,
                "what": str(first.get("reason") or "a refused action"),
                "text": str(first.get("text") or ""),
            },
        )
