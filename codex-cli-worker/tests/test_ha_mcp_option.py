"""The option home_assistant_mcp: the bundled hass-mcp server, which tools Codex gets, and where its token goes.

The server reads Home Assistant with the Supervisor token, which has full rights. These tests check that
Codex is offered hass-mcp's read tools approved ahead and its write tools on prompt, each write with a page
card; that a tool not sorted into a list is never offered; that a scheduled run gets the same server as a page chat; that the option turns all of it off; and that the token
reaches the server without a place a command Codex runs could read it. All data here is invented.
"""
from __future__ import annotations

import fnmatch
import io
import json
import re
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import yaml

from test_server import server  # First: it puts the worker's folder on sys.path.

# isort: split
import approvals
import hass_mcp
import hass_mcp_launch
import remote_control as rc

WORKER = Path(__file__).resolve().parents[1]
PREFIX = f"mcp_servers.{hass_mcp.SERVER_NAME}."
FAKE_TOKEN = "invented-supervisor-token-0123456789abcdef"

# Every tool of hass-mcp 0.6.0 (app/server.py of the pinned wheel, sha256 37ad047c...0be8). When the pin moves,
# this list moves with it, and each new tool goes into READ_TOOLS or WRITE_TOOLS in hass_mcp.py.
PINNED_TOOLS = {
    "add_card", "add_view", "call_service_tool", "domain_summary_tool", "entity_action", "get_dashboard_config",
    "get_entities_by_area", "get_entity", "get_error_log", "get_history", "get_history_range", "get_statistics",
    "get_statistics_range", "get_version", "list_automations", "list_dashboard_backups", "list_dashboards",
    "list_entities", "list_view_sections", "move_card", "remove_card", "remove_view", "restart_ha",
    "restore_dashboard", "search_entities_tool", "set_dashboard_config", "system_overview", "update_card",
    "update_view",
}


def configured(args):
    """Return every --config value of a Codex command line."""
    return [value for flag, value in pairwise(args) if flag == "--config"]


def server_settings(args):
    """Return the hass-mcp server's settings on a command line, as {setting: value}."""
    return {key[len(PREFIX):]: json.loads(value) for key, _, value in
            (item.partition("=") for item in configured(args)) if key.startswith(PREFIX)}


class ToolListTests(unittest.TestCase):
    """Each tool of the pinned version is sorted: reads approved ahead, writes asked about before each call."""

    def test_every_pinned_tool_is_sorted_into_one_list(self):
        reads, writes = set(hass_mcp.READ_TOOLS), set(hass_mcp.WRITE_TOOLS)
        self.assertFalse(reads & writes)
        self.assertEqual(reads | writes, PINNED_TOOLS)

    def test_the_pin_is_the_same_in_the_module_the_dockerfile_and_the_requirements(self):
        dockerfile = (WORKER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"ARG HASS_MCP_VERSION={hass_mcp.VERSION}\n", dockerfile)
        requirements = (WORKER / "hass-mcp.requirements.txt").read_text(encoding="utf-8")
        self.assertIn(f"\nhass-mcp=={hass_mcp.VERSION} \\\n", requirements)

    def test_reads_are_approved_ahead_and_writes_are_asked_about_each_time(self):
        settings = hass_mcp.settings("/usr/bin/python3")
        self.assertEqual(settings["enabled_tools"], [*hass_mcp.READ_TOOLS, *hass_mcp.WRITE_TOOLS])
        modes = {key[len("tools."):-len(".approval_mode")]: value for key, value in settings.items()
                 if key.startswith("tools.")}
        self.assertEqual(modes, {**{tool: "approve" for tool in hass_mcp.READ_TOOLS},
                                 **{tool: "prompt" for tool in hass_mcp.WRITE_TOOLS}})
        self.assertNotIn("default_tools_approval_mode", settings)

    def test_a_tool_outside_the_lists_is_never_offered(self):
        # enabled_tools is an allow list: a tool a later hass-mcp adds stays hidden until it is sorted.
        offered = set(hass_mcp.settings("/usr/bin/python3")["enabled_tools"])
        self.assertNotIn("a_tool_from_a_later_version", offered)
        self.assertEqual(offered, PINNED_TOOLS)

    def test_every_write_tool_is_on_prompt_on_every_launch(self):
        # A write tool with no mode would be "auto", whose question offers "Allow for this session".
        with patch.object(server, "read_options", return_value={}):
            launches = {"page": server.build_app_server_args(),
                        "schedule": server.build_app_server_args(attended=False),
                        "relay": server.relay_tool_args()}
        for name, args in launches.items():
            settings = server_settings(args)
            for tool in hass_mcp.WRITE_TOOLS:
                with self.subTest(launch=name, tool=tool):
                    self.assertIn(tool, settings["enabled_tools"])
                    self.assertEqual(settings[f"tools.{tool}.approval_mode"], "prompt")

    def test_every_write_tool_has_a_page_card_and_no_read_tool_does(self):
        cards = {tool for server_name, tool in approvals.TOOL_CARDS if server_name == hass_mcp.SERVER_NAME}
        self.assertEqual(cards, set(hass_mcp.WRITE_TOOLS))


