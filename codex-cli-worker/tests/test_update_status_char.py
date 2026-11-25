"""Characterize the update banner facts without contacting Home Assistant."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import update_status as status  # noqa: E402


def manifest(version="1.2.3"):
    return {"id": 1, "type": "result", "success": True, "result": {"version": version}}


class Socket:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def recv(self):
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)

    def send(self, value):
        self.sent.append(json.loads(value))

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True


class VersionAndManifest(unittest.TestCase):
    def test_clean_version_shape_and_length(self):
        for value in ("1.2.3", "v1.0-rc+1", "a" * 40):
            with self.subTest(value=value):
                self.assertEqual(status.clean_version(value), value)
        for value in (None, 1, True, "", ".1", "has space", "../bad", "a" * 41):
            with self.subTest(value=value):
                self.assertIsNone(status.clean_version(value))

    def test_manifest_result_success_not_found_and_bad_results(self):
        self.assertEqual(status.manifest_reply_version(manifest()), "1.2.3")
        self.assertIsNone(
            status.manifest_reply_version(
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "not_found"},
                }
            )
        )
        cases = (
            {},
            {"id": 2, "type": "result", "success": True},
            {"id": 1, "type": "other", "success": True},
            {"id": 1, "type": "result", "success": False, "error": {}},
            {"id": 1, "type": "result", "success": True, "result": None},
            manifest("<bad>"),
        )
        for reply in cases:
            with self.subTest(reply=reply), self.assertRaises(status.ReadFailed):
                status.manifest_reply_version(reply)

    def test_serialized_status_keeps_nulls_and_both_fields(self):
        self.assertEqual(
            status.UpdateStatus("1.2.3", None).as_json(),
            {"restart_version": "1.2.3", "update_version": None},
        )
        self.assertEqual(
            status.UpdateStatus(None, "2.0").as_json(),
            {"restart_version": None, "update_version": "2.0"},
        )


class LoadedVersion(unittest.TestCase):
    def test_authentication_and_manifest_request_then_close(self):
        ws = Socket([{"type": "auth_required"}, {"type": "auth_ok"}, manifest()])
        with patch.object(
            status.websocket, "create_connection", return_value=ws
        ) as connect:
            self.assertEqual(
                status.loaded_integration_version("example-token"), "1.2.3"
            )
        connect.assert_called_once_with(
            status.CORE_WEBSOCKET_URL, timeout=status.WEBSOCKET_SECONDS
        )
        self.assertEqual(
            ws.sent,
            [
                {"type": "auth", "access_token": "example-token"},
                {
                    "id": 1,
                    "type": "manifest/get",
                    "integration": status.DISCOVERY_SERVICE,
                },
            ],
        )
        self.assertEqual(len(ws.timeouts), 3)
        self.assertTrue(all(timeout >= 0.1 for timeout in ws.timeouts))
        self.assertTrue(ws.closed)

    def test_not_found_is_a_valid_absent_loaded_integration(self):
        ws = Socket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "not_found"},
                },
            ]
        )
        with patch.object(status.websocket, "create_connection", return_value=ws):
            self.assertIsNone(status.loaded_integration_version("example-token"))
        self.assertTrue(ws.closed)

    def test_missing_token_never_connects(self):
        with (
            patch.object(status.websocket, "create_connection") as connect,
            self.assertRaises(status.ReadFailed),
        ):
            status.loaded_integration_version("")
        connect.assert_not_called()

    def test_bad_or_oversized_replies_and_io_failure_are_refused_and_closed(self):
        cases = (
            [{"type": "wrong"}],
            [{"type": "auth_required"}, {"type": "auth_invalid"}],
            ["not-json"],
            ["[]"],
            ["x" * (status.MAX_REPLY_BYTES + 1)],
            [{"type": "auth_required"}, OSError("test error")],
            [{"type": "auth_required"}, {"type": "auth_ok"}, manifest("bad value")],
        )
        for index, replies in enumerate(cases):
            with self.subTest(case=index):
                ws = Socket(replies)
                with (
                    patch.object(
                        status.websocket, "create_connection", return_value=ws
                    ),
                    self.assertRaises(status.ReadFailed),
                ):
                    status.loaded_integration_version("example-token")
                self.assertTrue(ws.closed)


class BannerFacts(unittest.TestCase):
    def test_restart_skips_reads_when_disabled_or_local_version_invalid(self):
        with (
            patch.object(status, "install_option_on", return_value=False),
            patch.object(status, "manifest_version") as disk,
            patch.object(status, "loaded_integration_version") as loaded,
        ):
            self.assertIsNone(status.restart_version("example-token"))
        disk.assert_not_called()
        loaded.assert_not_called()
        with (
            patch.object(status, "install_option_on", return_value=True),
            patch.object(status, "manifest_version", return_value="bad version"),
            patch.object(status, "loaded_integration_version") as loaded,
        ):
            self.assertIsNone(status.restart_version("example-token"))
        loaded.assert_not_called()

    def test_restart_compares_versions_and_suppresses_failed_reads(self):
        with (
            patch.object(status, "install_option_on", return_value=True),
            patch.object(status, "manifest_version", return_value="2.0"),
            patch.object(status, "loaded_integration_version") as loaded,
        ):
            for loaded_value, expected in (
                (None, "2.0"),
                ("1.0", "2.0"),
                ("2.0", None),
            ):
                loaded.return_value = loaded_value
                self.assertEqual(status.restart_version("example-token"), expected)
            loaded.side_effect = status.ReadFailed("test error")
            self.assertIsNone(status.restart_version("example-token"))

    def test_available_update_requires_true_and_a_valid_different_latest_version(self):
        cases = (
            (
                {
                    "data": {
                        "update_available": True,
                        "version": "1",
                        "version_latest": "2",
                    }
                },
                "2",
            ),
            (
                {
                    "data": {
                        "update_available": True,
                        "version": "2",
                        "version_latest": "2",
                    }
                },
                None,
            ),
            ({"data": {"update_available": 1, "version_latest": "2"}}, None),
            (
                {"data": {"update_available": True, "version_latest": "bad version"}},
                None,
            ),
            ({"data": {}}, None),
            ({"data": None}, None),
            ({}, None),
            (None, None),
        )
        for reply, expected in cases:
            with (
                self.subTest(reply=reply),
                patch.object(status, "get_json", return_value=reply) as get,
            ):
                self.assertEqual(status.available_update("example-token"), expected)
                get.assert_called_once_with(
                    status.SELF_INFO_URL, "example-token", status.MAX_REPLY_BYTES
                )
        with patch.object(status, "get_json", side_effect=ValueError("bad reply")):
            self.assertIsNone(status.available_update("example-token"))

    def test_read_returns_both_independent_facts(self):
        with (
            patch.object(status, "restart_version", return_value="1.1") as restart,
            patch.object(status, "available_update", return_value="1.2") as update,
        ):
            self.assertEqual(
                status.read("example-token").as_json(),
                {"restart_version": "1.1", "update_version": "1.2"},
            )
        restart.assert_called_once_with("example-token")
        update.assert_called_once_with("example-token")


if __name__ == "__main__":
    unittest.main()
