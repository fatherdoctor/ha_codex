"""Characterize the current MCP bridges over their newline-delimited stdio."""

import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

BRIDGES = (
    "github_mcp",
    "ha_call_mcp",
    "ha_data_mcp",
    "relay_reads_mcp",
    "scheduling_mcp",
    "verification_mcp",
)
WORKER = Path(__file__).resolve().parents[1]
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))


def line(value):
    return json.dumps(value).encode() + b"\n"


def run_bridge(name, data):
    return subprocess.run(
        [sys.executable, "-B", str(WORKER / (name + ".py"))],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("name", BRIDGES)
def test_requests_notifications_errors_and_eof(name):
    data = b"".join(
        (
            line({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            line({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            line({"jsonrpc": "2.0", "id": "ping-id", "method": "ping"}),
            line({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
            b"not-json\n",
            b"\xff\n",
            line({"jsonrpc": "2.0", "id": 4, "method": "unknown"}),
        )
    )
    proc = run_bridge(name, data)
    assert proc.returncode == 0
    assert proc.stderr == b""
    assert proc.stdout.endswith(b"\n")
    responses = [json.loads(raw) for raw in proc.stdout.splitlines()]
    assert [response["id"] for response in responses] == [
        1,
        "ping-id",
        3,
        None,
        None,
        4,
    ]
    assert all(response["jsonrpc"] == "2.0" for response in responses)
    assert responses[0]["result"]["protocolVersion"] == "2024-11-05"
    assert responses[0]["result"]["serverInfo"]["name"]
    assert responses[1]["result"] == {}
    assert responses[2]["result"]["tools"]
    assert responses[3]["error"]["code"] == -32700
    assert responses[4]["error"]["code"] == -32700
    assert responses[5]["error"]["code"] == -32601
    empty = run_bridge(name, b"")
    assert (empty.returncode, empty.stdout, empty.stderr) == (0, b"", b"")


@pytest.mark.parametrize("name", BRIDGES)
def test_oversized_input_ends_stream_without_a_response(name):
    proc = run_bridge(name, b" " * 65537 + b"\n" + line({"id": 1, "method": "ping"}))
    assert (proc.returncode, proc.stdout, proc.stderr) == (1, b"", b"")


@pytest.mark.parametrize("name", BRIDGES)
def test_incomplete_last_line_is_processed_before_eof(name):
    proc = run_bridge(name, b'{"id":7,"method":"ping"}')
    assert proc.returncode == 0
    assert proc.stderr == b""
    assert json.loads(proc.stdout) == {"jsonrpc": "2.0", "id": 7, "result": {}}


@pytest.mark.parametrize("name", BRIDGES)
def test_tool_call_returns_a_framed_error_for_unknown_tool(name):
    # Each bridge rejects the unknown name before it can reach a backend.
    module = importlib.import_module(name)
    params = {"name": "unknown-tool", "arguments": {}}
    result = module.respond({"method": "tools/call", "params": params})
    assert result["isError"] is True
    assert result["content"][0]["type"] == "text"
    assert isinstance(result["content"][0]["text"], str)


@pytest.mark.parametrize("name", BRIDGES)
def test_tool_call_forwards_arguments_and_reports_transport_failure(name, monkeypatch):
    module = importlib.import_module(name)
    adapter = {
        "github_mcp": "fetch",
        "ha_call_mcp": "call",
        "ha_data_mcp": "fetch",
        "relay_reads_mcp": "read",
        "scheduling_mcp": "fetch",
        "verification_mcp": "verify",
    }[name]
    available = module.respond({"method": "tools/list"})["tools"]
    tool_name = available[0]["name"]
    calls = []

    def success(*args):
        calls.append(args)
        return {"ok": True, "status": "ok", "text": "sample"}

    monkeypatch.setattr(module, adapter, success)
    request = {
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"sample": 1},
            "_meta": {"threadId": "thread-1"},
        },
    }
    result = module.respond(request)
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert {"sample": 1} in calls[0]
    if name != "relay_reads_mcp":
        assert calls[0][-1] == "thread-1"

    def unavailable(*args):
        raise OSError("disconnected")

    monkeypatch.setattr(module, adapter, unavailable)
    result = module.respond(request)
    assert result["isError"] is True
    assert result["content"][0]["type"] == "text"
    assert "disconnected" not in result["content"][0]["text"]
