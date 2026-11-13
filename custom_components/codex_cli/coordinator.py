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
        return data

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
        if login.get("status_ok"):
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
