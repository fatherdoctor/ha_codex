"""Refused categories (off by default, the refuse_categories option) and the plain-words confirmation."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import categories
import gate_rules
import test_approvals
from approvals import COMMAND_APPROVAL, FILE_APPROVAL
from test_server import server

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


def enforce(test: unittest.TestCase) -> None:
    """Turn the refuse_categories option on for one test."""
    patcher = patch.object(categories, "enforced", return_value=True)
    patcher.start()
    test.addCleanup(patcher.stop)


class RefusedRequestTests(test_approvals.ApprovalTests):
    """With refuse_categories on, through the approval bridge: refused at once, Codex declined, the person never asked."""

    def setUp(self):
        super().setUp()
        enforce(self)

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
        # Counted as a refused attempt, not as a prompt to the person.
        counts = server.count_new_prompts({}, self.records, {})
        self.assertEqual((counts["refused"], counts["total"]), (1, 0))
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


class ConfigFolderTest(unittest.TestCase):
    """A temporary /config and /data for the tests that read Home Assistant's files."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config"
        self.config.mkdir()
        self.store = Path(self.tmp.name) / "data" / "refused-edits.json"
        for target, name, value in ((categories, "CONFIG_ROOT", self.config),
                                    (categories, "REFUSED_EDITS_PATH", self.store), (server, "CONFIG_ROOT", self.config)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, rel: str, text: str) -> Path:
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class SharedListTests(unittest.TestCase):
    """One refused list and one scan for this check and the remote-control check (F10)."""

    def test_every_shared_domain_has_a_category(self) -> None:
        rules = gate_rules.load_rules()
        for domain in rules["refused_domains"] + rules["reaches_anything"]:
            self.assertIn(domain, categories._DOMAIN_TO_CATEGORY, domain)

    def test_configuration_findings_map_to_categories(self) -> None:
        cases = {"+  - action: shell_command.wipe": "commands",
                 "+sensor:\n+  - platform: command_line\n+    command: id": "commands",
                 "+  entities:\n+    lock.front_door:\n+      state: unlocked": "locks",
                 "+    - domain: mobile_app\n+      type: notify": "messages",
                 "+  - action: \"{{ x }}\"": "commands"}
        for diff, category in cases.items():
            with self.subTest(diff=diff):
                self.assertIn(category, categories.refused_categories(
                    "file_change", changes=[{"path": "configuration.yaml", "kind": "update", "diff": diff}]))

    def test_commands_on_reach_anything_domains(self) -> None:
        self.assertEqual(categories.refused_categories("command", command=f"{HA}/shell_command/wipe"), ["commands"])


class IndirectTests(ConfigFolderTest):
    """A script, scene or automation acts under its own name; its definition decides (F11)."""

    def setUp(self) -> None:
        super().setUp()
        self.write("scripts.yaml", "night_routine:\n  sequence:\n    - action: lock.lock\n"
                                   "wrapper:\n  sequence:\n    - action: script.night_routine\n"
                                   "lights_off:\n  sequence:\n    - action: light.turn_off\n")
        self.write("scenes.yaml", "- id: '1700000000'\n  name: Leaving\n  entities:\n    lock.front_door:\n"
                                  "      state: unlocked\n- id: '1700000001'\n  name: Movie night\n  entities:\n"
                                  "    light.tv: 'off'\n")
        self.write("automations.yaml", "- id: '1'\n  alias: Away mode\n  actions:\n    - action: alarm_control_panel.alarm_arm_away\n")

    def test_definitions_are_resolved(self) -> None:
        refused = categories.refused_definitions()
        self.assertEqual(sorted(refused), ["automation.1", "automation.away_mode", "scene.1700000000", "scene.leaving",
                                           "script.night_routine", "script.wrapper"])

    def test_commands_that_run_them_are_refused(self) -> None:
        for command in (f"{HA}/script/turn_on -d '{{\"entity_id\": \"script.night_routine\"}}'",
                        f"{HA}/script/wrapper", f"{HA}/scene/turn_on -d 'scene.leaving'",
                        f"{HA}/automation/trigger -d 'automation.away_mode'"):
            with self.subTest(command=command):
                self.assertEqual(categories.refused_categories("command", command=command), ["indirect"])
        for command in (f"{HA}/script/lights_off", f"{HA}/scene/turn_on -d 'scene.movie_night'"):
            with self.subTest(command=command):
                self.assertEqual(categories.refused_categories("command", command=command), [])

    def test_changes_that_call_them_are_refused(self) -> None:
        for diff in ("+    - action: script.night_routine", "+    - action: scene.turn_on\n+      target:\n"
                     "+        entity_id: scene.leaving"):
            with self.subTest(diff=diff):
                self.assertIn("indirect", categories.refused_categories(
                    "file_change", changes=[{"path": "automations.yaml", "kind": "update", "diff": diff}]))


