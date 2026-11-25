"""Core discovery, first-run relay read boundaries and launch integration."""

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_server import server
import core_address
import relay_reads
import relay_reads_mcp
import remote_control


def info(port=8123, ssl=False):
    return {"result": "ok", "data": {"port": port, "ssl": ssl}}


class AddressTests(unittest.TestCase):
    def test_discovery_default_alternate_custom_port_and_ssl(self):
        address = core_address.CoreAddress()
        for port, ssl in (
            (8123, False),
            (80, False),
            (43210, False),
            (443, True),
            (9443, True),
        ):
            fetch = Mock(return_value=info(port, ssl))
            result = address.refresh({}, "synthetic-supervisor", fetch)
            self.assertEqual(
                result["url"], f"{'https' if ssl else 'http'}://homeassistant:{port}"
            )
            self.assertEqual(result["source"], "discovery")
            fetch.assert_called_once_with("synthetic-supervisor")

    def test_empty_and_legacy_defaults_do_not_pin_a_port(self):
        for value in (
            "",
            " ",
            None,
            *core_address.LEGACY_DEFAULTS,
            "http://homeassistant:8123/",
        ):
            address = core_address.CoreAddress()
            result = address.refresh({"ha_url": value}, "synthetic", lambda _: info(80))
            self.assertEqual(result["url"], "http://homeassistant:80")
        self.assertIsNone(core_address.CoreAddress().snapshot({})["url"])

    def test_override_wins_and_removal_needs_fresh_discovery(self):
        address = core_address.CoreAddress()
        override = "https://homeassistant:9443"
        result = address.refresh({"ha_url": override}, "synthetic", lambda _: info(80))
        self.assertEqual(result, {"url": override, "source": "override", "error": ""})
        self.assertIsNone(address.snapshot({})["url"])
        self.assertEqual(
            address.refresh({}, "synthetic", lambda _: info(80))["url"],
            "http://homeassistant:80",
        )

        def missing(_):
            raise OSError("Synthetic transport failure.")

        self.assertEqual(
            address.refresh({"ha_url": override}, "synthetic", missing)["url"], override
        )

    def test_unavailable_or_invalid_reading_stays_null(self):
        address = core_address.CoreAddress()
        for value in (
            {},
            {"result": "error", "data": info()["data"]},
            info(True),
            info(0),
            info(65536),
            info("80"),
            info(80, "false"),
        ):
            with self.subTest(value=type(value).__name__):
                address.refresh({}, "synthetic", lambda _: info(80))
                result = address.refresh({}, "synthetic", lambda _: value)
                self.assertIsNone(result["url"])
                self.assertEqual(result["source"], "unavailable")
        result = address.refresh({}, "", lambda _: (_ for _ in ()).throw(OSError()))
        self.assertIsNone(result["url"])

    def test_override_rejects_credentials_schemes_queries_and_bad_ports(self):
        for url in (
            "file:///config",
            "http://user:password@homeassistant:80",
            "http://homeassistant:0",
            "http://homeassistant:99999",
            "http://homeassistant:80/?token=sample",
            "http://homeassistant:80/#sample",
        ):
            self.assertIsNone(
                core_address.CoreAddress().refresh(
                    {"ha_url": url}, "", lambda _: info()
                )["url"]
            )

    def test_supervisor_transport_is_fixed_bounded_and_cannot_redirect(self):
        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [json.dumps(info(80)).encode()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.return_value = response
        client = Mock()
        client.__enter__ = Mock(return_value=session)
        client.__exit__ = Mock(return_value=False)
        with patch.object(core_address.requests, "Session", return_value=client):
            self.assertEqual(core_address.fetch_info("synthetic-supervisor"), info(80))
        self.assertFalse(session.trust_env)
        args, kwargs = session.get.call_args
        self.assertEqual(args, (core_address.INFO_URL,))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (3, 5))
        self.assertEqual(
            kwargs["headers"], {"Authorization": "Bearer synthetic-supervisor"}
        )
        response.iter_content.return_value = [b"x" * 65537]
        with (
            patch.object(core_address.requests, "Session", return_value=client),
            self.assertRaises(ValueError),
        ):
            core_address.fetch_info("synthetic-supervisor")
        response.status_code = 302
        with (
            patch.object(core_address.requests, "Session", return_value=client),
            self.assertRaises(ValueError),
        ):
            core_address.fetch_info("synthetic-supervisor")
        with (
            patch.object(core_address.requests, "Session") as no_request,
            self.assertRaises(ValueError),
        ):
            core_address.fetch_info("")
        no_request.assert_not_called()

    def test_address_export_needs_the_core_token_and_strips_inherited_credentials(self):
        """Without the HA_TOKEN option Codex gets neither HA_TOKEN nor HA_URL; with it, both.

        A call to HA_URL without a token counts as a failed login, and Home Assistant bans the app after a few.
        """
        options = {**server.DEFAULT_OPTIONS, "HA_TOKEN": ""}
        with (
            patch.object(server, "core_address", core_address.CoreAddress()),
            patch.object(server, "read_options", return_value=options),
            patch.object(server, "ha_token", return_value="synthetic-supervisor"),
            patch.object(core_address, "get_json", return_value=info(80)),
            patch.dict(
                os.environ,
                {
                    "SUPERVISOR_TOKEN": "synthetic-supervisor",
                    "HA_TOKEN": "inherited",
                    "HA_URL": "http://stale.example.test",
                    "HA_RELAY_CAPABILITY": "stale",
                },
            ),
        ):
            server.refresh_core_address()
            env = server.codex_env()
            for name in (
                "SUPERVISOR_TOKEN",
                "HA_TOKEN",
                "HA_URL",
                "HA_RELAY_CAPABILITY",
            ):
                self.assertNotIn(name, env)
            relay_env = server.relay_env()
            for name in (
                "SUPERVISOR_TOKEN",
                "HA_TOKEN",
                "HA_URL",
                "HA_RELAY_CAPABILITY",
            ):
                self.assertNotIn(name, relay_env)
            options["HA_TOKEN"] = " synthetic-core "
            env, relay_env = server.codex_env(), server.relay_env()
            for environment in (env, relay_env):
                self.assertEqual(
                    (environment["HA_TOKEN"], environment["HA_URL"]),
                    ("synthetic-core", "http://homeassistant:80"),
                )
                self.assertNotIn("SUPERVISOR_TOKEN", environment)
                self.assertNotIn("HA_RELAY_CAPABILITY", environment)

    def test_task_launch_refreshes_before_baseline_and_process_start(self):
        class Boundary(BaseException):
            pass

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(server, "task_cancellation_requested", return_value=False),
            patch.object(server.protected_policy, "policy_problem", return_value=""),
            patch.object(server, "get_run_dir", return_value=Path(temp)),
            patch.object(server, "update_task"),
            patch.object(server, "note_task_sign_in"),
            patch.object(server.verification, "begin", return_value="synthetic"),
            patch.object(server.ha_data, "begin", return_value="synthetic"),
            patch.object(server, "start_activity"),
            patch.object(server, "refresh_core_address") as refresh,
            patch.object(server, "record_baseline", side_effect=Boundary),
        ):
            with self.assertRaises(Boundary):
                server.run_task("sample-task", "Inspect the sample configuration.")
            refresh.assert_called_once()

    def test_native_relay_launch_refreshes_and_registers_read_tool(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(server, "core_address", core_address.CoreAddress()),
            patch.object(
                server,
                "read_options",
                return_value={**server.DEFAULT_OPTIONS, "HA_TOKEN": ""},
            ),
            patch.object(server, "ha_token", return_value="synthetic-supervisor"),
            patch.object(core_address, "get_json", return_value=info(80)) as metadata,
            patch.object(remote_control.threading, "Thread"),
        ):
            proc = types.SimpleNamespace(
                stdout=io.StringIO(""), wait=lambda: 0, poll=lambda: None
            )
            popen = Mock(return_value=proc)
            supervisor = remote_control.Supervisor(
                "/synthetic/codex",
                Path(temp) / "state.json",
                server.relay_env,
                lambda: "Sample relay",
                popen=popen,
                run=Mock(return_value=subprocess.CompletedProcess([], 0)),
                tool_args_factory=server.relay_read_mcp_args,
            )
            supervisor.enabled = lambda: True
            supervisor.wake = Mock()
            supervisor.wake.is_set.return_value = False
            supervisor.wake.wait.return_value = True
            supervisor._loop()
            args, kwargs = popen.call_args
            self.assertTrue(any("relay_reads_mcp.py" in value for value in args[0]))
            # Without the HA_TOKEN option the relay gets no address to call by hand either.
            self.assertNotIn("HA_URL", kwargs["env"])
            self.assertNotIn("HA_TOKEN", kwargs["env"])
            self.assertNotIn("SUPERVISOR_TOKEN", kwargs["env"])
            self.assertNotIn("HA_RELAY_CAPABILITY", kwargs["env"])
            self.assertGreaterEqual(metadata.call_count, 1)

    def test_redaction_uses_the_effective_trimmed_core_credential(self):
        with (
            patch.object(server, "ha_token", return_value="synthetic-supervisor"),
            patch.object(server, "api_token", return_value="synthetic-worker"),
            patch.object(
                server, "read_options", return_value={"HA_TOKEN": " synthetic-core "}
            ),
        ):
            self.assertEqual(
                server.relay_read_redact(
                    "synthetic-core synthetic-worker synthetic-supervisor"
                ),
                "[redacted] [redacted] [redacted]",
            )

    def test_first_run_instructions_keep_network_approval_and_credential_boundary(self):
        text = server.remote_control_instructions({})
        for phrase in (
            "home_assistant_relay.read",
            "needs no hand-made HA_TOKEN",
            "request native approval",
            "Never bypass a denial",
            "No Core API token is configured",
            "instead of guessing a port",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("Authorization: Bearer", text)


class RelayReadTests(unittest.TestCase):
    def setUp(self):
        self.proc = types.SimpleNamespace(poll=lambda: None)
        self.stopping = False
        self.fetch = Mock(
            return_value=[
                {
                    "entity_id": "light.sample",
                    "state": "off",
                    "attributes": {"friendly_name": "Sample light"},
                }
            ]
        )
        self.broker = relay_reads.RelayReads(
            lambda: (self.proc, self.stopping),
            lambda: "synthetic-supervisor",
            lambda value: value.replace("synthetic-supervisor", "[redacted]"),
            self.fetch,
        )
        self.broker.new_launch()

    def call(self, **args):
        return self.broker.dispatch({"arguments": args})

    def test_first_run_lists_entities_without_a_user_core_token(self):
        result = self.call(operation="states", domain="light")
        self.assertEqual(result["entities"][0]["entity_id"], "light.sample")
        self.fetch.assert_called_once_with("/states", "synthetic-supervisor")
        self.assertNotIn("synthetic-supervisor", json.dumps(result))

    def test_state_only_returns_selected_observational_attributes(self):
        self.fetch.return_value = {
            "entity_id": "light.sample",
            "state": "off",
            "attributes": {
                "friendly_name": "Sample light",
                "password": "must never leave the worker",
            },
        }
        result = self.call(
            operation="state", entity_id="light.sample", attributes=["friendly_name"]
        )
        self.assertEqual(result["attributes"], {"friendly_name": "Sample light"})
        self.assertNotIn("password", json.dumps(result))

    def test_mutations_arbitrary_urls_and_credential_fields_fail_before_http(self):
        cases = (
            {"operation": "call_service"},
            {"operation": "config_check"},
            {"operation": "states", "url": "arbitrary"},
            {
                "operation": "state",
                "entity_id": "light.sample",
                "attributes": ["access_token"],
            },
            {"operation": "state", "entity_id": "sensor.api_token"},
            {"operation": "state", "entity_id": "../auth"},
            {"operation": "states", "limit": True},
        )
        for args in cases:
            with (
                self.subTest(operation=args["operation"]),
                self.assertRaises(ValueError),
            ):
                self.broker.dispatch({"arguments": args})
        self.fetch.assert_not_called()

    def test_paging_excludes_credential_like_ids_and_marks_truncation(self):
        self.fetch.return_value = [
            {"entity_id": "sensor.api_token", "state": "hidden"},
            {"entity_id": "light.b", "state": "x" * 600},
            {"entity_id": "light.a", "state": "on"},
        ]
        result = self.call(operation="states", domain="light", limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(result["entities"][0]["entity_id"], "light.a")
        result = self.call(operation="states", domain="light", offset=1, limit=1)
        self.assertTrue(result["entities"][0]["state_truncated"])
        self.assertIsNone(result["next_offset"])

    def test_no_launch_a_key_stop_and_exit_are_refused(self):
        # D-CX-KEYS: the socket takes no key; a request with one, or before any launch, is refused.
        for request in (
            {"capability": "invented", "arguments": {"operation": "states"}},
            {"operation": "states"},
        ):
            with self.assertRaises(ValueError):
                self.broker.dispatch(request)
        unlaunched = relay_reads.RelayReads(
            lambda: (self.proc, False), lambda: "synthetic-supervisor", str, self.fetch
        )
        with self.assertRaises(ValueError):
            unlaunched.dispatch({"arguments": {"operation": "states"}})
        self.stopping = True
        with self.assertRaises(ValueError):
            self.call(operation="states")
        self.stopping = False
        self.proc = types.SimpleNamespace(poll=lambda: 0)
        with self.assertRaises(ValueError):
            self.call(operation="states")
        self.fetch.assert_not_called()

    def test_a_diagnostics_export_in_the_launch_window_keeps_the_launch(self):
        # Review case C2: the export ends the diagnostics session while no process runs yet, after relay_env.
        self.proc = None
        self.broker.finish_observation_if_stopped()
        self.proc = types.SimpleNamespace(poll=lambda: None)
        self.assertEqual(
            self.call(operation="states", domain="light")["entities"][0]["entity_id"],
            "light.sample",
        )

    def test_inflight_result_is_discarded_after_a_new_launch(self):
        def expire(*args):
            self.broker.new_launch()
            return []

        self.fetch.side_effect = expire
        with self.assertRaises(ValueError):
            self.call(operation="states")

    def test_concurrent_read_is_refused_without_another_backend_call(self):
        self.broker.inflight.acquire()
        try:
            with self.assertRaises(ValueError):
                self.call(operation="states")
        finally:
            self.broker.inflight.release()
        self.fetch.assert_not_called()

    def test_known_credentials_are_redacted_and_oversized_data_refused(self):
        self.fetch.return_value = {
            "entity_id": "light.sample",
            "state": "synthetic-supervisor",
            "attributes": {},
        }
        self.assertEqual(
            self.call(operation="state", entity_id="light.sample")["state"],
            "[redacted]",
        )
        self.fetch.return_value = {
            "entity_id": "light.sample",
            "state": "off",
            "attributes": {"friendly_name": "x" * 70000},
        }
        with self.assertRaises(ValueError):
            self.call(
                operation="state",
                entity_id="light.sample",
                attributes=["friendly_name"],
            )

    def test_missing_worker_credential_and_invalid_core_response_fail_closed(self):
        with (
            patch.object(self.broker, "token", return_value=""),
            self.assertRaises(ValueError),
        ):
            self.call(operation="states")
        self.fetch.assert_not_called()
        self.fetch.return_value = {"not": "a list"}
        with self.assertRaises(ValueError):
            self.call(operation="states")

    def test_native_unix_bridge_and_a_stopping_relay(self):
        if os.environ.get("PYTEST_CURRENT_TEST"):
            import pytest_socket

            pytest_socket.socket_allow_hosts([], allow_unix_socket=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "read.sock"
            listener = self.broker.serve(path)
            try:
                with patch.dict(os.environ, {"HA_RELAY_SOCKET": str(path)}):
                    self.assertTrue(relay_reads_mcp.read({"operation": "states"})["ok"])
                    self.stopping = True
                    self.assertFalse(
                        relay_reads_mcp.read({"operation": "states"})["ok"]
                    )
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                listener.shutdown()
                listener.server_close()

    def test_stdio_tool_registration_has_no_action_or_approval_tool(self):
        script = Path(relay_reads_mcp.__file__)
        messages = [
            {"id": 1, "method": "initialize"},
            {"id": 2, "method": "tools/list"},
        ]
        result = subprocess.run(
            [sys.executable, "-I", str(script)],
            input="".join(json.dumps(m) + "\n" for m in messages),
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        tools = rows[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["read"])
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
        self.assertEqual(
            tools[0]["inputSchema"]["properties"]["operation"]["enum"],
            ["states", "state"],
        )
