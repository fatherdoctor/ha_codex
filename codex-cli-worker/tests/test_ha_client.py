"""The worker's one Home Assistant client: the strict WebSocket login, the limits, and one REST attempt per call."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

from test_server import server  # puts the worker folder on the import path
import ha_client

TOKEN = "synthetic-supervisor"


def fake_socket(*replies):
    """Return a stand-in with websocket-client's interface that answers recv() with each reply in turn."""
    connection = Mock(spec=["recv", "send", "close", "settimeout"])
    connection.recv.side_effect = [reply if isinstance(reply, str) else json.dumps(reply) for reply in replies]
    return connection


LOGIN = ({"type": "auth_required"}, {"type": "auth_ok"})


def sent(connection):
    """The frames the client sent, decoded."""
    return [json.loads(call.args[0]) for call in connection.send.call_args_list]


class WebSocketTests(unittest.TestCase):
    """ha_client.ws: one login handshake for every caller."""

    def run_ws(self, connection, messages, limits=ha_client.WsLimits()):
        """Run ws() against the stand-in connection and return the connect mock and the results."""
        with patch.object(ha_client.websocket, "create_connection", return_value=connection) as connect:
            return connect, ha_client.ws(TOKEN, messages, limits)

    def test_logs_in_then_returns_each_result_in_order(self):
        """Send the token after auth_required, number the messages, skip other frames, and close the connection."""
        connection = fake_socket(*LOGIN, {"id": 7, "type": "result", "success": True},
                                 {"id": 1, "type": "event"}, {"id": 1, "type": "result", "success": True, "result": "a"},
                                 {"id": 2, "type": "result", "success": True, "result": ["b"]})
        connect, results = self.run_ws(connection, [{"type": "one"}, {"type": "two", "x": 1}],
                                       ha_client.WsLimits(timeout=20))
        self.assertEqual(results, ["a", ["b"]])
        connect.assert_called_once_with("ws://supervisor/core/websocket", timeout=20)
        self.assertEqual(sent(connection), [{"type": "auth", "access_token": TOKEN},
                                            {"type": "one", "id": 1}, {"type": "two", "x": 1, "id": 2}])
        connection.close.assert_called_once()

    def test_login_is_strict(self):
        """A direct auth_ok, or a refusal after auth_required, is an auth error and sends no command."""
        for replies in ([{"type": "auth_ok"}], [{"type": "auth_required"}, {"type": "auth_invalid", "message": "no"}]):
            with self.subTest(replies=replies):
                connection = fake_socket(*replies)
                with self.assertRaises(ha_client.WsError) as caught:
                    self.run_ws(connection, [{"type": "one"}])
                self.assertEqual(caught.exception.kind, "auth")
                self.assertEqual(caught.exception.reply, replies[-1])
                self.assertNotIn("one", [frame.get("type") for frame in sent(connection)])
                connection.close.assert_called_once()

    def test_a_failed_command_stops_the_session(self):
        """The first unsuccessful reply ends the session before the next message goes out."""
        failure = {"id": 1, "type": "result", "success": False, "error": {"message": "refused"}}
        connection = fake_socket(*LOGIN, failure)
        with self.assertRaises(ha_client.WsError) as caught:
            self.run_ws(connection, [{"type": "one"}, {"type": "two"}])
        self.assertEqual((caught.exception.kind, caught.exception.reply), ("failed", failure))
        self.assertEqual(len(sent(connection)), 2)

    def test_limits(self):
        """Frame count, frame size in UTF-8 bytes, and a frame that is not an object each end the session."""
        cases = (
            ([*LOGIN, *[{"id": 9, "type": "result"}] * 3], ha_client.WsLimits(max_frames=3), "no_result"),
            (['{"type":"auth_required","pad":"' + "א" * 60 + '"}'], ha_client.WsLimits(max_frame_bytes=100), "too_large"),
            ([*LOGIN, "[1]"], ha_client.WsLimits(), "invalid"),
        )
        for replies, limits, kind in cases:
            with self.subTest(kind=kind):
                with self.assertRaises(ha_client.WsError) as caught:
                    self.run_ws(fake_socket(*replies), [{"type": "one"}], limits)
                self.assertEqual(caught.exception.kind, kind)

    def test_deadline_bounds_the_whole_session(self):
        """Past the deadline no further frame is read; before it, each frame waits at most the time left."""
        connection = fake_socket(*LOGIN)
        clock = iter([100.0, 100.0, 130.0, 200.0])
        with patch.object(ha_client.time, "monotonic", side_effect=lambda: next(clock)), \
                self.assertRaises(ha_client.WsError) as caught:
            self.run_ws(connection, [{"type": "one"}], ha_client.WsLimits(timeout=15, deadline=45))
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual([call.args[0] for call in connection.settimeout.call_args_list], [15, 15])

    def test_connection_errors_pass_through(self):
        """A transport error reaches the caller as it is, and the connection still closes."""
        connection = fake_socket()
        connection.recv.side_effect = OSError("closed by peer")
        with self.assertRaisesRegex(OSError, "closed by peer"):
            self.run_ws(connection, [{"type": "one"}])
        connection.close.assert_called_once()


