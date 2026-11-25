"""The update banners' facts: a restart to finish an integration update, and an app update (update_status.py)."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
import requests
import update_status
import websocket

WEB = Path(__file__).resolve().parents[1] / "web"


class FakeSocket:
    """A Core WebSocket that answers the auth handshake and manifest/get with the given reply."""

    def __init__(self, reply, auth="auth_ok"):
        self.replies = [{"type": "auth_required"}, {"type": auth}, reply]
        self.sent = []

    def settimeout(self, seconds):
        pass

    def recv(self):
        return json.dumps(self.replies.pop(0))

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        pass


def manifest(version):
    return {
        "id": 1,
        "type": "result",
        "success": True,
        "result": {"domain": "codex_cli", "version": version},
    }


NOT_FOUND = {
    "id": 1,
    "type": "result",
    "success": False,
    "error": {"code": "not_found", "message": "x"},
}


class RestartNeeded(unittest.TestCase):
    """The restart banner names the version on disk while Home Assistant runs another one, or none."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.target = Path(temp.name) / "codex_cli"
        self.options = Path(temp.name) / "options.json"
        self.options.write_text(json.dumps({"install_integration": True}))

    def install(self, version):
        self.target.mkdir(exist_ok=True)
        (self.target / "manifest.json").write_text(
            json.dumps({"domain": "codex_cli", "version": version})
        )

    def restart(self, reply=None, error=None, auth="auth_ok"):
        socket = FakeSocket(reply, auth)
        create = patch.object(
            update_status.websocket,
            "create_connection",
            side_effect=error,
            return_value=None if error else socket,
        )
        with create as connect:
            result = update_status.restart_version(
                "synthetic-supervisor", self.target, self.options
            )
        return result, socket, connect

    def test_core_runs_an_older_version_after_the_app_updated_the_files(self):
        self.install("0.5.0")
        result, socket, connect = self.restart(manifest("0.4.2"))
        self.assertEqual(result, "0.5.0")
        self.assertEqual(connect.call_args.args[0], "ws://supervisor/core/websocket")
        self.assertEqual(
            socket.sent[0], {"type": "auth", "access_token": "synthetic-supervisor"}
        )
        self.assertEqual(
            socket.sent[1],
            {"id": 1, "type": "manifest/get", "integration": "codex_cli"},
        )

    def test_first_install_before_any_restart(self):
        # Home Assistant did not see the folder at its last start, so it answers not_found.
        self.install("0.5.0")
        self.assertEqual(self.restart(NOT_FOUND)[0], "0.5.0")

    def test_no_banner_once_core_runs_the_version_on_disk(self):
        self.install("0.5.0")
        self.assertIsNone(self.restart(manifest("0.5.0"))[0])

    def test_no_banner_without_an_integration_on_disk(self):
        result, _, connect = self.restart(manifest("0.4.2"))
        self.assertIsNone(result)
        connect.assert_not_called()

    def test_no_banner_and_no_read_with_the_install_option_off(self):
        # The owner installs the integration then; the app reads nothing in custom_components.
        self.install("0.5.0")
        self.options.write_text(json.dumps({"install_integration": False}))
        with patch.object(update_status, "manifest_version") as read_manifest:
            result, _, connect = self.restart(manifest("0.4.2"))
        self.assertIsNone(result)
        read_manifest.assert_not_called()
        connect.assert_not_called()

    def test_no_banner_when_core_cannot_say(self):
        # Each failure leaves the page silent rather than guessing that a restart is due.
        self.install("0.5.0")
        cases = {
            "unreachable": {"error": OSError("synthetic")},
            "closed": {"error": websocket.WebSocketException("synthetic")},
            "auth refused": {"reply": manifest("0.4.2"), "auth": "auth_invalid"},
            "other error": {
                "reply": {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_error"},
                }
            },
            "no version": {
                "reply": {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": {"domain": "codex_cli"},
                }
            },
            "odd version": {"reply": manifest("<b>1</b>")},
            "wrong id": {"reply": {**manifest("0.4.2"), "id": 2}},
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.restart(**case)[0])


