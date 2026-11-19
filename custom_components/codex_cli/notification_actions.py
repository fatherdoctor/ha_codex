"""Answer a waiting Codex question from a button on a mobile app notification."""

from __future__ import annotations

from collections.abc import Callable
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback

from .api import CodexCliApiClient, CodexCliApiError

if TYPE_CHECKING:
    from .coordinator import CodexCliCoordinator

_LOGGER = logging.getLogger(__name__)

# The companion apps fire this when a notification button is tapped.
EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"
# Written by the worker: the choice's position, the turn that asked, and the task. Keep the two in step.
CHOICE_ACTION_RE = re.compile(
    r"CODEX_CLI_CHOICE_(\d{1,2})_([0-9a-f]{32})_([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
)


def parse_choice_action(action: Any) -> tuple[str, str, int] | None:
    """Return the task ID, turn ID and choice position a Codex notification button names, or None for any other action."""
    match = CHOICE_ACTION_RE.fullmatch(action) if isinstance(action, str) else None
    if match is None:
        return None
    return match[3], match[2], int(match[1])


@callback
def async_listen_for_choices(
    hass: HomeAssistant, client: CodexCliApiClient, coordinator: CodexCliCoordinator
) -> Callable[[], None]:
    """Send the choice tapped on a Codex notification to the worker; return the function that stops listening."""

    async def handle_action(event: Event) -> None:
        """Reply with the tapped choice; when the worker does not take it, log why.

        A tap that is not taken makes no notification: Home Assistant shows a
        Codex notification only while a chat waits on the owner, and the worker
        keeps that one up until the chat no longer waits.
        """
        parsed = parse_choice_action(event.data.get("action"))
        if parsed is None:
            return
        task_id, turn_id, choice = parsed
        # Only an administrator's tap answers Codex; a tap with no user is not taken either.
        user = (
            await hass.auth.async_get_user(event.context.user_id)
            if event.context.user_id
            else None
        )
        if user is None or not user.is_active or not user.is_admin:
            _LOGGER.warning(
                "Codex did not take an answer for task %s: only a Home Assistant administrator can answer Codex",
                task_id,
            )
            return
        try:
            await client.reply_choice(task_id, turn_id, choice)
        except CodexCliApiError as exc:
            _LOGGER.warning(
                "Codex did not take the answer picked for task %s: %s", task_id, exc
            )
            return
        await coordinator.async_request_refresh()

    return hass.bus.async_listen(EVENT_NOTIFICATION_ACTION, handle_action)