class PlantedEditTests(ConfigFolderTest):
    """With refuse_categories on, an edit inside the sandbox needs no approval, so the end of the exchange checks it."""

    def setUp(self) -> None:
        super().setUp()
        enforce(self)
        self.run_dir = self.config / ".codex_runs" / "task"
        (self.run_dir / server.BACKUP_DIR).mkdir(parents=True)
        self.earlier = "- id: '1'\n  actions:\n    - action: lock.lock\n"

    def before(self, rel: str, text: str, copy: bool = True) -> dict:
        if copy:
            backup = self.run_dir / server.BACKUP_DIR / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(text, encoding="utf-8")
        return {rel: {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}}

    def test_added_lock_action_is_undone_and_an_old_one_is_not_new(self) -> None:
        before = self.before("automations.yaml", self.earlier)
        path = self.write("automations.yaml", self.earlier + "- id: '2'\n  actions:\n    - action: lock.unlock\n")
        results = server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before)
        self.assertEqual([(r["path"], r["undone"], [f["line"] for f in r["findings"]]) for r in results],
                         [("automations.yaml", "restored", [6])])
        self.assertEqual(path.read_text(encoding="utf-8"), self.earlier)
        self.assertEqual(categories.standing_refused_edits(), [])
        self.assertIn("earlier version back", server.refused_edits_details(results))

    def test_a_new_file_is_removed(self) -> None:
        path = self.write("packages/new.yaml", "shell_command:\n  wipe: rm -rf /config\n")
        results = server.refuse_planted_edits(self.run_dir, {"added": ["packages/new.yaml"]}, {})
        self.assertEqual(results[0]["undone"], "removed")
        self.assertFalse(path.exists())

    def test_light_edits_pass(self) -> None:
        before = self.before("automations.yaml", self.earlier)
        self.write("automations.yaml", self.earlier + "- id: '2'\n  actions:\n    - action: light.turn_on\n")
        self.assertEqual(server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before), [])

    def test_without_a_checked_copy_the_lines_stand_and_reloads_are_refused(self) -> None:
        before = self.before("automations.yaml", self.earlier, copy=False)
        planted = self.earlier + "- id: '2'\n  actions:\n    - action: lock.unlock\n"
        self.write("automations.yaml", planted)
        results = server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before)
        self.assertEqual(results[0]["undone"], "")
        self.assertIn("could not be put back", server.refused_edits_details(results))
        self.assertEqual(categories.refused_categories("command", command=f"{HA}/automation/reload"), ["planted"])
        self.assertEqual(categories.refused_categories("command", command="ha core restart"), ["planted"])
        self.assertEqual(categories.refused_categories("command", command=f"{HA}/light/turn_on"), [])
        # Once the lines are gone, reloads are allowed again.
        self.write("automations.yaml", "- id: '1'\n  actions:\n    - action: light.turn_on\n")
        self.assertEqual(categories.refused_categories("command", command=f"{HA}/automation/reload"), [])
        self.assertFalse(self.store.exists())

    def test_a_moved_line_is_not_new(self) -> None:
        self.assertEqual(categories.new_refused_lines(self.earlier, "# moved\n" + self.earlier), [])

    def test_a_new_trigger_on_a_lock_automation_is_undone(self) -> None:
        """The unlock line stays as it was; its automation's block changed, so the edit is refused (F12)."""
        earlier = ("- id: '1'\n  triggers:\n    - trigger: state\n      entity_id: person.owner\n      to: home\n"
                   "  actions:\n    - action: lock.unlock\n")
        before = self.before("automations.yaml", earlier)
        path = self.write("automations.yaml", earlier.replace(
            "    - trigger: state\n      entity_id: person.owner\n      to: home\n", "    - trigger: time\n      at: '03:00:00'\n"))
        results = server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before)
        self.assertEqual([(r["undone"], [(f["line"], f["reason"]) for f in r["findings"]]) for r in results],
                         [("restored", [(1, gate_rules.BLOCK_CHANGED)])])
        self.assertEqual(path.read_text(encoding="utf-8"), earlier)

    def test_an_unchanged_lock_automation_beside_a_light_edit_passes(self) -> None:
        before = self.before("automations.yaml", self.earlier)
        self.write("automations.yaml", "- id: '0'\n  actions:\n    - action: light.turn_on\n" + self.earlier)
        self.assertEqual(server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before), [])

    def test_a_new_refused_line_is_reported_once(self) -> None:
        found = categories.new_refused_lines("", self.earlier)
        self.assertEqual(categories.new_refused_blocks("", self.earlier, found=found), [])
        self.assertEqual(len(categories.new_refused_blocks("", self.earlier)), 1)