def self_info(update_available, latest="0.5.1", version="0.5.0"):
    data = {
        "version": version,
        "version_latest": latest,
        "update_available": update_available,
    }
    return {"result": "ok", "data": data}


class UpdateAvailable(unittest.TestCase):
    """The update banner names Supervisor's newer version; the app installs nothing."""

    def available(self, reply=None, error=None):
        with patch.object(
            update_status, "get_json", side_effect=error, return_value=reply
        ) as get:
            return update_status.available_update("synthetic-supervisor"), get

    def test_supervisor_offers_a_newer_version(self):
        result, get = self.available(self_info(True))
        self.assertEqual(result, "0.5.1")
        self.assertEqual(
            get.call_args.args[:2],
            ("http://supervisor/addons/self/info", "synthetic-supervisor"),
        )

    def test_no_banner_without_an_update(self):
        self.assertIsNone(self.available(self_info(False))[0])
        self.assertIsNone(self.available(self_info(True, latest="0.5.0"))[0])
        self.assertIsNone(self.available(self_info("true"))[0])

    def test_no_banner_when_supervisor_cannot_say(self):
        for case in (
            {"error": requests.ConnectionError("synthetic")},
            {"error": ValueError("403")},
            {"reply": {"result": "ok"}},
            {"reply": {"data": None}},
            {"reply": self_info(True, latest="../x")},
            {"reply": self_info(True, latest=None)},
        ):
            with self.subTest(case=case):
                self.assertIsNone(self.available(**case)[0])


class Route(unittest.TestCase):
    """GET /update-status gives both versions and, with an update, the path of the app's Info page."""

    def setUp(self):
        for name, value in (("api_token", "tok"), ("ha_token", "synthetic-supervisor")):
            patcher = patch.object(server, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = server.app.test_client()

    def get(self, status, slug="a1b2c3d4_codex_cli_worker"):
        with (
            patch.object(server.update_status, "read", return_value=status) as read,
            patch.object(server, "addon_slug", return_value=slug),
        ):
            response = self.client.get(
                "/update-status", headers={"Authorization": "Bearer tok"}
            )
        read.assert_called_once_with("synthetic-supervisor")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_both_banners_and_the_info_page(self):
        reply = self.get(update_status.UpdateStatus("0.5.0", "0.5.1"))
        self.assertEqual(
            reply,
            {
                "ok": True,
                "restart_version": "0.5.0",
                "update_version": "0.5.1",
                "info_url": "/config/app/a1b2c3d4_codex_cli_worker/info",
            },
        )

    def test_no_update_means_no_link(self):
        reply = self.get(update_status.UpdateStatus("0.5.0", None))
        self.assertEqual(reply["info_url"], None)
        self.assertEqual(reply["restart_version"], "0.5.0")

    def test_an_update_without_a_slug_has_no_link(self):
        self.assertIsNone(
            self.get(update_status.UpdateStatus(None, "0.5.1"), slug=None)["info_url"]
        )

    def test_the_route_needs_the_worker_token(self):
        self.assertEqual(self.client.get("/update-status").status_code, 401)


class Page(unittest.TestCase):
    """The page loads the banner script and has both banners, hidden until the worker names a version."""

    def test_banners_and_script(self):
        html = (WEB / "index.html").read_text()
        for banner in ("restart-banner", "update-banner"):
            with self.subTest(banner):
                tag = html.split(f'id="{banner}"', 1)[1].split(">", 1)[0]
                self.assertIn("hidden", tag)
        self.assertIn('<script src="assets/update_banner.js" defer></script>', html)
        script = (WEB / "assets" / "update_banner.js").read_text()
        # The app only shows the facts: no call that installs an update or restarts anything.
        self.assertEqual(re.findall(r'api\("([^"]+)"', script), ["update-status"])


if __name__ == "__main__":
    unittest.main()
