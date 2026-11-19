"""The compatibility drivers in tests/compat: settings, the lab check, exit codes and each step's pass rule.

The drivers run against a lab Home Assistant; these tests use a stand-in lab and recorded answers only.
"""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

COMPAT = Path(__file__).resolve().parents[2] / "tests" / "compat"
sys.path.insert(0, str(COMPAT))

import cx02_sign_in  # noqa: E402
import cx03_chat_task  # noqa: E402
import cx04_file_edit  # noqa: E402
import lab as compat  # noqa: E402


def run(step, environment):
    """Run a step through run_step with the given environment; return its exit code and its last line."""
    out = io.StringIO()
    with (
        mock.patch.dict(os.environ, environment, clear=True),
        mock.patch.object(sys, "argv", ["driver", "--core", "2026.12.0"]),
        redirect_stdout(out),
        self_exit() as code,
    ):
        compat.run_step("test", step)
    return code[0], out.getvalue().strip().splitlines()[-1]


class self_exit:
    """Catch sys.exit and keep its code."""

    def __enter__(self):
        self.code = [None]
        return self.code

    def __exit__(self, kind, value, traceback):
        if kind is SystemExit:
            self.code[0] = value.code
            return True
        return False


class StandInLab(compat.Lab):
    """A lab that answers from a dict instead of the network."""

    def __init__(self, up=True, marker="on", store=None):
        super().__init__(
            "http://lab.invalid:8123", "stand-in", compat.DEFAULT_ADDON_REPO, []
        )
        self.up, self.marker, self.store = up, marker, store or {}

    def is_up(self):
        return self.up

    def state(self, entity_id):
        return self.marker if entity_id == compat.LAB_MARKER_ENTITY else None

    def supervisor(self, method, endpoint, data=None, timeout=compat.REQUEST_TIMEOUT):
        return self.store


