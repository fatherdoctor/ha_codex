"""Stdio MCP bridge for the home_assistant_call tools: call, one Home Assistant action per call, and find.

call is registered with the approval mode "prompt", so Codex asks the person before each call. find only reads
(ha_find.py): it searches the entities by name, area or domain and is approved ahead, as the other read tools are.
This bridge only forwards the request to the worker's socket, with the chat's thread id that Codex sent
with the call. Under remote control it sends the relay's key (HA_CALL_CAPABILITY) in its place. The worker
checks the chat or the key, and the form of the request (ha_call.py).
"""
from __future__ import annotations

import json
import os
import socket
import sys

import tool_threads

TOOL = {
    "name": "call",
    "title": "Home Assistant action",
    "description": (
        "Send one Home Assistant action (a service call) that changes the house, for example light.turn_off, "
        "automation.reload or homeassistant.restart. Codex asks the person before each call. "
        "Put entity_id, device_id, area_id, floor_id or label_id in target, and the other fields in data. "
        "Prefer target.entity_id with the exact entity id, from the find tool when you do not have it. "
        "As a fallback, give name (the person's words) in place of target: the action is sent only "
        "when exactly one entity of the service's domain fits, else the answer lists the candidates. "
        "When target is only entity ids, the answer reads back the new state of the first 5 of them; "
        "for other targets, or more entities, read the state with a read tool."
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
FIND_TOOL = {
    "name": "find",
    "title": "Find Home Assistant devices",
    "description": (
        "Search the house's entities by the words a person uses, by area or by domain, and get their exact "
        "entity_id, friendly name, area and current state (at most 25, the closest first). Read-only: it changes "
        "nothing and needs no approval. Use it before an action to get the entity_id for the call tool's "
        "target.entity_id. query is loose: 'the overhead lamp in the media room' works, and 'lamp' or 'light' "
        "fits every light. area or domain alone lists that area's or domain's entities."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "maxLength": 120,
                      "description": "The person's words for the device, for example media room overhead"},
            "area": {"type": "string", "maxLength": 120, "description": "An area name or area id, for example kitchen"},
            "domain": {"type": "string", "maxLength": 64, "description": "A domain, for example light"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "At most this many rows (25)"},
        },
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
}
TOOLS = {TOOL["name"]: TOOL, FIND_TOOL["name"]: FIND_TOOL}
MAX_REQUEST_BYTES = 65536


def call(arguments, thread_id, tool="call"):
    """Forward the arguments, and the chat's thread id or the relay's key, to the worker once; return its answer."""
    relay_key = os.environ.get("HA_CALL_CAPABILITY")
    origin = {"capability": relay_key} if relay_key else {"thread_id": thread_id}
    encoded = json.dumps({**origin, "tool": tool, "arguments": arguments}).encode() + b"\n"
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
        return {"tools": list(TOOLS.values())}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            tool = params.get("name")
            if tool not in TOOLS:
                raise ValueError("Unknown tool")
            result = call(params.get("arguments", {}), tool_threads.thread_id_from_call(params), tool)
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
