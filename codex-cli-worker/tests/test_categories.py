"""Refused categories and the plain-words confirmation (CX-B5): enforced on the action, outside the model."""

from __future__ import annotations

import unittest

import categories
import test_approvals
from approvals import COMMAND_APPROVAL, FILE_APPROVAL

HA = "curl -s -X POST -H 'Authorization: Bearer $HA_TOKEN' $HA_URL/api/services"


class RefusedCommandTests(unittest.TestCase):
    """Each refused category is found in the command text, however Codex describes it."""

    def assertRefused(self, command: str, category: str) -> None:
        """The command reaches exactly this category, among any others."""
        self.assertIn(category, categories.refused_categories("command", command=command), command)

    def test_locks_alarm_garage_and_cameras(self) -> None:
        """Service calls, websocket calls and entity ids in those domains are refused."""
        cases = {
            "locks": [f"{HA}/lock/unlock -d '{{\"entity_id\": \"lock.front_door\"}}'",
                      "python3 ws.py '{\"type\": \"call_service\", \"domain\": \"lock\", \"service\": \"lock\"}'",
                      "curl $HA_URL/api/states/lock.back_door"],
            "alarm": [f"{HA}/alarm_control_panel/alarm_disarm", f"{HA}/alarmo/arm -d '{{\"mode\": \"away\"}}'"],
            "garage": [f"{HA}/cover/open_cover -d '{{\"entity_id\": \"cover.garage_door\"}}'",
                       "curl $HA_URL/api/states/cover.side_gate"],
            "cameras": ["curl -o snap.jpg $HA_URL/api/camera_proxy/camera.porch",
                        f"{HA}/camera/snapshot -d '{{\"entity_id\": \"camera.yard\"}}'"],
        }
        for category, commands in cases.items():
            for command in commands:
                with self.subTest(command=command):
                    self.assertRefused(command, category)

    def test_messages_to_people(self) -> None:
        """Notify and text-to-speech actions, messaging scripts, and mail or chat services are refused."""
        for command in (f"{HA}/notify/mobile_app_phone -d '{{\"message\": \"hi\"}}'",
                        f"{HA}/tts/speak -d '{{\"entity_id\": \"tts.cloud\"}}'",
                        f"{HA}/script/turn_on -d '{{\"entity_id\": \"script.notify_family\"}}'",
                        "echo hi | sendmail neighbor@example.invalid",
                        "curl -X POST https://api.twilio.com/2010-04-01/Accounts/x/Messages.json",
                        "curl -d hi https://ntfy.sh/topic",
                        "curl -X POST https://hooks.slack.com/services/x"):
            with self.subTest(command=command):
                self.assertRefused(command, "messages")

    def test_publishing(self) -> None:
        """A push, a release, a pull request, a package upload or a write to GitHub's API is refused."""
        for command in ("git push origin main", "git -C /config push", "gh release create v1.0",
                        "gh pr create --fill", "gh issue comment 3 -b hi", "npm publish", "twine upload dist/*",
                        "docker push me/image", "gh api -X POST repos/o/r/issues",
                        "curl -X POST https://api.github.com/repos/o/r/issues"):
            with self.subTest(command=command):
                self.assertRefused(command, "publishing")

    def test_policy_and_settings(self) -> None:
        """This app's options, its policy files and its rules cannot be reached by a command."""
        for command in ("curl -X POST http://supervisor/addons/self/options -d '{}'", "ha apps options codex",
                        "cp mine.json /usr/share/ha_codex/policy.json", "echo '{}' > /data/options.json",
                        "sed -i d /data/codex-home/AGENTS.md"):
            with self.subTest(command=command):
                self.assertRefused(command, "policy")

    def test_lights_switches_scenes_and_ordinary_work_pass(self) -> None:
        """The device actions the app is for, reloads, reading and git commits are not refused."""
        for command in (f"{HA}/light/turn_on -d '{{\"entity_id\": \"light.kitchen\"}}'",
                        f"{HA}/switch/turn_off -d '{{\"entity_id\": \"switch.fan_plug\"}}'",
                        f"{HA}/scene/turn_on -d '{{\"entity_id\": \"scene.movie_night\"}}'",
                        f"{HA}/automation/reload", "curl $HA_URL/api/states/sensor.outdoor_temperature",
                        "cat lock.json", "python3 -c 'import threading; threading.Lock()'",
                        "git -C /config commit -am 'Kitchen lights'", "git log --oneline", "ls /config/camera"):
            with self.subTest(command=command):
                self.assertEqual(categories.refused_categories("command", command=command), [])


class RefusedFileChangeTests(unittest.TestCase):
    """A configuration change may not add an action in a refused category."""

    def change(self, diff: str, path: str = "automations.yaml") -> list[str]:
        """Return the refused categories of one change to this path."""
        return categories.refused_categories("file_change", changes=[{"path": path, "kind": "update", "diff": diff}])

    def test_added_actions_in_refused_domains(self) -> None:
        """An automation or script that would unlock, disarm, open the garage, snapshot or message is refused."""
        cases = {"locks": "+    - action: lock.unlock", "alarm": "+  - service: alarm_control_panel.alarm_disarm",
                 "garage": "+      action: cover.open_cover", "cameras": "+  - action: camera.snapshot",
                 "messages": "+    - action: notify.mobile_app_phone"}
        for category, line in cases.items():
            with self.subTest(category=category):
                self.assertEqual(self.change("@@ -1 +1,2 @@\n alias: x\n" + line), [category])
        self.assertEqual(self.change("+  - action: script.announce_dinner", "scripts.yaml"), ["messages"])
        # A new file is its whole text.
        self.assertEqual(self.change("action: lock.lock\n", "packages/new.yaml"), ["locks"])

    def test_removed_lines_and_conditions_pass(self) -> None:
        """Taking such an action out, or reading a lock in a condition, is not refused; lights are allowed."""
        self.assertEqual(self.change("-    - action: lock.unlock\n+    - action: light.turn_off"), [])
        self.assertEqual(self.change("+  - condition: state\n+    entity_id: lock.front_door\n+    state: locked"), [])


