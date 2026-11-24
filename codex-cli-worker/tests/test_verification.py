"""Verification boundaries, result ownership, and credential protection."""
import ast
import inspect
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_server import server
import tool_threads
import verification
from verification import Verification
import verification_mcp


THREAD = "11111111-2222-4333-8444-555555555555"


class VerificationTests(unittest.TestCase):
    """Checks and credential handling for one running chat turn."""

    def setUp(self):
        """Create a temporary worker with one running chat turn and begin verification for it."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.options = {**server.DEFAULT_OPTIONS, "task_root": str(self.root / "tasks")}
        patcher = patch.dict(server.tasks, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key, value in {"CONFIG_ROOT": self.config, "DATA_ROOT": self.root,
                           "TASK_STATE_FILE": self.root / "index.json"}.items():
            patcher = patch.object(server, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(server, "read_options", return_value=self.options)
        patcher.start()
        self.addCleanup(patcher.stop)
        server.get_task_dir("chat").mkdir(parents=True)
        turn = server.new_turn("Check my dashboard")
        server.tasks["chat"] = {"task_id": "chat", "status": "running",
                                "current_turn_id": turn["turn_id"], "turns": [turn]}
        self.engine = Verification(server)
        tool_threads.CHATS.bind("chat", THREAD)
        self.addCleanup(tool_threads.CHATS.release, "chat")
        self.capability = self.engine.begin("chat")

    def test_an_ended_exchange_is_refused_and_unsupported_operations_are_not_run(self):
        """Reject an unbound thread and an ended exchange, and refuse operations outside the supported set."""
        with self.assertRaises(ValueError):
            self.engine.dispatch({"thread_id": "99999999-2222-4333-8444-555555555555", "operation": "entity"})
        with self.assertRaises(ValueError):
            self.engine.dispatch({"capability": self.capability, "operation": "entity"})
        result = self.engine.dispatch({"thread_id": THREAD, "operation": "call_service"})
        self.assertEqual(result["status"], "unavailable")
        self.engine.end("chat")
        with self.assertRaises(ValueError):
            self.engine.dispatch({"thread_id": THREAD, "operation": "entity"})
        tool_threads.CHATS.release("chat")
        self.engine.begin("chat")
        with self.assertRaises(ValueError):
            self.engine.dispatch({"thread_id": THREAD, "operation": "entity"})

    def test_fresh_state_assertion_persists_in_this_turn(self):
        """Read the entity state again for each check and record it in the turn, with only the requested attributes."""
        with patch.object(self.engine, "core", side_effect=[
            {"state": "off", "attributes": {"brightness": 0, "token": "private"}},
            {"state": "on", "attributes": {"brightness": 100}},
        ]) as core:
            for expected in ("failed", "passed"):
                result = self.engine.run("chat", {"operation": "entity", "entity_id": "light.kitchen",
                                                  "expected_state": "on", "attributes": ["brightness"]})
                self.assertEqual(result["status"], expected)
                self.assertNotIn("token", result["attributes"])
            self.assertEqual(core.call_count, 2)
        self.assertEqual(len(server.tasks["chat"]["turns"][0]["verification"]), 2)
        self.assertNotIn(self.capability, json.dumps(server.task_payload(server.tasks["chat"])))
        self.engine.begin("chat")
        self.assertEqual(server.tasks["chat"]["verification"], [])

    def test_find_runs_as_a_check_and_is_recorded_in_this_turn(self):
        """Look an entity up by name through the same capability, limits and record as every other check."""
        states = [{"entity_id": "light.kitchen_ceiling", "state": "on", "attributes": {"friendly_name": "Kitchen Ceiling"}}]
        with patch("verification.fetch_states", return_value=states), patch.object(server, "ha_token", return_value="synthetic"):
            result = self.engine.dispatch({"thread_id": THREAD, "operation": "find", "name": "kitchen ceiling"})
        self.assertEqual(result["matches"], [{"entity_id": "light.kitchen_ceiling", "name": "Kitchen Ceiling", "state": "on"}])
        recorded = server.tasks["chat"]["turns"][0]["verification"]
        self.assertEqual([(check["operation"], check["status"]) for check in recorded], [("find", "observed")])

    def test_expired_check_cannot_record_results_in_a_finished_or_replaced_turn(self):
        """Discard the result of a check whose turn ended, completed, or was replaced while it ran."""
        for outcome in ("ended", "completed", "replaced"):
            with self.subTest(outcome=outcome):
                server.tasks["chat"]["status"] = "running"
                self.engine.begin("chat")
                def read(*_args, **_kwargs):
                    """End, complete, or replace the turn while the entity state is being read."""
                    if outcome == "ended":
                        self.engine.end("chat")
                    elif outcome == "completed":
                        server.tasks["chat"]["status"] = "completed"
                    else:
                        self.engine.begin("chat")
                    return {"state": "on", "attributes": {}}
                with patch.object(self.engine, "core", side_effect=read):
                    result = self.engine.run("chat", {"operation": "entity", "entity_id": "light.kitchen"})
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(server.tasks["chat"]["verification"], [])

    def test_malformed_thread_ids_and_other_app_logs_fail_closed(self):
        """Reject malformed thread ids and refuse another app's logs without calling Home Assistant."""
        for value in ("שלום", "", None, [], 1, THREAD.replace("1", "A"), "abcdefab-2222-4333-8444-555555555555".upper()):
            with self.subTest(thread=value), self.assertRaises(ValueError):
                self.engine.dispatch({"thread_id": value, "operation": "entity"})
        with self.assertRaises(ValueError):
            self.engine.dispatch({"operation": "entity"})
        with patch.object(self.engine, "core") as core:
            result = self.engine.run("chat", {"operation": "logs", "target": "core_mosquitto"})
        self.assertEqual(result["status"], "unavailable")
        core.assert_not_called()

    def test_dashboard_readback_compares_configuration_not_save_ack(self):
        """Fail the readback when the dashboard Home Assistant returns differs from the expected configuration."""
        with patch.object(self.engine, "ws_read", return_value={"views": []}):
            result = self.engine.run("chat", {"operation": "dashboard_readback", "path": "/lovelace/0",
                                              "expected_config": {"views": [{"title": "Missing"}]}})
        self.assertEqual(result["status"], "failed")

    def test_websocket_read_authenticates_and_closes_the_actual_client_interface(self):
        """Authenticate the WebSocket read with the Home Assistant token and close a client that only offers close()."""
        # websocket-client has close(), but no context manager methods.
        ws = Mock(spec=["recv", "send", "close"])
        ws.recv.side_effect = [
            '{"type":"auth_required"}', '{"type":"auth_ok"}',
            '{"id":1,"type":"result","success":true,"result":{"views":[]}}',
        ]
        with patch("ha_client.websocket.create_connection", return_value=ws), patch.object(server, "ha_token", return_value="supervisor-secret"):
            self.assertEqual(self.engine.ws_read({"type": "lovelace/config"}), {"views": []})
        self.assertEqual(json.loads(ws.send.call_args_list[0].args[0]), {"type": "auth", "access_token": "supervisor-secret"})
        ws.close.assert_called_once()

    def test_no_operation_renders_or_screenshots_a_dashboard(self):
        """The dashboard capture is gone: the tool does not offer it and the worker refuses it without a Core call."""
        self.assertNotIn("dashboard", verification_mcp.TOOL["inputSchema"]["properties"]["operation"]["enum"])
        self.assertNotIn("screenshot", verification_mcp.TOOL["description"])
        with patch.object(self.engine, "core") as core, patch("verification.requests.request") as request:
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/0"})
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("dashboard,", result["message"])
        core.assert_not_called()
        request.assert_not_called()
        self.assertFalse(hasattr(self.engine, "browser"))
        self.assertFalse(hasattr(self.engine, "request_retry"))
        # The AppArmor profile lets the app run no browser either.
        profile = (Path(__file__).resolve().parents[1] / "apparmor.txt").read_text(encoding="utf-8").lower()
        for name in ("chromium", "chrome", "playwright"):
            self.assertNotIn(name, profile)

    def test_readback_with_save_pending_saves_the_turn_edit_first(self):
        """save_pending on a readback saves the pending edit, then reads the dashboard back; without it, nothing is saved."""
        calls = []
        with patch.object(self.engine, "save_pending_dashboard", side_effect=lambda *a: calls.append(("save", a))), \
             patch.object(self.engine, "ws_read", side_effect=lambda m: calls.append(("read", m)) or {"views": [{}]}):
            result = self.engine.run("chat", {"operation": "dashboard_readback", "path": "/lovelace/0",
                                              "save_pending": True})
            self.assertEqual(result["status"], "observed")
            self.assertEqual([name for name, _ in calls], ["save", "read"])
            self.assertEqual(calls[0][1], ("chat", "/lovelace/0"))
            calls.clear()
            self.engine.run("chat", {"operation": "dashboard_readback", "path": "/lovelace/0"})
            self.assertEqual([name for name, _ in calls], ["read"])

    def test_readback_with_a_bad_path_saves_nothing(self):
        """A path that is not a local dashboard path is refused before the pending edit is saved."""
        for path in ("/lovelace/0?edit=1", "https://example.test/lovelace", 7, None, ["/lovelace/0"]):
            with self.subTest(path=path), patch.object(self.engine, "save_pending_dashboard") as save, \
                 patch.object(self.engine, "ws_read") as read:
                result = self.engine.run("chat", {"operation": "dashboard_readback", "path": path,
                                                  "save_pending": True})
                self.assertEqual(result["status"], "unavailable")
                self.assertIn("Use a local dashboard path", result["message"])
                save.assert_not_called()
                read.assert_not_called()

    def test_readback_refuses_save_pending_when_auto_save_is_off(self):
        """A refused save is reported, and the readback does not run as if the edit were saved."""
        self.options["auto_save_lovelace"] = False
        with patch.object(self.engine, "ws_read") as read:
            result = self.engine.run("chat", {"operation": "dashboard_readback", "path": "/lovelace/0",
                                              "save_pending": True})
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("auto-save is disabled", result["message"])
        read.assert_not_called()

    def test_snapshot_excludes_credentials_but_manifest_detects_changes(self):
        """Secret files (secrets.yaml and all of .storage) stay out of the archive but in change detection."""
        for name in ("secrets.yaml", ".storage/auth", ".storage/auth_provider.homeassistant", ".storage/core.config_entries", ".storage/lovelace",
                     "automations.yaml"):
            path = self.config / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"private": "value"}')
        server.get_run_dir("chat").mkdir(parents=True)
        snapshot = server.create_snapshot("chat")
        with tarfile.open(snapshot["path"]) as archive:
            self.assertEqual(archive.getnames(), ["automations.yaml"])
        self.assertIn("secrets.yaml", server.build_manifest())
        self.assertEqual(snapshot["file_count"], 1)
        recovery = server.recovery_copies(server.get_run_dir("chat"),
            {"added": [], "changed": ["secrets.yaml"], "deleted": []}, ["secrets.yaml"], {})
        details = server.validation_details(["secrets.yaml: bad YAML"], {}, recovery)
        self.assertIn("have no recovery copies", details)
        self.assertNotIn("New files", details)

    def test_supervisor_token_is_not_in_subprocess_environment(self):
        """Keep the Supervisor tokens out of the environment Codex runs in."""
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "private", "HASSIO_TOKEN": "private"}):
            environment = server.codex_env()
        self.assertNotIn("SUPERVISOR_TOKEN", environment)
        self.assertNotIn("HASSIO_TOKEN", environment)

    def test_token_option_comes_with_the_address_it_works_at(self):
        """With the HA_TOKEN option Codex also gets HA_URL: Core's own address, or the ha_url option when it names another."""
        with patch.object(server, "core_address", server.CoreAddress()), patch.dict(os.environ, {"HA_URL": "http://stale.example.test", "HA_TOKEN": "inherited"}):
            # Before discovery, neither stale inherited variable reaches Codex.
            environment = server.codex_env()
            self.assertNotIn("HA_TOKEN", environment)
            self.assertNotIn("HA_URL", environment)
            server.core_address.refresh({}, 'synthetic', fetch=lambda _: {'result': 'ok', 'data': {'port': 8123, 'ssl': False}})
            self.assertIn('HA_URL', server.codex_env())
            addresses = {
                None: "http://homeassistant:8123",
                "  ": "http://homeassistant:8123",
                # The option's earlier default, which rejects a Home Assistant token.
                "http://supervisor/core": "http://homeassistant:8123",
                "http://supervisor/core/api/": "http://homeassistant:8123",
                "http://homeassistant:8123/": "http://homeassistant:8123",
                "https://ha.example.test:8443/": "https://ha.example.test:8443",
            }
            for value, expected in addresses.items():
                with self.subTest(ha_url=value), patch.dict(self.options, {"HA_TOKEN": " token ", "ha_url": value}):
                    environment = server.codex_env()
                    self.assertEqual((environment["HA_TOKEN"], environment["HA_URL"]), ("token", expected))

    def test_pending_dashboard_cannot_save_unrelated_or_unvalidated_files(self):
        """Refuse to save a pending dashboard in read-only mode, with auto-save off, or without a turn baseline."""
        self.options["codex_sandbox"] = "read-only"
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.engine.save_pending_dashboard("chat", "/lovelace/0")
        self.options["codex_sandbox"] = "workspace-write"
        self.options["auto_save_lovelace"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.engine.save_pending_dashboard("chat", "/lovelace/0")
        self.options["auto_save_lovelace"] = True
        with self.assertRaisesRegex(ValueError, "baseline"):
            self.engine.save_pending_dashboard("chat", "/lovelace/0")

    def test_default_storage_dashboard_is_discovered(self):
        """Find the default dashboard in .storage/lovelace when that file changed."""
        path = self.config / ".storage" / "lovelace"
        path.parent.mkdir()
        path.write_text(json.dumps({"data": {"config": {"views": [{"title": "Home"}]}}}))
        refs = server.find_lovelace_dashboard_refs({"added": [], "changed": [".storage/lovelace"]})
        self.assertEqual(refs[0]["storage_file"], ".storage/lovelace")

    def test_expired_screenshot_is_deleted_but_other_images_remain(self):
        """Delete an expired verification screenshot and keep an image the user attached."""
        root = server.get_task_dir("chat")
        (root / "expired.png").write_bytes(b"expired")
        (root / "user.png").write_bytes(b"user")
        server.tasks["chat"]["turns"][0]["verification_attachments"] = [
            {"origin": "verification", "expires_at": 0, "path": "expired.png"},
            {"origin": "user", "expires_at": 0, "path": "user.png"},
        ]
        self.engine.cleanup()
        self.assertFalse((root / "expired.png").exists())
        self.assertTrue((root / "user.png").exists())


class WorkerInterfaceTests(unittest.TestCase):
    """VerificationWorker lists exactly what verification.py reads from the worker, and server.py has each name."""

    def setUp(self):
        """Collect the names of the interface and the names that the module reads from `self.worker`."""
        tree = ast.parse(Path(verification.__file__).read_text(encoding="utf-8"))
        interface = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "VerificationWorker")
        self.methods = {node.name for node in interface.body if isinstance(node, ast.FunctionDef)}
        self.values = {node.target.id for node in interface.body if isinstance(node, ast.AnnAssign)}
        self.used = {node.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                     and node.value.attr == "worker" and isinstance(node.value.value, ast.Name)
                     and node.value.value.id == "self"}

    def test_the_interface_lists_each_name_that_the_module_reads_and_no_other(self):
        """A new use of the worker must be added to the interface; a name no longer used must leave it."""
        self.assertEqual(self.used, self.methods | self.values)

    def test_the_worker_has_each_listed_name(self):
        """server.py defines each method as a function and each value as a module global."""
        for name in self.methods:
            self.assertTrue(inspect.isfunction(getattr(server, name, None)), name)
        for name in self.values:
            self.assertTrue(hasattr(server, name), name)

    def test_each_listed_method_takes_the_arguments_that_the_worker_takes(self):
        """A method of the interface and the function in server.py have the same parameters and return type.

        The worker function may take more keyword-only parameters with defaults; the module does not pass them.
        """
        for name in sorted(self.methods):
            declared = inspect.signature(getattr(verification.VerificationWorker, name))
            actual = inspect.signature(getattr(server, name))
            wanted = [str(parameter) for key, parameter in declared.parameters.items() if key != "self"]
            extra = [parameter for key, parameter in actual.parameters.items() if key not in declared.parameters]
            kept = [str(parameter) for key, parameter in actual.parameters.items() if key in declared.parameters]
            self.assertEqual(wanted, kept, name)
            self.assertTrue(all(parameter.kind is parameter.KEYWORD_ONLY and parameter.default is not parameter.empty
                                for parameter in extra), name)
            self.assertEqual(str(declared.return_annotation), str(actual.return_annotation), name)
