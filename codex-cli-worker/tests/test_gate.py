"""The remote-control command check: the hook over a fixture of tool calls, the shared scan, the config watch, the marker."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gate"))
import gate_hook
import gate_rules
import remote_control as rc

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "gate_tool_calls.json").read_text(encoding="utf-8"))
HOOK = ROOT / "gate" / "gate_hook.py"
RULES = gate_rules.load_rules(ROOT / "policy" / "refused-domains.json")


def run_hook(payload, remote_on: bool, tmp: str, event: str = "") -> subprocess.CompletedProcess:
    """Run the hook script as Codex would: python -I, the event on stdin, the saved choice in a file."""
    state = Path(tmp) / "state.json"
    state.write_text(json.dumps({"enabled": remote_on}), encoding="utf-8")
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, "-I", str(HOOK), "--event", event or "PreToolUse", "--state", str(state)],
                          input=text, capture_output=True, text=True, timeout=30, check=False)


class HookFixtureTests(unittest.TestCase):
    """Every fixture case is refused or let through as written, run as a separate process."""

    def test_fixture_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for case in FIXTURE["cases"]:
                with self.subTest(case["name"]):
                    event = case["input"].get("hook_event_name", "") if isinstance(case["input"], dict) else ""
                    result = run_hook(case["input"], case["remote_control_on"], tmp, event)
                    if case["expect"] == "deny":
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertTrue(result.stderr.strip())
                        decision = json.loads(result.stdout)
                        out = decision["hookSpecificOutput"]
                        if event == "PermissionRequest":
                            self.assertEqual(out["decision"]["behavior"], "deny")
                        else:
                            self.assertEqual(out["permissionDecision"], "deny")
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(result.stdout, "")

    def test_fixture_covers_both_answers(self) -> None:
        answers = {case["expect"] for case in FIXTURE["cases"]}
        self.assertEqual(answers, {"allow", "deny"})


class HookStateTests(unittest.TestCase):
    """Whether remote control is on comes from the app's saved choice, failing closed."""

    def test_missing_state_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(gate_hook.remote_control_on(Path(tmp) / "none.json"))

    def test_unreadable_or_odd_state_is_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            for text in ("not json", "[]", '{"other": 1}', '{"enabled": true}'):
                state.write_text(text, encoding="utf-8")
                self.assertTrue(gate_hook.remote_control_on(state), text)
            state.write_text('{"enabled": false}', encoding="utf-8")
            self.assertFalse(gate_hook.remote_control_on(state))
            self.assertTrue(gate_hook.remote_control_on(Path(tmp)))  # a folder cannot be read

    def test_a_hook_without_its_rules_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alone = Path(tmp) / "sub" / "gate_hook.py"
            alone.parent.mkdir()
            alone.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", str(alone), "--state", str(Path(tmp) / "s.json")],
                                    input='{"tool_input": {"command": ["ls"]}}', capture_output=True, text=True,
                                    timeout=30, check=False)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_a_bad_command_line_refuses(self) -> None:
        result = subprocess.run([sys.executable, "-I", str(HOOK), "--no-such-option"], input="{}",
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 2)

    def test_a_judging_error_refuses(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            original = gate_rules.check_tool_call
            gate_rules.check_tool_call = lambda *a: 1 / 0
            try:
                code = gate_hook.main(["--state", str(Path(tmp) / "s.json")], io.StringIO("{}"), out, err)
            finally:
                gate_rules.check_tool_call = original
        self.assertEqual(code, 2)
        self.assertIn(gate_hook.UNREADABLE, err.getvalue())


class RulesTests(unittest.TestCase):
    """The shared refused-domain list and the line scan."""

    def test_file_is_never_looser_than_the_built_in_list(self) -> None:
        data = json.loads((ROOT / "policy" / "refused-domains.json").read_text(encoding="utf-8"))
        for key in gate_rules.LIST_KEYS:
            self.assertLessEqual(set(gate_rules.STRICT_RULES[key]), set(data[key]), key)
        for domain, kinds in gate_rules.STRICT_RULES["device_actions"].items():
            self.assertLessEqual(set(kinds), set(data["device_actions"][domain]), domain)

    def test_missing_or_shrunk_file_keeps_the_strict_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(gate_rules.load_rules(Path(tmp) / "none.json")["refused_domains"],
                             gate_rules.STRICT_RULES["refused_domains"])
            small = Path(tmp) / "small.json"
            small.write_text(json.dumps({"refused_domains": ["siren"], "device_actions": {"lock": []}}), encoding="utf-8")
            rules = gate_rules.load_rules(small)
            self.assertIn("lock", rules["refused_domains"])
            self.assertIn("siren", rules["refused_domains"])
            self.assertIn("unlock", rules["device_actions"]["lock"])

    def test_scan_reports_line_numbers_and_reasons(self) -> None:
        text = "- alias: x\n  actions:\n    - action: light.turn_on\n    - action: lock.unlock\n"
        found = gate_rules.find_refused(text, RULES)
        self.assertEqual([(f.line, f.reason) for f in found], [(4, "an action on lock")])

    def test_script_calls_resolve_to_refused_scripts(self) -> None:
        text = "    - action: script.leave_home\n    - action: script.turn_on\n      target:\n        entity_id: script.leave_home\n"
        self.assertEqual(gate_rules.find_refused(text, RULES), [])
        found = gate_rules.find_refused(text, RULES, {"leave_home"})
        self.assertEqual([f.line for f in found], [1, 4])

    def test_scenes_platforms_definitions_and_device_covers(self) -> None:
        cases = {
            "  entities:\n    lock.front_door:\n      state: unlocked\n": "a state for lock, which a scene can set",
            "  - platform: command_line\n    command: id\n": "a command_line platform, which runs on its own once loaded",
            "sensor:\n  - platform: scrape\n": "a scrape platform, which runs on its own once loaded",
            "rest_command:\n  x:\n    url: http://example.com\n": "a rest_command definition, which runs on its own once loaded",
            "  shell_command: !include shell.yaml\n": "a shell_command definition, which runs on its own once loaded",
            "    - domain: cover\n      type: open\n": "a device action on cover (open)",
            "    - action: cover.open_cover\n": "an action on cover",
            "    - action: script.announce_dinner\n": "a call to script.announce_dinner, whose name says it sends a message",
        }
        for text, reason in cases.items():
            with self.subTest(text):
                self.assertEqual([f.reason for f in gate_rules.find_refused(text, RULES)], [reason])

    def test_ordinary_lines_are_not_refused(self) -> None:
        text = ("- alias: lights\n  triggers:\n    - platform: state\n      entity_id: lock.front_door\n"
                "  actions:\n    - action: light.turn_on\n    - action: scene.turn_on\n      target:\n"
                "        entity_id: scene.evening\n    - action: script.lights_off\n")
        self.assertEqual(gate_rules.find_refused(text, RULES), [])

    def test_parse_patch_keeps_added_lines_per_file(self) -> None:
        patch = ("*** Begin Patch\n*** Update File: a.yaml\n@@\n-old\n+new\n*** Add File: b.yaml\n+x\n+y\n"
                 "*** Delete File: c.yaml\n*** End Patch\n")
        self.assertEqual(gate_rules.parse_patch(patch), [("a.yaml", "new"), ("b.yaml", "x\ny"), ("c.yaml", "")])


class ConfigWatchTests(unittest.TestCase):
    """The config watch reports refused actions added since its baseline, and keeps the baseline across restarts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config"
        self.config.mkdir()
        (self.config / "automations.yaml").write_text("- actions:\n    - action: lock.lock\n", encoding="utf-8")
        (self.config / "scripts.yaml").write_text(
            "goodnight:\n  sequence:\n    - action: lock.lock\nlights:\n  sequence:\n    - action: light.turn_off\n",
            encoding="utf-8")
        (self.config / ".storage").mkdir()
        (self.config / ".storage" / "x.yaml").write_text("- action: lock.unlock\n", encoding="utf-8")
        (self.config / "secrets.yaml").write_text("- action: lock.unlock\n", encoding="utf-8")
        self.watch = rc.ConfigWatch(self.config, Path(self.tmp.name) / "data" / "baseline.json",
                                    rules=lambda: RULES)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_existing_actions_are_the_baseline(self) -> None:
        self.watch.take_baseline()
        self.assertTrue(self.watch.has_baseline())
        self.assertEqual(self.watch.new_findings(), [])

    def test_added_action_is_found_and_a_move_is_not(self) -> None:
        self.watch.take_baseline()
        (self.config / "automations.yaml").write_text(
            "- alias: added\n  actions:\n    - action: notify.phone\n- actions:\n    - action: lock.lock\n",
            encoding="utf-8")
        found = self.watch.new_findings()
        self.assertEqual([(f["file"], f["line"], f["reason"]) for f in found],
                         [("automations.yaml", 3, "an action on notify")])

    def test_trigger_edit_on_a_block_that_locks_is_found(self) -> None:
        self.watch.take_baseline()
        (self.config / "automations.yaml").write_text(
            "- triggers:\n    - trigger: time\n      at: '03:00'\n  actions:\n    - action: lock.lock\n",
            encoding="utf-8")
        found = self.watch.new_findings()
        self.assertEqual([(f["line"], f["reason"]) for f in found], [(1, gate_rules.BLOCK_CHANGED)])

    def test_a_new_line_in_a_block_is_reported_once(self) -> None:
        self.watch.take_baseline()
        (self.config / "automations.yaml").write_text(
            "- actions:\n    - action: lock.lock\n    - action: lock.unlock\n", encoding="utf-8")
        self.assertEqual([f["reason"] for f in self.watch.new_findings()], ["an action on lock"])

    def test_written_by_a_shell_command_into_a_package(self) -> None:
        self.watch.take_baseline()
        (self.config / "packages").mkdir()
        (self.config / "packages" / "p.yaml").write_text(
            "automation:\n  - actions:\n      - action: \"{{ 'lock' }}.unlock\"\n", encoding="utf-8")
        self.assertEqual([f["reason"] for f in self.watch.new_findings()], ["a templated action"])

    def test_new_call_to_a_script_that_locks(self) -> None:
        self.watch.take_baseline()
        (self.config / "automations.yaml").write_text(
            "- actions:\n    - action: lock.lock\n- actions:\n    - action: script.goodnight\n    - action: script.lights\n",
            encoding="utf-8")
        found = self.watch.new_findings()
        self.assertEqual([f["line"] for f in found], [4])
        self.assertIn("script.goodnight", found[0]["reason"])

    def test_stores_and_secrets_are_not_read(self) -> None:
        self.assertEqual(sorted(p.name for p in self.watch.files()), ["automations.yaml", "scripts.yaml"])

    def test_restart_keeps_the_baseline(self) -> None:
        self.watch.take_baseline()
        (self.config / "automations.yaml").write_text("- actions:\n    - action: lock.lock\n    - action: tts.speak\n",
                                                     encoding="utf-8")
        again = rc.ConfigWatch(self.config, self.watch.baseline_path, rules=lambda: RULES)
        self.assertEqual(len(again.new_findings()), 1)

    def test_unreadable_baseline_reports_everything(self) -> None:
        self.watch.baseline_path.parent.mkdir(parents=True)
        self.watch.baseline_path.write_text("not json", encoding="utf-8")
        self.assertEqual(len(self.watch.new_findings()), 2)


class SupervisorWatchTests(unittest.TestCase):
    """Turn on takes the baseline; Turn off keeps it only while there is something to review; review clears it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.config = base / "config"
        self.config.mkdir()
        (self.config / "automations.yaml").write_text("- actions:\n    - action: light.turn_on\n", encoding="utf-8")
        self.watch = rc.ConfigWatch(self.config, base / "baseline.json", rules=lambda: RULES)
        self.sup = rc.Supervisor(codex_binary="codex", state_path=base / "rc.json", env_factory=dict,
                                 name_factory=lambda: "", gate=lambda: "", watch=self.watch, watch_interval=0.05)
        self.sup._launch = lambda: self.sup.snapshot()  # no process in these tests

    def tearDown(self) -> None:
        self.sup.save_enabled(False)
        self.watch.clear()
        self.sup.watch_wake.set()
        self.tmp.cleanup()

    def wait_for(self, count: int) -> dict:
        for _ in range(100):
            snap = self.sup.snapshot()
            if snap["config_findings_count"] == count:
                return snap
            self.sup.watch_wake.set()
            import time
            time.sleep(0.05)
        self.fail(f"findings never reached {count}: {self.sup.snapshot()['config_findings']}")

    def test_start_watch_stop_and_review(self) -> None:
        ok, _ = self.sup.start()
        self.assertTrue(ok)
        self.assertTrue(self.watch.has_baseline())
        (self.config / "automations.yaml").write_text("- actions:\n    - action: camera.turn_off\n", encoding="utf-8")
        snap = self.wait_for(1)
        self.assertEqual(snap["config_findings"][0]["file"], "automations.yaml")
        self.sup.stop()
        self.assertTrue(self.watch.has_baseline(), "an unreviewed change keeps the watch after Turn off")
        self.assertEqual(self.sup.snapshot()["config_findings_count"], 1)
        self.sup.review()
        self.assertFalse(self.watch.has_baseline())
        self.assertEqual(self.sup.snapshot()["config_findings_count"], 0)

    def test_clean_stop_forgets_the_baseline(self) -> None:
        self.sup.start()
        self.sup.stop()
        self.assertFalse(self.watch.has_baseline())

    def test_review_while_on_rebaselines(self) -> None:
        self.sup.start()
        (self.config / "automations.yaml").write_text("- actions:\n    - action: camera.turn_off\n", encoding="utf-8")
        self.wait_for(1)
        self.sup.review()
        self.assertTrue(self.watch.has_baseline())
        self.assertEqual(self.watch.new_findings(), [])

    def test_resume_without_baseline_takes_one(self) -> None:
        self.sup.save_enabled(True)
        self.sup.resume()
        self.assertTrue(self.watch.has_baseline())


class MarkerTests(unittest.TestCase):
    """The shipped marker allows Turn on only when the requirements hold and the hook passes its self-test."""

    def marker(self, tmp: str, hook: Path | None = None, requirements: str | None = None) -> Path:
        req = Path(tmp) / "requirements.toml"
        req.write_text(requirements if requirements is not None else (ROOT / "gate" / "requirements.toml").read_text(),
                       encoding="utf-8")
        hook = hook or HOOK
        marker = Path(tmp) / "gate.json"
        marker.write_text(json.dumps({"files": [str(req), str(hook)], "hook": str(hook), "requirements": str(req)}),
                          encoding="utf-8")
        return marker

    def test_shipped_files_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rc.gate_problem(self.marker(tmp)), "")

    def test_full_access_or_outside_hooks_block(self) -> None:
        for text in ('allowed_sandbox_modes = ["workspace-write", "danger-full-access"]\nallow_managed_hooks_only = true\n',
                     'allowed_sandbox_modes = ["workspace-write"]\n', "not = [toml"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(text):
                self.assertTrue(rc.gate_problem(self.marker(tmp, requirements=text)).startswith(rc.GATE_BROKEN))

    def test_a_hook_that_lets_everything_through_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hook = Path(tmp) / "hook.py"
            hook.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
            self.assertIn("let through", rc.gate_problem(self.marker(tmp, hook=hook)))

    def test_a_hook_that_refuses_everything_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hook = Path(tmp) / "hook.py"
            hook.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
            self.assertIn("refused a test request", rc.gate_problem(self.marker(tmp, hook=hook)))

    def test_shipped_marker_lists_the_dockerfile_paths(self) -> None:
        marker = json.loads((ROOT / "gate" / "remote-control-gate.json").read_text(encoding="utf-8"))
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(marker["hook"], marker["files"])
        self.assertIn(marker["requirements"], marker["files"])
        self.assertIn("/etc/codex/", dockerfile)
        self.assertIn("/usr/share/ha_codex/remote-control-gate.json", dockerfile)
        for path in marker["files"]:
            self.assertTrue(path.startswith(("/etc/codex/", "/usr/share/ha_codex/")), path)
        managed = (ROOT / "gate" / "managed_config.toml").read_text(encoding="utf-8")
        self.assertEqual(managed.count(marker["hook"]), 2)


class UndumpableWorkerTests(unittest.TestCase):
    """The worker marks itself not dumpable, in a child process here so the test runner is not changed."""

    @unittest.skipUnless(sys.platform.startswith("linux"), "prctl is Linux only")
    def test_the_flag_is_set(self) -> None:
        # Only the function is run (server.py starts threads on import); PR_GET_DUMPABLE is 3.
        code = (
            "import ctypes, sys;"
            "src = open(sys.argv[1] + '/server.py').read();"
            "start = src.index('PR_SET_DUMPABLE = 4'); end = src.index('def main()');"
            "ns = {}; exec(src[start:end], ns);"
            "assert ns['make_worker_undumpable']();"
            "libc = ctypes.CDLL(None); print(libc.prctl(3, 0, 0, 0, 0))"
        )
        result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1])],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
