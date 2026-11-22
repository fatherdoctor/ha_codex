"""Characterize the existing tool servers' one-request Unix socket protocol."""
import json
import os
import socket

import pytest
import pytest_socket

from github_access import GitHubService
from ha_call import HomeAssistantCall
from ha_data import HomeAssistantData
from relay_reads import RelayReads
from schedule_api import Service


SERVERS = [
    (HomeAssistantData, 16384, {"status": "unavailable", "message": "Invalid or expired data request"}),
    (HomeAssistantCall, 65536, {"status": "unavailable", "message": "Invalid call request"}),
    (RelayReads, 16384, {"ok": False, "error": "Invalid relay read request JSON."}),
    (GitHubService, 65536, {"ok": False, "text": "The request is not valid JSON."}),
    (Service, 65536, {"ok": False, "error": "The request is not valid JSON."}),
]


@pytest.fixture(params=SERVERS, ids=lambda item: item[0].__name__)
def endpoint(request, tmp_path):
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    cls, limit, malformed = request.param
    engine = object.__new__(cls)
    seen = []

    def dispatch(payload):
        seen.append(payload)
        return {"received": payload}

    engine.dispatch = dispatch
    path = tmp_path / "tool.sock"
    server = engine.serve(path)
    try:
        yield path, limit, malformed, seen
    finally:
        server.shutdown()
        server.server_close()


def exchange(path, *parts):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path))
        for part in parts:
            client.sendall(part)
        client.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def test_one_json_line_per_connection_and_socket_mode(endpoint):
    path, _, _, seen = endpoint
    assert os.stat(path).st_mode & 0o777 == 0o600
    response = exchange(path, b'{"operation":', b'"first"}\n{"operation":"second"}\n')
    assert response == b'{"received": {"operation": "first"}}\n'
    assert seen == [{"operation": "first"}]


def test_malformed_and_empty_requests_have_framed_errors(endpoint):
    path, _, malformed, seen = endpoint
    for raw in (b'not json\n', b''):
        response = exchange(path, raw)
        assert response.endswith(b'\n')
        assert json.loads(response) == malformed
    assert seen == []


def test_oversized_request_is_rejected_without_dispatch(endpoint):
    path, limit, _, seen = endpoint
    response = json.loads(exchange(path, b' ' * (limit + 1)))
    assert response.get("ok") is False or response.get("status") == "unavailable"
    assert seen == []