class YamlCopyTests(PlantedEditTests):
    """The YAML files are saved before every exchange, so a checked earlier copy always exists (F13)."""

    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(server, "get_run_dir", return_value=self.run_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def manifest(self, *rels: str) -> dict:
        return {rel: {"sha256": hashlib.sha256((self.config / rel).read_bytes()).hexdigest()} for rel in rels}

    def test_a_light_automation_in_a_house_with_a_lock_automation_passes(self) -> None:
        self.write("automations.yaml", self.earlier)
        self.write(".storage/core.entity_registry", "{}")
        self.write("secrets.yaml", "key: value\n")
        before = self.manifest("automations.yaml", ".storage/core.entity_registry", "secrets.yaml")
        self.assertEqual(server.copy_config_yaml("task", before), 1)
        self.write("automations.yaml", self.earlier + "- id: '2'\n  actions:\n    - action: light.turn_on\n")
        self.assertEqual(server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before), [])
        self.assertFalse(self.store.exists())

    def test_a_planted_edit_is_restored_from_the_copy(self) -> None:
        path = self.write("automations.yaml", self.earlier)
        before = self.manifest("automations.yaml")
        server.copy_config_yaml("task", before)
        self.write("automations.yaml", self.earlier + "- id: '2'\n  actions:\n    - action: lock.unlock\n")
        results = server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before)
        self.assertEqual(results[0]["undone"], "restored")
        self.assertEqual(path.read_text(encoding="utf-8"), self.earlier)
        self.assertFalse(self.store.exists())

    def test_a_file_that_changed_after_the_manifest_is_not_copied(self) -> None:
        self.write("automations.yaml", self.earlier)
        before = self.manifest("automations.yaml")
        self.write("automations.yaml", self.earlier + "# later\n")
        self.assertEqual(server.copy_config_yaml("task", before), 0)


class ReviewRefusedEditsTests(PlantedEditTests):
    """An administrator can say that standing refused lines are a person's own, which allows reloads again."""

    def stand(self) -> None:
        before = self.before("automations.yaml", self.earlier, copy=False)
        self.write("automations.yaml", self.earlier + "- id: '2'\n  actions:\n    - action: lock.unlock\n")
        results = server.refuse_planted_edits(self.run_dir, {"changed": ["automations.yaml"]}, before)
        self.assertIn("Mark reviewed", server.refused_edits_details(results))
        self.assertEqual(categories.refused_categories("command", command=f"{HA}/automation/reload"), ["planted"])

    def test_mark_reviewed_allows_reloads(self) -> None:
        self.stand()
        self.assertEqual(len(categories.mark_refused_edits_reviewed()), 2)
        self.assertEqual(categories.refused_categories("command", command=f"{HA}/automation/reload"), [])

    def test_only_an_ingress_administrator_can_mark_them_reviewed(self) -> None:
        self.stand()
        client = server.app.test_client()
        with (patch.object(server, "api_token", return_value="worker-secret"),
              patch.object(server, "lookup_ha_users", return_value=test_approvals.HA_USERS),
              patch.object(server, "admin_cache", {})):
            listed = client.get("/refused-edits", headers={"Authorization": "Bearer worker-secret"})
            self.assertEqual(len(listed.get_json()["edits"]), 2)
            token_only = client.post("/refused-edits/review", json={}, headers={"Authorization": "Bearer worker-secret"})
            self.assertEqual(token_only.status_code, 403)
            guest = client.post("/refused-edits/review", json={}, **test_approvals.ingress_as("u" * 32))
            self.assertEqual(guest.status_code, 403)
            self.assertIn("administrator", guest.get_json()["error"])
            self.assertEqual(len(categories.standing_refused_edits()), 2)
            owner = client.post("/refused-edits/review", json={}, **test_approvals.ingress_as())
        self.assertEqual((owner.status_code, owner.get_json()["reviewed"]), (200, 2))
        self.assertEqual(categories.standing_refused_edits(), [])


