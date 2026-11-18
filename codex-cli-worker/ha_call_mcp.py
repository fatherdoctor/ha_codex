"""Stdio MCP bridge for the home_assistant_call tool: one Home Assistant action per call.

The tool is registered without an approval mode, so Codex asks the person before each call.
This bridge only forwards the request and the turn's call key (HA_CALL_CAPABILITY) to the
worker's socket. The worker holds the refusals and writes the audit line (ha_call.py).
"""
from __future__ import annotations

import json
import os
import socket
import sys

TOOL = {
    "name": "call",
    "title": "Home Assistant action",
    "description": (
        "Send one Home Assistant action (a service call) that changes the house, for example light.turn_off, "
        "automation.reload or homeassistant.restart. Codex asks the person before each call. "
        "Put entity_id, device_id, area_id, floor_id or label_id in target, and the other fields in data. "
        "For a device the person named, give name (their words) in place of target: the action is sent only "
        "when exactly one entity of the service's domain fits, else the answer lists the candidates. "
        "The answer reads back the new state of each target entity. hassio and codex_cli services are refused."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "service": {"type": "string", "maxLength": 200,
                        "description": "domain.service, for example light.turn_on"},
            "target": {"type": "object", "additionalProperties": False,
                       "properties": {key: {"anyOf": [{"type": "string"},
                                                      {"type": "array", "items": {"type": "string"}}]}
                                      for key in ("entity_id", "device_id", "area_id", "floor_id", "label_id")}},
            "data": {"type": "object", "description": "The service's other fields, for example brightness_pct"},
            "name": {"type": "string", "maxLength": 120,
                     "description": "The device's name as the person said it, for example kitchen ceiling; "
                                    "in place of target"},
        },
        "required": ["service"],
    },
    # A tool that changes things: Codex treats it as one that needs the person's answer.
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
}
MAX_REQUEST_BYTES = 65536


def call(arguments):
    """Forward the arguments and the turn's call key to the worker, once, and return its answer."""
    encoded = json.dumps({"capability": os.environ.get("HA_CALL_CAPABILITY", ""),
                          "arguments": arguments}).encode() + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("Call request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(80)
        client.connect(os.environ.get("HA_CALL_SOCKET", "/data/ha-call.sock"))
        client.sendall(encoded)
        with client.makefile("rb") as response:
            raw = response.readline(64 * 1024 + 1)
    if not raw or len(raw) > 64 * 1024:
        raise ValueError("Invalid call response")
    return json.loads(raw)


def respond(request):
    """Implement the MCP initialization, discovery and single-tool request surface."""
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "home-assistant-call", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            if params.get("name") != TOOL["name"]:
                raise ValueError("Unknown tool")
            result = call(params.get("arguments", {}))
        except (OSError, ValueError, TypeError):
            # A timeout is an OSError: the action may have reached Home Assistant, so read the state back.
            result = {"status": "unavailable",
                      "message": "No answer from the worker; read the state back before you try again"}
        return {"content": [{"type": "text", "text": json.dumps(result)}],
                "isError": result.get("status") != "ok"}
    raise ValueError("Method not found")


def main():
    """Read bounded newline-delimited JSON-RPC; stdout contains protocol data only."""
    while raw := sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1):
        if len(raw) > MAX_REQUEST_BYTES:
            return 1
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            try:
                response["result"] = respond(request)
            except (ValueError, TypeError, AttributeError):
                response["error"] = {"code": -32601, "message": "Unsupported request"}
            print(json.dumps(response), flush=True)
        except (ValueError, UnicodeError):
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}),
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
