"""Data coordinator for the Codex CLI integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CodexCliApiClient, CodexCliApiError, CodexCliAuthError
from .const import (
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    EVENT_GITHUB_ACCESS,
    GITHUB_ERROR_STATES,
    GITHUB_WARNING_STATES,
    ISSUE_GITHUB_ACCESS_LOST,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_NOT_SIGNED_IN = "codex_not_signed_in"
ISSUE_REMOTE_CONTROL_CHANGE = "remote_control_config_change"
# Every repair issue the coordinator raises. They are not tied to the config entry, so unload and removal delete
# them: an owner who removes the integration must not keep a "sign in" repair.
REPAIR_ISSUES = (
    ISSUE_NOT_SIGNED_IN,
    ISSUE_REMOTE_CONTROL_CHANGE,
    ISSUE_GITHUB_ACCESS_LOST,
)


def async_delete_repair_issues(hass: HomeAssistant) -> None:
    """Delete every repair issue this integration raises."""
    for issue_id in REPAIR_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


GITHUB_STATE_WORDS = {
    "near_expiry": "expires within 7 days",
    "expired": "expired",
    "revoked": "refused by GitHub (revoked)",
    "access_changed": "reaches different repositories or access",
    "failed": "cannot be checked",
}


def github_repair_key(state: str, kind: str) -> str:
    """Return the Repair's translation key: what still works and the next step differ by state and sign-in kind.

    Sign in on the page starts a GitHub App sign-in, so a token connection is sent to the token form.
    """
    if state == "near_expiry":
        return (
            "github_access_near_expiry_token"
            if kind == "token"
            else "github_access_near_expiry"
        )
    if state in ("expired", "revoked"):
        return (
            "github_access_lost_token" if kind == "token" else ISSUE_GITHUB_ACCESS_LOST
        )
    if state == "access_changed":
        return "github_access_changed"
    return "github_access_check_failed"


def _expiry_date(value: Any) -> str:
    """Return "on <date>" for the sign-in's expiry, or "within 7 days" when the worker gave no readable date."""
    try:
        return f"on {datetime.fromisoformat(str(value)).date().isoformat()}"
    except ValueError:
        return "within 7 days"


class CodexCliCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the local Codex CLI Worker."""

    def __init__(self, hass: HomeAssistant, client: CodexCliApiClient) -> None:
        """Set up polling of the worker status through the given client, every DEFAULT_SCAN_INTERVAL_SECONDS."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self.client = client
        self._github_state: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the worker status and update the sign-in repair issue.

        Rejected worker authentication raises ConfigEntryAuthFailed and any other worker error raises UpdateFailed;
        the base class handles an unexpected one.
        """
        try:
            data = await self.client.status()
        except CodexCliAuthError as exc:
            raise ConfigEntryAuthFailed("Worker authentication failed") from exc
        except CodexCliApiError as exc:
            raise UpdateFailed(str(exc)) from exc

        self._update_repair_issue(data)
        self._update_remote_control_issue(data)
        self._update_github_issue(data)
        return data

    def _update_repair_issue(self, data: dict[str, Any]) -> None:
        """Create a repair issue if Codex CLI needs sign-in."""
        login = data.get("codex_login") or {}
        access = data.get("account_access")
        needs_sign_in = (
            access.get("needs_sign_in")
            if isinstance(access, dict)
            else not login.get("status_ok")
        )
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

    def _update_github_issue(self, data: dict[str, Any]) -> None:
        """Raise a repair while the add-on's GitHub sign-in is lost or changed, and fire one event per change.

        A worker without the GitHub sign-in (no "github" key) changes nothing. An owner who never connected gets
        no repair. Only names and states are copied; the worker never reports a token or a device code here.
        """
        github = data.get("github")
        if not isinstance(github, dict):
            return
        state = github.get("state")
        if not isinstance(state, str) or not state:
            return
        previous, self._github_state = self._github_state, state
        account = github.get("account")
        login = str(account.get("login") or "") if isinstance(account, dict) else ""
        last = github.get("last_check")
        reason = str(last.get("reason") or "") if isinstance(last, dict) else ""
        if previous is not None and previous != state:
            # The worker fires the same event through the Core API; this copy, marked by its source, still
            # reaches house automations when that call failed. Names only, never a token.
            self.hass.bus.async_fire(
                EVENT_GITHUB_ACCESS,
                {
                    "state": state,
                    "previous_state": previous,
                    "reason": reason,
                    "account": login,
                    "missing": [
                        str(item)
                        for item in github.get("missing") or []
                        if isinstance(item, str)
                    ][:50],
                    "extra": [
                        str(item)
                        for item in github.get("extra") or []
                        if isinstance(item, str)
                    ][:50],
                    "permission_problem": github.get("permission_problem")
                    if isinstance(github.get("permission_problem"), str)
                    else None,
                    "expires_at": github.get("expires_at")
                    if isinstance(github.get("expires_at"), str)
                    else None,
                    "addon": "codex_cli_worker",
                    "source": "integration",
                },
            )
        if state in GITHUB_ERROR_STATES:
            severity = ir.IssueSeverity.ERROR
        elif state in GITHUB_WARNING_STATES:
            severity = ir.IssueSeverity.WARNING
        else:
            # ok, disconnected, connecting: nothing to repair.
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_GITHUB_ACCESS_LOST)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_GITHUB_ACCESS_LOST,
            is_fixable=False,
            severity=severity,
            translation_key=github_repair_key(state, str(github.get("kind") or "")),
            translation_placeholders={
                "state": GITHUB_STATE_WORDS.get(state, state),
                "reason": reason or "no reason given",
                "account": login or "the connected account",
                "expires": _expiry_date(github.get("expires_at")),
            },
        )