class SettingsTests(unittest.TestCase):
    def test_missing_address_is_not_run(self):
        code, line = run(lambda lab, args: "never", {})
        self.assertEqual(code, compat.NOT_RUN)
        self.assertEqual(line, "NOT RUN: COMPAT_HA_URL is not set")

    def test_missing_token_file_is_not_run(self):
        code, line = run(
            lambda lab, args: "never",
            {
                "COMPAT_HA_URL": "http://lab.invalid:8123",
                "COMPAT_HA_TOKEN_FILE": "/nonexistent/token",
            },
        )
        self.assertEqual(code, compat.NOT_RUN)
        self.assertIn("cannot read COMPAT_HA_TOKEN_FILE", line)

    def test_settings_are_read(self):
        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as handle:
            handle.write("stand-in-token\n")
        self.addCleanup(os.unlink, handle.name)
        environment = {
            "COMPAT_HA_URL": "http://lab.invalid:8123/",
            "COMPAT_HA_TOKEN_FILE": handle.name,
            "COMPAT_LAB_SHELL": "ssh compat-lab",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            lab = compat.Lab.from_environment()
        self.assertEqual(lab.url, "http://lab.invalid:8123")
        self.assertEqual(lab.token, "stand-in-token")
        self.assertEqual(lab.addon_repo, compat.DEFAULT_ADDON_REPO)
        self.assertEqual(lab.shell, ["ssh", "compat-lab"])


class LabMarkerTests(unittest.TestCase):
    def test_instance_without_the_marker_is_not_run(self):
        with self.assertRaises(compat.StepResult) as caught:
            compat.require_lab(StandInLab(marker=None))
        self.assertEqual(caught.exception.code, compat.NOT_RUN)
        self.assertIn("not marked as a lab", caught.exception.why)

    def test_marker_off_is_not_run(self):
        with self.assertRaises(compat.StepResult) as caught:
            compat.require_lab(StandInLab(marker="off"))
        self.assertEqual(caught.exception.code, compat.NOT_RUN)

    def test_instance_that_does_not_answer_fails(self):
        with self.assertRaises(compat.StepResult) as caught:
            compat.require_lab(StandInLab(up=False))
        self.assertEqual(caught.exception.code, compat.FAIL)

    def test_marked_lab_passes_the_check(self):
        compat.require_lab(StandInLab())


class ExitCodeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            compat.Lab, "from_environment", return_value=StandInLab()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pass(self):
        self.assertEqual(
            run(lambda lab, args: f"ok on {args.core}", {}),
            (compat.PASS, "PASS: ok on 2026.12.0"),
        )

    def test_fail(self):
        def step(lab, args):
            raise compat.failed("the rule did not hold")

        self.assertEqual(run(step, {}), (compat.FAIL, "FAIL: the rule did not hold"))

    def test_a_crash_is_a_fail_never_a_pass(self):
        def step(lab, args):
            raise KeyError("integration")

        code, line = run(step, {})
        self.assertEqual(code, compat.FAIL)
        self.assertEqual(line, "FAIL: the driver stopped: KeyError: 'integration'")


class AppSlugTests(unittest.TestCase):
    STORE = {
        "repositories": [
            {"slug": "a1b2c3d4", "url": "https://github.com/fatherdoctor/ha_codex"},
            {"slug": "e5f6a7b8", "url": "https://github.com/someone/other"},
        ],
        "addons": [
            {"slug": "e5f6a7b8_codex_cli_worker", "repository": "e5f6a7b8"},
            {"slug": "a1b2c3d4_other_app", "repository": "a1b2c3d4"},
            {"slug": "a1b2c3d4_codex_cli_worker", "repository": "a1b2c3d4"},
        ],
    }

    def test_slug_comes_from_the_configured_repository(self):
        self.assertEqual(
            StandInLab(store=self.STORE).addon_slug(), "a1b2c3d4_codex_cli_worker"
        )

    def test_no_slug_when_the_repository_is_not_in_the_store(self):
        store = {
            "repositories": self.STORE["repositories"][1:],
            "addons": self.STORE["addons"],
        }
        self.assertIsNone(StandInLab(store=store).addon_slug())


class LabShellTests(unittest.TestCase):
    def test_no_shell_is_not_run(self):
        with self.assertRaises(compat.StepResult) as caught:
            StandInLab().sh("true")
        self.assertEqual(caught.exception.code, compat.NOT_RUN)

    def test_script_goes_to_sh_on_standard_input(self):
        lab = StandInLab()
        lab.shell = ["env"]
        self.assertEqual(lab.sh("echo one; echo two"), "one\ntwo\n")

    def test_a_failed_script_fails(self):
        lab = StandInLab()
        lab.shell = ["env"]
        with self.assertRaises(compat.StepResult) as caught:
            lab.sh("echo broken >&2; exit 3")
        self.assertEqual(caught.exception.code, compat.FAIL)
        self.assertIn("exited 3: broken", caught.exception.why)


class ErrorLineTests(unittest.TestCase):
    def test_error_lines_are_found(self):
        log = (
            "2026-12-01 10:00:00 INFO (MainThread) started\n"
            "2026-12-01 10:00:01 ERROR (MainThread) [custom_components.codex_cli] setup failed\n"
            "[ERROR] worker: could not start\n"
            "an ERRORS column is not an error line\n"
        )
        self.assertEqual(len(compat.error_lines(log)), 2)


class SignInTests(unittest.TestCase):
    def test_ok(self):
        login = {"has_auth_file": True, "status_ok": True, "server_check": "ok"}
        self.assertEqual(cx02_sign_in.judge(login)[0], compat.PASS)

    def test_rejected_is_not_run(self):
        login = {"has_auth_file": True, "status_ok": False, "server_check": "rejected"}
        code, why = cx02_sign_in.judge(login)
        self.assertEqual(code, compat.NOT_RUN)
        self.assertIn("sign-in expired", why)

    def test_no_sign_in_file_is_not_run(self):
        login = {
            "has_auth_file": False,
            "status_ok": False,
            "server_check": "unchecked",
        }
        self.assertEqual(cx02_sign_in.judge(login)[0], compat.NOT_RUN)

    def test_cli_refuses_the_file(self):
        login = {
            "has_auth_file": True,
            "status_ok": False,
            "server_check": "unchecked",
            "message": "bad file",
        }
        self.assertEqual(
            cx02_sign_in.judge(login),
            (compat.FAIL, "the Codex CLI does not accept the sign-in file: bad file"),
        )

    def test_no_answer_is_a_fail(self):
        login = {"has_auth_file": True, "status_ok": True, "server_check": "unchecked"}
        self.assertEqual(cx02_sign_in.judge(login)[0], compat.FAIL)


class ChatTaskTests(unittest.TestCase):
    def test_the_prompt_does_not_hold_the_word(self):
        self.assertNotIn(cx03_chat_task.WORD, cx03_chat_task.PROMPT)

    def test_console_errors_count_and_other_messages_do_not(self):
        messages = [
            ("log", "loaded"),
            ("warning", "slow"),
            ("error", "boom"),
            ("pageerror", "TypeError: x"),
        ]
        self.assertEqual(
            cx03_chat_task.console_problems(messages), ["boom", "TypeError: x"]
        )


class FileEditTests(unittest.TestCase):
    LINE = "compat_marker_1767225600: added"

    def finished(self, **changes):
        task = {
            "status": "completed",
            "changes": {"added": [], "changed": ["compat/scratch.yaml"], "deleted": []},
            "backups": [
                {
                    "path": "compat/scratch.yaml",
                    "status": "saved",
                    "copy": "backups/compat/scratch.yaml",
                }
            ],
            "config_check": {"result": "valid", "errors": "", "warnings": ""},
        }
        task.update(changes)
        return task

    def text(self, extra=""):
        return cx04_file_edit.SCRATCH_TEXT + extra + self.LINE + "\n"

    def judge(self, task, text=None):
        return cx04_file_edit.judge(
            task, self.text() if text is None else text, self.LINE
        )

    def test_pass(self):
        self.assertEqual(self.judge(self.finished())[0], compat.PASS)

    def test_a_permission_request_fails(self):
        self.assertIn(
            "asked for permission",
            self.judge(self.finished(permission_required=True))[1],
        )

    def test_a_failed_turn_fails(self):
        self.assertEqual(self.judge(self.finished(status="failed"))[0], compat.FAIL)

    def test_the_file_must_be_listed_as_changed(self):
        task = self.finished(
            changes={"added": ["compat/scratch.yaml"], "changed": [], "deleted": []}
        )
        self.assertIn("as changed", self.judge(task)[1])

    def test_the_previous_version_must_be_saved(self):
        for backups in (
            [],
            [{"path": "compat/scratch.yaml", "status": "missing", "copy": ""}],
        ):
            with self.subTest(backups=backups):
                self.assertIn(
                    "no saved copy", self.judge(self.finished(backups=backups))[1]
                )

    def test_the_configuration_check_must_be_valid(self):
        for result in ("disabled", "skipped", "unavailable", "invalid"):
            with self.subTest(result=result):
                task = self.finished(
                    config_check={"result": result, "errors": "", "warnings": ""}
                )
                self.assertEqual(self.judge(task)[0], compat.FAIL)

    def test_the_line_must_be_in_the_file(self):
        self.assertEqual(
            self.judge(self.finished(), cx04_file_edit.SCRATCH_TEXT)[1],
            "the line is not in the file",
        )

    def test_other_changes_fail(self):
        self.assertEqual(
            self.judge(self.finished(), self.text(extra="other: line\n"))[1],
            "the chat changed more than the one line",
        )

    def test_scratch_script_writes_only_under_config_compat(self):
        lab = StandInLab()
        with mock.patch.object(lab, "sh") as sh:
            cx04_file_edit.write_scratch(lab)
        script = sh.call_args.args[0]
        self.assertTrue(
            script.startswith(
                "mkdir -p /config/compat && cat > /config/compat/scratch.yaml <<'SCRATCH'\n"
            )
        )
        self.assertIn(cx04_file_edit.SCRATCH_TEXT, script)


class DriverFolderTests(unittest.TestCase):
    def test_each_built_step_has_a_script_named_in_the_readme(self):
        readme = (COMPAT / "README.md").read_text(encoding="utf-8")
        for script in (
            "cx01_install.py",
            "cx02_sign_in.py",
            "cx03_chat_task.py",
            "cx04_file_edit.py",
        ):
            with self.subTest(script=script):
                self.assertTrue((COMPAT / script).is_file())
                self.assertIn(f"`{script}` |", readme)
        for number in range(1, 15):
            self.assertIn(f"| cx-{number:02d} ", readme)

    def test_scripts_parse_core_argument(self):
        for script in (
            "cx01_install.py",
            "cx02_sign_in.py",
            "cx03_chat_task.py",
            "cx04_file_edit.py",
        ):
            source = (COMPAT / script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertIn("compat.run_step(", source)


if __name__ == "__main__":
    unittest.main()
