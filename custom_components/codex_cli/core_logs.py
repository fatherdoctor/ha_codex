"""Let the paired worker read bounded Core logs, never other apps' logs."""
from __future__ import annotations

from collections.abc import Iterator
import hmac
import json
import os
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN


def loaded_runtime_data(hass: HomeAssistant) -> Iterator[Any]:
    """Yield the runtime data of each loaded Codex config entry, in entry order: the one place that picks an entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            yield getattr(entry, "runtime_data", None)


class DiagnosticView(HomeAssistantView):
    """Allow the paired worker to read bounded Core logs, never other apps' logs.

    A caller needs both Home Assistant administrator authentication and the paired worker's token.
    """

    url = "/api/codex_cli/diagnostic_logs"
    name = "api:codex_cli:diagnostic_logs"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep the Home Assistant instance whose loaded Codex entry holds the worker token."""
        self.hass = hass

    def check_caller(self, request: web.Request, data: dict[str, Any]) -> None:
        """Accept an administrator whose request body carries the paired worker token.

        Raises HTTPServiceUnavailable when no Codex entry is loaded, and HTTPForbidden for any other caller.
        """
        tokens = [getattr(runtime, "worker_token", None) for runtime in loaded_runtime_data(self.hass)]
        worker_token = next((token for token in tokens if isinstance(token, str) and token), None)
        if worker_token is None:
            raise web.HTTPServiceUnavailable
        supplied = data.get("worker_token", "")
        if not isinstance(supplied, str) or not request[KEY_HASS_USER].is_admin or not hmac.compare_digest(
            supplied.encode(), worker_token.encode()
        ):
            raise web.HTTPForbidden

    async def post(self, request: web.Request) -> web.Response:
        """Return the last 100 Core log lines from Supervisor, cut off at 32768 bytes, to the paired worker.

        Only the "core" target is accepted. A Supervisor answer other than HTTP 200 is reported as HTTP 502.
        """
        data = await self.payload(request)
        self.check_caller(request, data)
        target = data.get("target") if isinstance(data, dict) else None
        if target != "core":
            raise web.HTTPBadRequest
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise web.HTTPServiceUnavailable
        async with async_get_clientsession(self.hass).get(
            "http://supervisor/core/logs", params={"lines": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15, allow_redirects=False,
        ) as response:
            if response.status != 200:
                return self.json({"error": f"Log read returned HTTP {response.status}"}, status_code=502)
            content = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                content.extend(chunk)
                if len(content) > 32768:
                    break
        # Final redaction is applied by the worker before persistence or model access.
        return self.json({"text": bytes(content[:32768]).decode("utf-8", "replace"),
                          "truncated": len(content) > 32768}, headers={"Cache-Control": "no-store"})

    async def payload(self, request: web.Request) -> dict[str, Any]:
        """Read the request body as a JSON object, refusing bodies over 4096 bytes or anything that is not an object."""
        # The Supervisor proxy only forwards selected headers, so pairing travels
        # in the authenticated JSON body, never a URL/query string.
        if request.content_length is not None and request.content_length > 4096:
            raise web.HTTPBadRequest
        content = bytearray()
        async for chunk in request.content.iter_chunked(1024):
            content.extend(chunk)
            if len(content) > 4096:
                raise web.HTTPBadRequest
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError) as exc:
            raise web.HTTPBadRequest from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest
        return data


