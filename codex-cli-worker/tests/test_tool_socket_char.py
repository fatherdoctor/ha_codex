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


def _read_exact(conn, count):
    chunks = []
    while count:
        chunk = conn.recv(count)
        if not chunk:
            raise EOFError("WebSocket closed during a frame")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _read_ws_frame(conn):
    first, second = _read_exact(conn, 2)
    size = second & 127
    if size == 126:
        size = int.from_bytes(_read_exact(conn, 2), "big")
    elif size == 127:
        size = int.from_bytes(_read_exact(conn, 8), "big")
    mask = _read_exact(conn, 4) if second & 128 else b""
    data = _read_exact(conn, size)
    if mask:
        data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
    return first & 15, data


def _send_ws_json(conn, value):
    payload = json.dumps(value).encode()
    assert len(payload) < 126
    conn.sendall(bytes((0x81, len(payload))) + payload)


def _fake_control_server(listener, outcome, result_queue):
    """Speak just enough WebSocket to observe one control request and close."""
    import base64
    import hashlib

    try:
        listener.settimeout(4)
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(4)
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                header += _read_exact(conn, 1)
            fields = {}
            for raw in header.decode("ascii").split("\r\n")[1:]:
                if ":" in raw:
                    key, value = raw.split(":", 1)
                    fields[key.lower()] = value.strip()
            key = fields["sec-websocket-key"]
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            messages = []
            for index in range(3):
                opcode, frame = _read_ws_frame(conn)
                assert opcode == 1
                request = json.loads(frame)
                messages.append(request)
                if index == 0:
                    _send_ws_json(conn, {"id": request["id"], "result": {}})
                elif index == 2:
                    reply = ({"id": request["id"], "result": {"enabled": True}} if outcome == "ok"
                             else {"id": request["id"], "error": {"code": -1}})
                    _send_ws_json(conn, reply)
            # close() may send a close frame before shutting down the socket.
            try:
                opcode, _ = _read_ws_frame(conn)
                closed = opcode == 8
            except EOFError:
                closed = True
            result_queue.put((messages, closed))
    except Exception as exc:
        result_queue.put(exc)


@pytest.mark.parametrize("outcome", ["ok", "error"])
def test_control_client_websocket_request_error_and_close(tmp_path, outcome):
    import queue
    import threading
    from remote_control import ControlClient, ControlError

    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    path = tmp_path / "control.sock"
    results = queue.Queue()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)
        thread = threading.Thread(target=_fake_control_server, args=(listener, outcome, results), daemon=True)
        thread.start()
        try:
            client = ControlClient(path)
            if outcome == "ok":
                assert client.enable() == {"enabled": True}
            else:
                with pytest.raises(ControlError, match="refused or is unsupported"):
                    client.enable()
            observed = results.get(timeout=6)
        finally:
            thread.join(timeout=5)
    assert not thread.is_alive()
    if isinstance(observed, Exception):
        raise observed
    messages, closed = observed
    assert messages[0]["id"] == 1
    assert messages[0]["method"] == "initialize"
    assert messages[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert messages[1] == {"method": "initialized"}
    assert messages[2] == {"id": 2, "method": "remoteControl/enable", "params": {"ephemeral": True}}
    assert closed


def test_control_client_reports_a_refused_unix_connection(tmp_path):
    from remote_control import ControlClient, ControlError

    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    path = tmp_path / "refused.sock"
    # A bound socket without listen() refuses the connection deterministically.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        with pytest.raises(ControlError, match="Could not reach"):
            ControlClient(path).enable()