class LaunchTests(unittest.TestCase):
    """Who gets the server, and that the option turns it off."""

    def launches(self, options):
        with patch.object(server, "read_options", return_value=options):
            return {"page": server.build_app_server_args(),
                    "schedule": server.build_app_server_args(attended=False),
                    "relay": server.relay_tool_args()}

    def test_on_by_default_for_a_page_chat_and_the_relay(self):
        for options in ({}, {"home_assistant_mcp": True}):
            launches = self.launches(options)
            with self.subTest(options=options):
                page = server_settings(launches["page"])
                self.assertEqual(page["command"], server.sys.executable)
                self.assertEqual(page["args"], [hass_mcp.LAUNCHER])
                self.assertFalse(page["required"])
                self.assertNotIn("env_vars", page)
                relay = server_settings(launches["relay"])
                self.assertEqual(relay["env_vars"], [hass_mcp.KEY_ENV])
                self.assertEqual(relay["enabled_tools"], [*hass_mcp.READ_TOOLS, *hass_mcp.WRITE_TOOLS])

    def test_a_scheduled_run_gets_the_same_server_as_a_page_chat(self):
        launches = self.launches({})
        self.assertEqual(server_settings(launches["schedule"]), server_settings(launches["page"]))

    def test_off_removes_the_server_everywhere(self):
        for name, args in self.launches({"home_assistant_mcp": False}).items():
            with self.subTest(launch=name):
                self.assertEqual([value for value in configured(args) if hass_mcp.SERVER_NAME in value], [])

    def test_the_default_options_and_the_option_schema_agree(self):
        self.assertIs(server.DEFAULT_OPTIONS["home_assistant_mcp"], True)
        config = yaml.safe_load((WORKER / "config.yaml").read_text(encoding="utf-8"))
        self.assertIs(config["options"]["home_assistant_mcp"], True)
        self.assertEqual(config["schema"]["home_assistant_mcp"], "bool")
        labels = yaml.safe_load((WORKER / "translations" / "en.yaml").read_text(encoding="utf-8"))
        self.assertEqual(labels["configuration"]["home_assistant_mcp"]["name"], "Enable Home Assistant MCP")


class TokenTests(unittest.TestCase):
    """The token reaches hass-mcp, and no launch line, chat environment or shell command holds it."""

    def relay_env(self, options):
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": FAKE_TOKEN}), \
                patch.object(server, "refresh_core_address"), \
                patch.object(server, "read_options", return_value=options):
            return rc.remote_env(server.relay_env())

    def test_no_launch_line_holds_the_token(self):
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": FAKE_TOKEN}), \
                patch.object(server, "read_options", return_value={}):
            for args in (server.build_app_server_args(), server.relay_tool_args()):
                self.assertFalse([value for value in args if FAKE_TOKEN in value])

    def test_a_chat_environment_holds_no_token(self):
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": FAKE_TOKEN, hass_mcp.KEY_ENV: FAKE_TOKEN}), \
                patch.object(server, "read_options", return_value={}):
            env = server.codex_env()
        self.assertNotIn(hass_mcp.KEY_ENV, env)
        self.assertFalse([name for name, value in env.items() if FAKE_TOKEN in value])

    def test_the_relay_gets_the_token_under_the_excluded_name_only_when_on(self):
        self.assertEqual(self.relay_env({})[hass_mcp.KEY_ENV], FAKE_TOKEN)
        self.assertNotIn(hass_mcp.KEY_ENV, self.relay_env({"home_assistant_mcp": False}))
        with patch.object(server, "refresh_core_address"), patch.object(server, "read_options", return_value={}):
            self.assertNotIn(hass_mcp.KEY_ENV, rc.remote_env(server.relay_probe_env()))

    def test_every_shell_exclude_covers_the_key_name(self):
        managed = tomllib.loads((WORKER / "gate" / "managed_config.toml").read_text(encoding="utf-8"))
        for patterns in (managed["shell_environment_policy"]["exclude"], list(server.SHELL_EXCLUDED_ENV)):
            self.assertTrue(any(fnmatch.fnmatchcase(hass_mcp.KEY_ENV, pattern) for pattern in patterns))
        self.assertIn('shell_environment_policy.exclude=["HA_*_CAPABILITY"]', configured(server.relay_tool_args()))

    def test_the_token_file_is_hidden_from_every_sandboxed_command(self):
        bwrap = (WORKER / "codex-bwrap").read_text(encoding="utf-8")
        hidden = re.search(r'^HIDDEN_DIRS="([^"]*)"', bwrap, re.MULTILINE).group(1).split()
        self.assertTrue(any(hass_mcp_launch.TOKEN_FILE.is_relative_to(folder) for folder in hidden))