class ConfirmationTests(unittest.TestCase):
    """What changes, where, and the effect, in plain words."""

    def test_device_action_names_the_action_the_target_and_the_effect(self) -> None:
        """A light action says what it turns on, by entity id."""
        text = categories.confirmation("command", command=f"{HA}/light/turn_on -d '{{\"entity_id\": \"light.kitchen\"}}'")
        self.assertEqual(text, {"what": "Run the Home Assistant action light.turn_on.", "where": "light.kitchen.",
                                "effect": "This turns on light.kitchen."})
        self.assertEqual(categories.confirmation("command", command=f"{HA}/scene/turn_on -d 'scene.movie'")["effect"],
                         "This applies scene.movie.")
        self.assertEqual(categories.confirmation("command", command=f"{HA}/light/turn_off")["where"],
                         "Every light the action reaches.")

    def test_reload_and_other_commands(self) -> None:
        """A reload says nothing else runs; any other command says where and that only it runs."""
        self.assertEqual(categories.confirmation("command", command=f"{HA}/automation/reload")["what"],
                         "Reload the automations.")
        text = categories.confirmation("command", command="pip download x", cwd="/config", network="pypi.org")
        self.assertEqual(text["where"], "In /config, reaching pypi.org.")

    def test_file_changes_say_which_files_and_when_they_take_effect(self) -> None:
        """Create, change or delete; the paths; one effect per kind of file."""
        text = categories.confirmation("file_change", changes=[
            {"path": "automations.yaml", "kind": "update", "diff": "+x"},
            {"path": "scenes.yaml", "kind": "add", "diff": "x"},
            {"path": ".storage/lovelace.kitchen", "kind": "update", "diff": "+x"}])
        self.assertEqual(text["what"], "Create and change 3 files.")
        self.assertEqual(text["where"], "automations.yaml, scenes.yaml, .storage/lovelace.kitchen.")
        self.assertIn("automations change once automations are reloaded", text["effect"])
        self.assertIn("A dashboard changes", text["effect"])

    def test_refused_request_says_nothing_happens_and_why(self) -> None:
        """The effect line of a refused request is the refusal, in one plain sentence per category."""
        text = categories.confirmation("command", command=f"{HA}/lock/unlock", refused=["locks"])
        self.assertTrue(text["effect"].startswith("Not allowed, so nothing happens. This app never controls locks."))


class RefusedRequestTests(test_approvals.ApprovalTests):
    """Through the approval bridge: refused at once, Codex declined, the person never asked."""

    def test_refused_command_is_declined_at_once_and_never_waits(self):
        """A lock action is declined to Codex immediately, recorded as refused, and has no live request."""
        approval_id = self.request(request_id=7, command=f"{HA}/lock/unlock -d 'lock.front_door'",
                                   reason="Unlock the door for the guest")
        record = self.records[-1]
        self.assertEqual((record["status"], record["can_allow"], record["refused"]), ("refused", False, ["locks"]))
        self.assertIn("never controls locks", record["note"])
        self.assertIn("never controls locks", record["confirmation"]["effect"])
        self.assertEqual(self.writer.messages()[-1], {"id": 7, "result": {"decision": "decline"}})
        self.assertIn("Refused by this app's rules", self.events[-1]["message"])
        for decision in ("allow", "deny"):
            with self.assertRaises(LookupError):
                self.session.decide(approval_id, decision)

    def test_refused_file_change_is_declined(self):
        """An automation change that adds a notify action is refused before anyone sees an Allow button."""
        self.session.receive({"method": "item/started", "params": {
            "threadId": "thread", "turnId": "turn", "item": {"id": "item", "type": "fileChange",
                "changes": [{"path": "automations.yaml", "kind": {"type": "update"},
                             "diff": "@@\n+  - action: notify.mobile_app_phone"}]}}})
        self.request(method=FILE_APPROVAL, request_id=8)
        self.assertEqual(self.records[-1]["status"], "refused")
        self.assertEqual(self.records[-1]["refused"], ["messages"])
        self.assertEqual(self.writer.messages()[-1], {"id": 8, "result": {"decision": "decline"}})

    def test_allowed_action_waits_with_its_confirmation(self):
        """A light action waits for the person, with the three plain lines on the record."""
        approval_id = self.request(request_id=9, command=f"{HA}/light/turn_on -d '{{\"entity_id\": \"light.kitchen\"}}'")
        record = self.records[-1]
        self.assertEqual(record["status"], "pending")
        self.assertTrue(record["can_allow"])
        self.assertEqual(record["confirmation"]["effect"], "This turns on light.kitchen.")
        self.session.decide(approval_id, "allow")
        self.assertEqual(self.writer.messages()[-1], {"id": 9, "result": {"decision": "accept"}})


# The inherited ApprovalTests cases already run in test_approvals; run only the new ones here.
for _name in [name for name in dir(test_approvals.ApprovalTests) if name.startswith("test_")]:
    if _name not in RefusedRequestTests.__dict__:
        setattr(RefusedRequestTests, _name, None)


if __name__ == "__main__":
    unittest.main()
