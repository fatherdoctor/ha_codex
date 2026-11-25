"""Parity: no category settings or bundled house practice; device commands go through the Home Assistant tools."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class ParityTests(unittest.TestCase):
    def test_category_settings_removed(self):
        import yaml

        config = yaml.safe_load((ROOT / "config.yaml").read_text())
        for options in (config["options"], config["schema"], server.DEFAULT_OPTIONS):
            self.assertNotIn("refuse_categories", options)
        self.assertNotIn(
            "refuse_categories", (ROOT / "translations/en.yaml").read_text()
        )

    def test_no_house_practice_is_bundled(self):
        self.assertFalse((ROOT / "policy/house-rules.md").exists())
        self.assertIn("/addon_config/AGENTS.md", (ROOT / "server.py").read_text())


class DeviceCommandSpeedTests(unittest.TestCase):
    """A plain device command finds its entity through the Home Assistant tools, not through files."""

    STATES = [
        {
            "entity_id": "light.office_overhead",
            "state": "off",
            "attributes": {"friendly_name": "Office Overhead", "brightness": None},
        },
        {
            "entity_id": "light.office_overhead_left_bulb",
            "state": "off",
            "attributes": {"friendly_name": "Office Overhead Left Bulb"},
        },
        {
            "entity_id": "switch.office_overhead_fan",
            "state": "on",
            "attributes": {"friendly_name": "Office Overhead Fan"},
        },
        {
            "entity_id": "light.kitchen_ceiling",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen Ceiling"},
        },
        {
            "entity_id": "sensor.office_overhead_api_key",
            "state": "synthetic-value",
            "attributes": {"friendly_name": "Office Overhead Key"},
        },
        {
            "entity_id": "input_text.office_overhead_note",
            "state": "synthetic-value",
            "attributes": {"mode": "password"},
        },
        {
            "entity_id": "light.unnamed_office_overhead",
            "state": "on",
            "attributes": None,
        },
        "not a state row",
    ]

    def find(self, **arguments):
        import verification

        engine = verification.Verification(Mock(ha_token=lambda: "synthetic"))
        with patch.object(
            verification,
            "fetch_states",
            return_value=json.loads(json.dumps(self.STATES)),
        ) as fetch:
            result = engine.find(arguments)
        fetch.assert_called_once_with("/states", "synthetic")
        return result

    def test_rules_send_a_device_command_to_the_home_assistant_tools(self):
        rules = server.PAGE_INSTRUCTIONS
        line = next(
            line
            for line in rules.splitlines()
            if line.startswith("For a device command")
        )
        # One call by name that acts and reads back (CX-SPEED cut 1); find only when no entity fits.
        self.assertIn(
            "make one home_assistant_call call with the service, the name the user gave",
            line,
        )
        self.assertIn('"name":"kitchen ceiling"', line)
        self.assertIn(
            "ask the owner which one and call again with that target.entity_id", line
        )
        self.assertLess(
            line.index("home_assistant_call call"),
            line.index("home_assistant.verify find"),
        )
        self.assertNotIn("home_assistant.verify entity", line)
        self.assertNotIn("home_assistant_actions", rules)
        self.assertIn(
            "Do not search files under /homeassistant or .storage or run shell commands to find an entity",
            line,
        )
        self.assertIn(
            '{"operation":"find","name":"kitchen ceiling","domain":"light"}', rules
        )

    def test_rules_no_longer_ask_for_the_owner_file_in_each_message(self):
        rules = server.PAGE_INSTRUCTIONS
        self.assertNotIn("check for it in each message", rules)
        self.assertIn(
            "Owner workspace instructions: if /addon_config/AGENTS.md exists, follow it.",
            rules,
        )
        self.assertIn(
            "read the file again only when its owner_instructions text says to", rules
        )

    def test_exchange_info_says_when_the_owner_file_changed_or_that_it_is_gone(self):
        import os

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "AGENTS.md"
            with patch.object(server, "AGENTS_PATH", target):
                self.assertIn(
                    f"There is no {target} now",
                    server.exchange_info("chat", None)["owner_instructions"],
                )
                target.write_text("Synthetic owner rules")
                os.utime(target, (1_800_000_000, 1_800_000_000))
                first = server.exchange_info("chat", None)["owner_instructions"]
                self.assertIn("last changed at 2027-01-15T08:00:00Z", first)
                self.assertEqual(
                    first, server.exchange_info("chat", None)["owner_instructions"]
                )
                os.utime(target, (1_800_000_060, 1_800_000_060))
                self.assertNotEqual(
                    first, server.exchange_info("chat", None)["owner_instructions"]
                )

    def test_find_returns_the_entity_for_the_words_a_person_uses(self):
        result = self.find(name="Office overhead", domain="light")
        self.assertEqual(result["status"], "observed")
        self.assertEqual(
            result["matches"][0],
            {
                "entity_id": "light.office_overhead",
                "name": "Office Overhead",
                "state": "off",
            },
        )
        self.assertEqual(
            [row["entity_id"] for row in result["matches"]],
            [
                "light.office_overhead",
                "light.unnamed_office_overhead",
                "light.office_overhead_left_bulb",
            ],
        )
        self.assertEqual((result["total_matches"], result["truncated"]), (3, False))

    def test_find_without_a_domain_covers_every_domain_and_hides_credentials(self):
        ids = [row["entity_id"] for row in self.find(name="overhead office")["matches"]]
        # The device itself comes before the entities that add a word to its name.
        self.assertEqual(ids[0], "light.office_overhead")
        self.assertIn("switch.office_overhead_fan", ids)
        self.assertNotIn("sensor.office_overhead_api_key", ids)
        self.assertNotIn("input_text.office_overhead_note", ids)
        self.assertNotIn("synthetic-value", json.dumps(self.find(name="overhead")))

    def test_find_ranks_a_renamed_light_above_its_siblings(self):
        light = {
            "entity_id": "light.hue_color_lamp_3",
            "state": "off",
            "attributes": {"friendly_name": "Office Overhead"},
        }
        siblings = [
            {
                "entity_id": f"sensor.office_overhead_status_{n}",
                "state": "on",
                "attributes": {"friendly_name": "Office Overhead Status"},
            }
            for n in range(12)
        ]
        with patch.object(self, "STATES", [*siblings, light]):
            result = self.find(name="Office Overhead")
        self.assertEqual(
            result["matches"][0],
            {
                "entity_id": light["entity_id"],
                "name": "Office Overhead",
                "state": "off",
            },
        )
        self.assertEqual(
            (len(result["matches"]), result["total_matches"], result["truncated"]),
            (10, 13, True),
        )

    def test_find_accepts_words_split_across_name_and_entity_id(self):
        sensor = {
            "entity_id": "sensor.office_status",
            "state": "on",
            "attributes": {"friendly_name": "Overhead"},
        }
        with patch.object(self, "STATES", [sensor]):
            result = self.find(name="Office Overhead")
        self.assertEqual(
            [row["entity_id"] for row in result["matches"]], [sensor["entity_id"]]
        )

    def test_find_caps_its_answer_and_reports_no_match(self):
        import verification

        many = [
            {"entity_id": f"light.sample_{n:02d}", "state": "on", "attributes": {}}
            for n in range(25)
        ]
        with patch.object(self, "STATES", many):
            result = self.find(name="sample")
        self.assertEqual(len(result["matches"]), verification.MAX_FIND_MATCHES)
        self.assertEqual((result["total_matches"], result["truncated"]), (25, True))
        self.assertEqual(self.find(name="garage")["matches"], [])
        self.assertIn("No entity matches", self.find(name="garage")["message"])

    def test_find_refuses_bad_arguments_before_any_read(self):
        import verification

        engine = verification.Verification(Mock(ha_token=lambda: "synthetic"))
        with patch.object(verification, "fetch_states") as fetch:
            for arguments in (
                {},
                {"name": ""},
                {"name": "..."},
                {"name": "x" * 121},
                {"name": 7},
                {"name": "office", "domain": "Light.x"},
                {"name": "office", "domain": 5},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    engine.find(arguments)
        fetch.assert_not_called()

    def test_find_is_offered_by_the_tool_and_stays_a_read(self):
        import verification_mcp

        schema = verification_mcp.TOOL["inputSchema"]["properties"]
        self.assertIn("find", schema["operation"]["enum"])
        self.assertIn("name", schema)
        self.assertIn("No service calls", verification_mcp.TOOL["description"])
        self.assertIn("do not search files", verification_mcp.TOOL["description"])


class ApprovalParityTests(unittest.TestCase):
    def test_preserved_action_confirmation_describes_target_and_effect(self):
        import approvals

        record = {
            "kind": "command",
            "command": "/api/services/light/turn_on -d light.sample",
            "cwd": "/homeassistant",
            "can_allow": True,
            "changes": [],
        }
        text = approvals.confirmation(record)
        self.assertEqual(text["what"], "Run the Home Assistant action light.turn_on.")
        self.assertEqual(text["where"], "light.sample.")
        self.assertEqual(text["effect"], "This turns on light.sample.")
        record["command"] = "/api/services/automation/reload"
        self.assertEqual(
            approvals.confirmation(record)["what"], "Reload the automations."
        )

    def test_preserved_file_confirmation_explains_when_changes_apply(self):
        import approvals

        record = {
            "kind": "file_change",
            "can_allow": True,
            "changes": [
                {"path": "automations.yaml", "kind": "update"},
                {"path": "scenes.yaml", "kind": "add"},
            ],
        }
        text = approvals.confirmation(record)
        self.assertEqual(text["what"], "Create and change 2 files.")
        self.assertIn(
            "automations change once automations are reloaded", text["effect"]
        )
        self.assertIn("scenes change once scenes are reloaded", text["effect"])

    def test_protected_request_confirmation_does_not_offer_a_change(self):
        import approvals

        record = {
            "kind": "command",
            "command": "edit protected policy",
            "cwd": "/homeassistant",
            "can_allow": False,
            "changes": [],
            "note": "Protected policy cannot be changed.",
        }
        self.assertTrue(
            approvals.confirmation(record)["effect"].startswith(
                "This request cannot be allowed here."
            )
        )

    def test_device_command_has_plain_words_and_requires_its_own_decision(self):
        import test_approvals as fixtures

        fixture = fixtures.ApprovalTests("runTest")
        fixture.setUp()
        try:
            approval_id = fixture.request(
                command="ha services call lock unlock", cwd="/homeassistant"
            )
            record = fixture.records[-1]
            self.assertTrue(record["can_allow"])
            self.assertEqual(record["status"], "pending")
            self.assertEqual(set(record["confirmation"]), {"what", "where", "effect"})
            self.assertTrue(all(record["confirmation"].values()))
            self.assertNotEqual(
                fixture.writer.messages()[-1].get("result"), {"decision": "accept"}
            )
            fixture.session.decide(approval_id, "allow")
            self.assertEqual(
                fixture.writer.messages()[-1]["result"], {"decision": "accept"}
            )
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