class LauncherTests(unittest.TestCase):
    """hass_mcp_launch.py: where it takes the token, what hass-mcp gets, and no token in its messages."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.token_file = Path(folder.name) / "SUPERVISOR_TOKEN"

    def test_the_relay_key_comes_first_then_the_file(self):
        self.token_file.write_text("from-file\n", encoding="utf-8")
        self.assertEqual(hass_mcp_launch.token({hass_mcp.KEY_ENV: "from-key"}, self.token_file), "from-key")
        self.assertEqual(hass_mcp_launch.token({}, self.token_file), "from-file")
        self.assertEqual(hass_mcp_launch.token({}, self.token_file.with_name("missing")), "")

    def test_hass_mcp_gets_the_address_and_token_and_not_the_key(self):
        env = hass_mcp_launch.server_env({hass_mcp.KEY_ENV: FAKE_TOKEN, "HOME": "/data"}, FAKE_TOKEN)
        self.assertEqual(env, {"HOME": "/data", "HA_URL": "http://supervisor/core", "HA_TOKEN": FAKE_TOKEN,
                               "HASS_MCP_BACKUP_DIR": "/data/hass-mcp-backups"})

    def test_it_replaces_itself_with_hass_mcp(self):
        with patch.dict("os.environ", {hass_mcp.KEY_ENV: FAKE_TOKEN}, clear=True), \
                patch.object(hass_mcp_launch.os, "execve") as execve:
            hass_mcp_launch.main()
        path, argv, env = execve.call_args.args
        self.assertEqual((path, argv), (hass_mcp_launch.SERVER, [hass_mcp_launch.SERVER]))
        self.assertEqual(env["HA_TOKEN"], FAKE_TOKEN)
        self.assertNotIn(hass_mcp.KEY_ENV, env)

    def test_with_no_token_it_stops_and_says_so(self):
        errors = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(hass_mcp_launch, "TOKEN_FILE", self.token_file.with_name("missing")), \
                patch.object(hass_mcp_launch.os, "execve") as execve, redirect_stderr(errors):
            self.assertEqual(hass_mcp_launch.main(), 1)
        execve.assert_not_called()
        self.assertIn("no Supervisor token", errors.getvalue())

    def test_the_launcher_and_the_module_agree(self):
        self.assertEqual(hass_mcp_launch.KEY_ENV, hass_mcp.KEY_ENV)
        dockerfile = (WORKER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"COPY hass_mcp_launch.py {hass_mcp.LAUNCHER}\n", dockerfile)


class ImageTests(unittest.TestCase):
    """The image installs exactly the pinned wheels, checked by hash, and nothing it compiles."""

    def test_every_requirement_is_pinned_with_a_hash(self):
        text = (WORKER / "hass-mcp.requirements.txt").read_text(encoding="utf-8")
        entries = [entry for entry in re.split(r"\n(?=[a-z])", text.split("\n", 3)[3]) if entry.strip()]
        self.assertGreater(len(entries), 30)
        for entry in entries:
            with self.subTest(entry=entry.split()[0]):
                self.assertRegex(entry.split()[0], r"^[a-z0-9-]+==[0-9][0-9A-Za-z.]*$")
                self.assertRegex(entry, r"--hash=sha256:[0-9a-f]{64}")

    def test_the_install_checks_hashes_and_takes_wheels_only(self):
        dockerfile = (WORKER / "Dockerfile").read_text(encoding="utf-8")
        install = dockerfile[dockerfile.index("python3 -m venv --without-pip /opt/hass-mcp"):]
        install = install[:install.index("\n\n")]
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "chmod -R a-w /opt/hass-mcp"):
            self.assertIn(flag, install)


if __name__ == "__main__":
    unittest.main()