class RestTests(unittest.TestCase):
    """REST calls go once to the Supervisor proxy; the ha_url option is never the worker's address."""

    def setUp(self):
        """Give the worker a Supervisor token and an ha_url option that the worker must ignore."""
        self.enterContext(patch.dict(os.environ, {"SUPERVISOR_TOKEN": TOKEN}))
        self.enterContext(patch.object(server, "read_options", return_value={"ha_url": "https://ha.example.test"}))
        self.request = self.enterContext(patch.object(ha_client.requests, "request"))
        self.request.return_value = Mock(status_code=200, text="")

    def test_api_url_accepts_each_path_form(self):
        """A path with or without a leading slash or api/ prefix gives one URL."""
        for path in ("states", "/states", "api/states", "/api/states"):
            self.assertEqual(ha_client.api_url(path), "http://supervisor/core/api/states")

    def test_service_and_event_post_once_to_the_proxy(self):
        """Each call makes one POST with the token, to the Supervisor proxy, and reports ok."""
        self.assertEqual(ha_client.call_ha_service("light.turn_on", {"entity_id": "light.desk"}), (True, "ok"))
        self.assertEqual(ha_client.fire_ha_event("codex_cli_task_result", {"task_id": "t"}), (True, "ok"))
        calls = [(call.args, call.kwargs["headers"], call.kwargs["json"]) for call in self.request.call_args_list]
        self.assertEqual(calls, [
            (("POST", "http://supervisor/core/api/services/light/turn_on"), {"Authorization": f"Bearer {TOKEN}"},
             {"entity_id": "light.desk"}),
            (("POST", "http://supervisor/core/api/events/codex_cli_task_result"), {"Authorization": f"Bearer {TOKEN}"},
             {"task_id": "t"}),
        ])

    def test_errors_name_the_url_and_make_one_attempt(self):
        """An HTTP error or a connection error is reported with the URL, with no second attempt."""
        url = "http://supervisor/core/api/services/light/turn_on"
        self.request.return_value = Mock(status_code=401, text="unauthorized")
        self.assertEqual(ha_client.call_ha_service("light.turn_on", {}), (False, f"{url}: HTTP 401: unauthorized"))
        self.request.side_effect = OSError("unreachable")
        self.assertEqual(ha_client.call_ha_service("light.turn_on", {}), (False, f"{url}: unreachable"))
        self.assertEqual(self.request.call_count, 2)

    def test_no_token_and_bad_service_name_make_no_request(self):
        """Without a token, or with a service name that has no domain, nothing is sent."""
        self.assertEqual(ha_client.call_ha_service("turn_on", {}), (False, "Invalid service name: turn_on"))
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": ""}):
            self.assertEqual(ha_client.call_ha_service("light.turn_on", {}), (False, ha_client.NO_TOKEN))
            self.assertEqual(ha_client.fire_ha_event("x", {}), (False, ha_client.NO_TOKEN))
        self.request.assert_not_called()

    def test_the_old_fallback_names_are_gone(self):
        """The worker keeps no second address model: the token source and URL choosers are deleted."""
        for name in ("ha_token_source", "ha_base_url", "ha_ws_url", "ha_api_url"):
            self.assertFalse(hasattr(server, name), name)


class ServerCallerTests(unittest.TestCase):
    """The server's two WebSocket callers keep their messages and take the strict login."""

    def setUp(self):
        """Give the server a token."""
        self.enterContext(patch.object(server, "ha_token", return_value=TOKEN))

    def test_user_list_refuses_a_login_without_auth_required(self):
        """lookup_ha_users raises the credential error on a direct auth_ok and returns the list otherwise."""
        users = [{"id": "u1", "is_admin": True}]
        with patch.object(ha_client.websocket, "create_connection",
                          return_value=fake_socket(*LOGIN, {"id": 1, "type": "result", "success": True, "result": users})):
            self.assertEqual(server.lookup_ha_users(), users)
        with patch.object(ha_client.websocket, "create_connection", return_value=fake_socket({"type": "auth_ok"})), \
                self.assertRaisesRegex(RuntimeError, "refused the credential"):
            server.lookup_ha_users()

    def test_user_list_fails_closed(self):
        """A failed command, or a result that is not a list, raises: the caller then refuses, never allows."""
        failure = {"id": 1, "type": "result", "success": False, "error": {"message": "synthetic"}}
        not_a_list = {"id": 1, "type": "result", "success": True, "result": {"id": "u1", "is_admin": True}}
        for reply in (failure, not_a_list):
            with self.subTest(reply=reply), \
                    patch.object(ha_client.websocket, "create_connection", return_value=fake_socket(*LOGIN, reply)), \
                    self.assertRaisesRegex(RuntimeError, "did not return its user list"):
                server.lookup_ha_users()

    def test_dashboard_save_reports_home_assistant_error(self):
        """save_lovelace_dashboard sends lovelace/config/save and returns Home Assistant's error text on failure."""
        error = {"code": "invalid_format", "message": "bad view"}
        connection = fake_socket(*LOGIN, {"id": 1, "type": "result", "success": False, "error": error})
        with patch.object(server, "CONFIG_ROOT") as root, \
                patch.object(ha_client.websocket, "create_connection", return_value=connection):
            (root / "x").read_text.return_value = json.dumps({"data": {"config": {"views": []}}})
            ok, message = server.save_lovelace_dashboard({"storage_file": ".storage/lovelace.x", "url_path": "x"})
        self.assertEqual((ok, json.loads(message)), (False, error))
        self.assertEqual(sent(connection)[1], {"type": "lovelace/config/save", "config": {"views": []},
                                               "url_path": "x", "id": 1})


if __name__ == "__main__":
    unittest.main()
