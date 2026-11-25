"""Record the current configuration contract of the bundled Home Assistant MCP server."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hass_mcp  # noqa: E402


class HassMcpConfigurationTests(unittest.TestCase):
    def test_enabled_defaults_to_on(self):
        self.assertIs(hass_mcp.enabled({}), True)
        self.assertIs(hass_mcp.enabled({"home_assistant_mcp": True}), True)

    def test_only_boolean_false_disables_the_option(self):
        self.assertIs(hass_mcp.enabled({"home_assistant_mcp": False}), False)
        for value in (None, 0, ""):
            with self.subTest(value=value):
                self.assertIs(hass_mcp.enabled({"home_assistant_mcp": value}), True)

    def test_chat_settings_use_an_optional_server_with_fixed_timeouts(self):
        settings = hass_mcp.settings("/sample/bin/python3")
        self.assertEqual(settings["command"], "/sample/bin/python3")
        self.assertEqual(settings["args"], ["/usr/share/ha_codex/hass_mcp_launch.py"])
        self.assertIs(settings["enabled"], True)
        self.assertIs(settings["required"], False)
        self.assertEqual(settings["startup_timeout_sec"], 20)
        self.assertEqual(settings["tool_timeout_sec"], 90)
        self.assertNotIn("env_vars", settings)

    def test_relay_settings_add_only_the_capability_variable_name(self):
        python = "/sample/bin/python3"
        chat = hass_mcp.settings(python)
        relay = hass_mcp.settings(python, relay=True)
        self.assertEqual(relay, {**chat, "env_vars": ["HA_MCP_CAPABILITY"]})

    def test_tool_settings_list_each_tool_with_its_approval_mode(self):
        settings = hass_mcp.settings("/sample/bin/python3")
        expected_modes = {
            **{tool: "approve" for tool in hass_mcp.READ_TOOLS},
            **{tool: "prompt" for tool in hass_mcp.WRITE_TOOLS},
        }
        self.assertEqual(
            settings["enabled_tools"], [*hass_mcp.READ_TOOLS, *hass_mcp.WRITE_TOOLS]
        )
        modes = {
            key.removeprefix("tools.").removesuffix(".approval_mode"): mode
            for key, mode in settings.items()
            if key.startswith("tools.") and key.endswith(".approval_mode")
        }
        self.assertEqual(modes, expected_modes)
        self.assertEqual(modes["get_entity"], "approve")
        self.assertEqual(modes["get_dashboard_config"], "approve")
        self.assertEqual(modes["entity_action"], "prompt")
        self.assertEqual(modes["set_dashboard_config"], "prompt")

    def test_disabled_option_omits_all_config_arguments(self):
        for relay in (False, True):
            with self.subTest(relay=relay):
                args = hass_mcp.mcp_args(
                    {"home_assistant_mcp": False}, "/sample/bin/python3", relay=relay
                )
                self.assertEqual(args, [])

    def test_enabled_option_emits_json_config_pairs_for_chat_and_relay(self):
        python = '/sample with spaces/bin/python="example"'
        prefix = "mcp_servers.home_assistant_mcp."
        for relay in (False, True):
            with self.subTest(relay=relay):
                args = hass_mcp.mcp_args({}, python, relay=relay)
                explicit = hass_mcp.mcp_args(
                    {"home_assistant_mcp": True}, python, relay=relay
                )
                self.assertEqual(args, explicit)
                decoded = {}
                for flag, argument in zip(args[::2], args[1::2], strict=True):
                    key, separator, value = argument.partition("=")
                    self.assertEqual(flag, "--config")
                    self.assertEqual(separator, "=")
                    self.assertTrue(key.startswith(prefix), key)
                    setting = key.removeprefix(prefix)
                    self.assertNotIn(setting, decoded)
                    decoded[setting] = json.loads(value)
                self.assertEqual(decoded, hass_mcp.settings(python, relay=relay))
