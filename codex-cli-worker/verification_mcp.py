"""Stdio MCP bridge for task-scoped checks outside the shell network sandbox."""
from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

import tool_threads

TOOL: dict[str, Any] = {
    "name": "verify",
    "description": (
        "Find entities by name, or check Home Assistant configuration, fresh entity state, Core logs, or "
        "dashboard configuration. For a device command, use find with the name the user gave (and a domain "
        "such as light when it is clear) to get the entity_id; do not search files for it. "
        "dashboard_readback reads a dashboard's saved configuration back from Home Assistant; with "
        "save_pending it first saves a dashboard edited in this turn, when auto-save is enabled. "
        "No service calls, reloads, arbitrary URLs, or other apps' logs."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["entity", "find", "config_check", "logs", "dashboard_readback"]},
            "entity_id": {"type": "string"}, "expected_state": {"type": "string"},
            "name": {"type": "string", "maxLength": 120,
                     "description": "For find: the words the user used for the device, for example office overhead"},
            "domain": {"type": "string", "maxLength": 64, "description": "For find: optional domain, for example light"},
            "attributes": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "path": {"type": "string", "description": "Local dashboard path, for example /lovelace/lights"},
            "save_pending": {"type": "boolean"},
        },
        "required": ["operation"],
    },
}


def verify(arguments: Any, thread_id: str) -> dict[str, Any]:
    """Forward only the supported fields and the chat's thread id that Codex sent with the call."""
    if not isinstance(arguments, dict) or set(arguments) - set(TOOL["inputSchema"]["properties"]):
        raise ValueError("Unsupported verification arguments")
    if arguments.get("operation") not in TOOL["inputSchema"]["properties"]["operation"]["enum"]:
        raise ValueError("Unsupported verification operation")
    payload = {**arguments, "thread_id": thread_id}
    encoded = json.dumps(payload).encode() + b"\n"
    if len(encoded) > 16384:
        raise ValueError("Verification request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(230)
        client.connect(os.environ.get("HA_VERIFICATION_SOCKET", "/data/verification.sock"))
        client.sendall(encoded)
        with client.makefile("rb") as response:
            raw = response.readline(128 * 1024 + 1)
    if not raw or len(raw) > 128 * 1024:
        raise ValueError("Invalid verification response")
    return json.loads(raw)


def respond(request: dict[str, Any]) -> dict[str, Any]:
    """Implement the MCP initialization, discovery and single-tool request surface."""
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "home-assistant-verification", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            if params.get("name") != TOOL["name"]:
                raise ValueError("Unknown verification tool")
            result = verify(params.get("arguments", {}), tool_threads.thread_id_from_call(params))
        except (OSError, ValueError, TypeError):
            result = {"status": "unavailable", "message": "Verification request failed or the task has ended"}
        return {"content": [{"type": "text", "text": json.dumps(result)}],
                "isError": result.get("status") == "unavailable"}
    raise ValueError("Method not found")


def main() -> int:
    """Read bounded newline-delimited JSON-RPC; stdout contains protocol data only."""
    while raw := sys.stdin.buffer.readline(65537):
        if len(raw) > 65536:
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
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
