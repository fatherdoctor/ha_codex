"""The one-time scan for old /config/ paths in the owner's own text after the move to /homeassistant (CX-HA-PATH)."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import old_path_scan  # noqa: E402

PLANTED = "invented-canary-text-7f3a"


class ScanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.agents = self.root / "AGENTS.md"
        self.notices = []
        self.schedules = []

    def scan(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = old_path_scan.scan_once(
                self.root, self.agents, lambda: self.schedules, self.notify
            )
        return result, out.getvalue()

    def notify(self, *notice):
        """Record the notice; report it sent unless a test set self.sent to False."""
        self.notices.append(notice)
        return getattr(self, "sent", True)

    def test_a_planted_line_in_each_place_is_counted(self):
        self.agents.write_text(
            f"Keep notes in /config/notes.md {PLANTED}\nNothing here\nAlso /config/www/x\n"
        )
        self.schedules = [
            {
                "name": "Morning check",
                "prompt": f"Read /config/automations.yaml {PLANTED}",
            },
            {"name": "Clean", "prompt": "Read /homeassistant/automations.yaml"},
        ]
        findings, log = self.scan()
        self.assertEqual(
            [(f.lines, f.schedules) for f in findings],
            [(2, ()), (1, ("Morning check",))],
        )
        self.assertIn("2 line(s) with /config/", log)
        self.assertIn("(schedules: Morning check)", log)
        title, message, notice_id = self.notices[0]
        self.assertEqual(notice_id, "codex_cli_config_path")
        self.assertIn("/homeassistant", title)
        self.assertIn("Morning check", message)

    def test_the_text_never_reaches_the_log_or_the_notification(self):
        self.agents.write_text(f"/config/secrets.yaml holds {PLANTED}\n")
        self.schedules = [{"name": "Night", "prompt": f"cat /config/{PLANTED}.yaml"}]
        _, log = self.scan()
        self.assertNotIn(PLANTED, log)
        self.assertNotIn(PLANTED, json.dumps(self.notices))
        self.assertNotIn("secrets.yaml", log + json.dumps(self.notices))

    def test_the_app_page_address_and_names_that_end_in_config_are_not_counted(self):
        self.agents.write_text(
            "Open /config/app/abc_codex/config\nSee lovelace/config and .git/config\n"
            "Home Assistant's own /configuration.yaml\n"
        )
        findings, log = self.scan()
        self.assertEqual(findings, [])
        self.assertEqual(self.notices, [])
        self.assertIn("no /config/ path", log)

    def test_a_second_start_does_not_scan(self):
        self.agents.write_text("/config/x\n")
        self.scan()
        self.assertEqual(
            json.loads((self.root / old_path_scan.MARKER_NAME).read_text())["version"],
            "0.6",
        )
        self.notices.clear()
        result, log = self.scan()
        self.assertIsNone(result)
        self.assertEqual(self.notices, [])
        self.assertEqual(log, "")

    def test_a_notice_that_was_not_sent_leaves_no_marker_so_the_next_start_scans_again(
        self,
    ):
        self.agents.write_text("/config/x\n")
        self.sent = False
        findings, log = self.scan()
        self.assertEqual(len(findings), 1)
        self.assertFalse((self.root / old_path_scan.MARKER_NAME).exists())
        self.assertIn("the scan runs again at the next start", log)
        self.sent = True
        findings, _ = self.scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(self.notices), 2)
        self.assertTrue((self.root / old_path_scan.MARKER_NAME).exists())

    def test_a_missing_file_and_a_broken_schedule_store_find_nothing(self):
        def broken():
            raise OSError("database is locked")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            findings = old_path_scan.scan_once(
                self.root, self.agents, broken, self.notify
            )
        self.assertEqual(findings, [])
        self.assertIn("could not read the schedules (OSError)", out.getvalue())
        self.assertNotIn("database is locked", out.getvalue())


class StartTests(unittest.TestCase):
    def test_the_start_runs_the_scan_once_after_the_schedules_are_ready(self):
        source = (Path(old_path_scan.__file__).parent / "server.py").read_text(
            encoding="utf-8"
        )
        main = source[source.index("\ndef main(") :]
        self.assertLess(
            main.index("schedule_service.engine()"),
            main.index("old_path_scan.scan_once("),
        )


if __name__ == "__main__":
    unittest.main()
