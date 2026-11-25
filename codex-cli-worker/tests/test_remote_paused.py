"""Remote Control is paused in this version: no process starts, the owner's saved choice is kept, the routes say so."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_server import server  # First: it puts the worker's folder on sys.path.
import remote_control as rc

ROOT = Path(__file__).resolve().parents[1]


class ThisVersionTests(unittest.TestCase):
    def test_remote_control_is_paused_in_this_version(self) -> None:
        self.assertIs(rc.PAUSED, True)


class PausedSupervisorTests(unittest.TestCase):
    """The supervisor starts nothing and writes nothing while PAUSED is on."""

    def setUp(self) -> None:
        paused = patch.object(rc, "PAUSED", True)
        paused.start()
        self.addCleanup(paused.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "remote_control.json"
        self.launches: list[list[str]] = []

        def popen(cmd, **_kwargs):
            self.launches.append(cmd)
            raise AssertionError(
                "the remote-control process must not start while paused"
            )

        self.sup = rc.Supervisor(
            codex_binary="/usr/local/bin/codex",
            state_path=self.state,
            env_factory=dict,
            name_factory=lambda: "Home Assistant",
            popen=popen,
        )

    def saved(self) -> str:
        return self.state.read_text(encoding="utf-8")

    def test_resume_of_an_owner_who_had_it_on_logs_once_and_keeps_the_choice(
        self,
    ) -> None:
        self.sup.save_enabled(True)
        before = self.saved()
        out = io.StringIO()
        with redirect_stdout(out):
            state = self.sup.resume()
        self.assertEqual(out.getvalue().strip(), rc.PAUSED_KEPT_LOG)
        self.assertEqual(
            (state["state"], state["message"], state["paused"]),
            ("paused", rc.PAUSED_MESSAGE, True),
        )
        self.assertEqual(self.saved(), before)
        self.assertIsNone(self.sup.thread)
        self.assertEqual(self.launches, [])

    def test_resume_of_an_owner_who_never_turned_it_on_logs_nothing(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.sup.resume()["state"], "paused")
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(self.state.exists())

    def test_start_launches_nothing_and_saves_nothing(self) -> None:
        state = self.sup.start()
        self.assertEqual(state["state"], "paused")
        self.assertFalse(self.state.exists())
        self.assertIsNone(self.sup.thread)
        self.assertEqual(self.launches, [])

    def test_stop_keeps_the_owners_choice_for_when_it_returns(self) -> None:
        self.sup.save_enabled(True)
        self.assertEqual(self.sup.stop()["state"], "paused")
        self.assertTrue(self.sup.enabled())

    def test_pair_is_refused_with_the_pause_line(self) -> None:
        self.sup.save_enabled(True)
        ok, state = self.sup.pair()
        self.assertFalse(ok)
        self.assertEqual(state["error"], rc.PAUSED_MESSAGE)
        self.assertEqual(state["pairing_code"], "")

    def test_launch_itself_is_gated(self) -> None:
        self.assertEqual(self.sup._launch()["state"], "paused")
        self.assertIsNone(self.sup.thread)


class PausedRouteTests(unittest.TestCase):
    """The page reads {paused: true}; every change is refused; the integration's summary says paused."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.options = Path(tmp.name) / "options.json"
        self.options.write_text(
            json.dumps({"remote_control_name": "Home Assistant"}), encoding="utf-8"
        )
        state_path = Path(tmp.name) / "remote_control.json"
        state_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        for patcher in (
            patch.object(rc, "PAUSED", True),
            patch.object(server.remote_control_supervisor, "state_path", state_path),
            patch.object(
                server.remote_control_supervisor,
                "popen",
                side_effect=AssertionError("no launch"),
            ),
            patch.object(server, "OPTIONS_PATH", self.options),
            patch.object(server, "api_token", return_value="tok"),
            patch.object(
                server, "ingress_admin", return_value=({"user_id": "fixture-admin"}, "")
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = server.app.test_client()
        self.ingress = {
            "headers": {"X-Ingress-Path": "/api/hassio_ingress/test"},
            "environ_overrides": {"REMOTE_ADDR": server.INGRESS_PROXY_IP},
        }

    def test_state_read_says_paused_and_runs_no_core_address_check(self) -> None:
        with patch.object(
            server.core_address, "snapshot", side_effect=AssertionError("no probe")
        ):
            response = self.client.get(
                "/remote-control", headers={"Authorization": "Bearer tok"}
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIs(body["paused"], True)
        self.assertEqual(body["remote_control"]["message"], rc.PAUSED_MESSAGE)
        self.assertIs(body["remote_control"]["enabled"], True)

    def test_every_change_is_refused_and_the_options_stay(self) -> None:
        before = self.options.read_text(encoding="utf-8")
        for action, body in (
            ("start", {}),
            ("stop", {}),
            ("pair", {}),
            ("name", {"name": "Other"}),
        ):
            with self.subTest(action=action):
                response = self.client.post(
                    f"/remote-control/{action}", json=body, **self.ingress
                )
                self.assertEqual(response.status_code, 409)
                self.assertIs(response.get_json()["paused"], True)
                self.assertEqual(response.get_json()["error"], rc.PAUSED_MESSAGE)
        self.assertEqual(self.options.read_text(encoding="utf-8"), before)
        self.assertTrue(server.remote_control_supervisor.enabled())

    def test_the_api_token_still_cannot_change_it(self) -> None:
        response = self.client.post(
            "/remote-control/start", headers={"Authorization": "Bearer tok"}, json={}
        )
        self.assertEqual(response.status_code, 403)

    def test_full_access_restart_keeps_the_choice_and_starts_nothing(self) -> None:
        server.restart_remote_control()
        self.assertTrue(server.remote_control_supervisor.enabled())
        self.assertIsNone(server.remote_control_supervisor.proc)

    def test_integration_summary_reports_paused(self) -> None:
        server.remote_control_supervisor.paused()
        self.assertEqual(
            server.remote_control_summary(), {"enabled": True, "state": "paused"}
        )


class PausedPageTests(unittest.TestCase):
    """The worker marks the page while paused; every remote part carries the mark the stylesheet hides.

    browser_remote_paused.cjs checks in a browser that the parts are hidden and that no request goes out.
    """

    def test_page_is_marked_only_while_paused(self) -> None:
        with patch.object(rc, "PAUSED", True):
            self.assertIn('<html lang="en" data-remote-paused>', server.render_index())
        with patch.object(rc, "PAUSED", False):
            self.assertNotIn("data-remote-paused", server.render_index())

    def test_every_remote_part_is_marked(self) -> None:
        page = server.render_index()
        for element in (
            'id="remote-status-row" data-remote',
            'id="app-pairing-section" data-remote',
            'id="connection-dialog" data-remote',
            'id="remote-control-section" data-remote',
            "<span data-remote>, the remote control name</span>",
        ):
            with self.subTest(element=element):
                self.assertIn(element, page)
        css = (ROOT / "web/assets/chat.css").read_text(encoding="utf-8")
        self.assertIn(
            "[data-remote-paused] [data-remote] {\n  display: none !important;", css
        )


if __name__ == "__main__":
    unittest.main()