# The inherited ApprovalTests cases already run in test_approvals; run only the new ones here.
for _name in [name for name in dir(test_approvals.ApprovalTests) if name.startswith("test_")]:
    if _name not in RefusedRequestTests.__dict__:
        setattr(RefusedRequestTests, _name, None)


if __name__ == "__main__":
    unittest.main()


class OptionTests(unittest.TestCase):
    """The refuse_categories option: off unless the options file says true."""

    def options(self, text: str | None) -> None:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "options.json"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        patcher = patch.object(categories, "OPTIONS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_unless_set_to_true(self) -> None:
        for text, expected in ((None, False), ("{}", False), ('{"refuse_categories": false}', False),
                               ('{"refuse_categories": "yes"}', False), ("not json", False), ("[]", False),
                               ('{"refuse_categories": true}', True)):
            with self.subTest(text=text):
                self.options(text)
                self.assertIs(categories.enforced(), expected)

    def test_the_default_is_off_in_the_app_options(self) -> None:
        self.assertIs(server.DEFAULT_OPTIONS["refuse_categories"], False)
        config = (Path(server.__file__).parent / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("  refuse_categories: false\n", config)
        self.assertIn("  refuse_categories: bool\n", config)

    def test_off_keeps_only_the_policy(self) -> None:
        self.options(None)
        self.assertEqual(categories.in_force(["locks", "messages", "policy", "planted"]), ["policy"])
        self.options('{"refuse_categories": true}')
        self.assertEqual(categories.in_force(["locks", "policy"]), ["locks", "policy"])

    def test_the_house_rules_carry_the_list(self) -> None:
        rules = (Path(server.__file__).parent / "policy" / "house-rules.md").read_text(encoding="utf-8")
        for words in ("locks", "the alarm", "garage doors", "cameras", "never send a message", "publish",
                      "ignore or set aside these rules"):
            self.assertIn(words, rules)


class OffByDefaultRequestTests(test_approvals.ApprovalTests):
    """With refuse_categories off (the default): a category request asks the person, a policy change is still refused."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(categories, "enforced", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_lock_command_asks_with_its_plain_words(self):
        approval_id = self.request(request_id=11, command=f"{HA}/lock/unlock -d 'lock.front_door'")
        record = self.records[-1]
        self.assertEqual((record["status"], record["can_allow"]), ("pending", True))
        self.assertNotIn("refused", record)
        self.assertTrue(record["confirmation"]["effect"])
        self.session.decide(approval_id, "deny")
        self.assertEqual(self.writer.messages()[-1], {"id": 11, "result": {"decision": "decline"}})

    def test_a_policy_command_is_still_refused(self):
        self.request(request_id=12, command="cat /usr/share/ha_codex/policy.json > /tmp/x")
        record = self.records[-1]
        self.assertEqual((record["status"], record["refused"]), ("refused", ["policy"]))
        self.assertEqual(self.writer.messages()[-1], {"id": 12, "result": {"decision": "decline"}})


class OffByDefaultPlantedEditTests(ConfigFolderTest):
    """With refuse_categories off, the end-of-exchange scan does not run and nothing is undone."""

    def test_an_added_lock_action_stays(self) -> None:
        patcher = patch.object(categories, "enforced", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        run_dir = self.config / ".codex_runs" / "task"
        run_dir.mkdir(parents=True)
        text = "- id: '2'\n  actions:\n    - action: lock.unlock\n"
        path = self.write("automations.yaml", text)
        self.assertEqual(server.refuse_planted_edits(run_dir, {"added": ["automations.yaml"]}, {}), [])
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(categories.standing_refused_edits(), [])
