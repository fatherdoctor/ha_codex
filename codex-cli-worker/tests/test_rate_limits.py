"""Usage from Codex's own rate-limit read (`account/rateLimits/read`), with the status screen as an estimated fallback."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_server import server

# The reply `codex app-server` 0.166.0 gave on a lab install (ids already redacted there): one weekly window,
# sent as `primary`, with no `secondary` and no 5-hour window on that plan.
CAPTURED = json.loads((Path(__file__).with_name("fixtures") / "account-rateLimits-read.json").read_text())
CENTRAL = timezone(timedelta(hours=-6))
NOW = datetime(2026, 11, 8, 21, 37, tzinfo=CENTRAL)

FAKE_APP_SERVER = """#!{python}
import json, sys
reply = json.loads({reply!r})
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({{"id": message["id"], "result": {{"userAgent": "fake"}}}}), flush=True)
        print(json.dumps({{"method": "account/updated", "params": {{"authMode": "chatgpt"}}}}), flush=True)
    elif message.get("method") == "account/rateLimits/read":
        {answer}
"""


class UsageFromRateLimitsTests(unittest.TestCase):
    """The mapping from the captured result onto the usage fields the sensors read."""

    def test_captured_weekly_window_fills_the_weekly_fields(self) -> None:
        """Window length, not slot, picks the field; the percentage is what is left; the reset is local time."""
        usage = server.usage_from_rate_limits(CAPTURED["result"], NOW)
        self.assertEqual(usage["weekly_percent"], "100")
        self.assertEqual(usage["weekly_limit"], "weekly 100%")
        reset = datetime.fromtimestamp(1794771099, tz=CENTRAL)
        self.assertEqual(usage["weekly_reset_at"], reset.isoformat())
        self.assertEqual(usage["weekly_reset"], f"{reset:%H:%M} on {reset.day} {reset:%b}")
        self.assertEqual(usage["five_hour_percent"], "")
        self.assertEqual(usage["five_hour_limit"], "")
        self.assertIn("weekly 100% left", usage["raw_excerpt"])

    def test_both_windows_and_used_percent_become_left(self) -> None:
        """A 5-hour window in either slot is placed by its length; 37 % used is 63 % left."""
        result = {"rateLimits": {
            "primary": {"usedPercent": 37, "windowDurationMins": 10080, "resetsAt": 1794771099},
            "secondary": {"usedPercent": 12.6, "windowDurationMins": 300, "resetsAt": 1794200000}}}
        usage = server.usage_from_rate_limits(result, NOW)
        self.assertEqual((usage["weekly_percent"], usage["five_hour_percent"]), ("63", "87"))
        self.assertEqual(usage["five_hour_reset"], f"{datetime.fromtimestamp(1794200000, tz=CENTRAL):%H:%M}")

    def test_malformed_windows_give_nothing(self) -> None:
        """Missing fields, a window of an unknown length and a non-object result give empty usage fields."""
        for result in ({}, {"rateLimits": None}, {"rateLimits": {"primary": {"usedPercent": "x"}}},
                       {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 3000}}}):
            with self.subTest(result=result):
                usage = server.usage_from_rate_limits(result, NOW)
                self.assertEqual((usage["five_hour_limit"], usage["weekly_limit"]), ("", ""))


class ReadRateLimitsTests(unittest.TestCase):
    """The app-server conversation, against a stand-in that answers with the captured reply."""

    def fake_codex(self, answer: str) -> str:
        """Write an executable stand-in for the CLI and return its path."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "codex"
        path.write_text(FAKE_APP_SERVER.format(python=sys.executable, reply=json.dumps(CAPTURED), answer=answer))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def read(self, answer: str) -> dict:
        """Run the read against the stand-in in a temporary working folder."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        real_popen = server.subprocess.Popen

        def popen(args, **kwargs):
            return real_popen(args, **{**kwargs, "cwd": folder.name})

        with (patch.object(server, "codex_env", return_value=dict(os.environ)),
              patch.object(server.subprocess, "Popen", side_effect=popen),
              patch.object(server, "RATE_LIMITS_TIMEOUT_SECONDS", 5)):
            return server.read_codex_rate_limits(self.fake_codex(answer))

    def test_initialize_then_read_returns_the_result(self) -> None:
        """The read skips notices and returns the result of request 2."""
        result = self.read('print(json.dumps({"id": message["id"], "result": reply["result"]}), flush=True)')
        self.assertEqual(result["rateLimits"]["primary"]["windowDurationMins"], 10080)

    def test_error_or_silence_raises(self) -> None:
        """An error reply raises with its message; a stand-in that never answers times out."""
        with self.assertRaisesRegex(RuntimeError, "not signed in"):
            self.read('print(json.dumps({"id": message["id"], "error": {"message": "not signed in"}}), flush=True)')
        with self.assertRaises((TimeoutError, RuntimeError)):
            self.read("pass")


class FetchCodexUsageTests(unittest.TestCase):
    """Which read wins, and the estimated note on the fallback."""

    SCREEN = {"status": "ok", "error": "", "weekly_percent": "70", "weekly_limit": "weekly 70%"}

    def fetch(self, read=None, screen=None, active=None):
        """Run fetch_codex_usage with a signed-in CLI and stand-ins for both reads."""
        with (patch.object(server, "active_task_id", return_value=active),
              patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
              patch.object(server, "codex_login_status", return_value={"status_ok": True}),
              patch.object(server, "read_codex_rate_limits", **(read or {"return_value": CAPTURED["result"]})),
              patch.object(server, "fetch_codex_usage_status", return_value=screen or dict(self.SCREEN)) as screen_read):
            return server.fetch_codex_usage(), screen_read

    def test_the_accounts_read_is_used_first(self) -> None:
        """A good read sets the fields with source rate_limits, no note, and never opens the status screen."""
        usage, screen_read = self.fetch()
        self.assertEqual((usage["status"], usage["source"], usage["note"]), ("ok", "rate_limits", ""))
        self.assertEqual(usage["weekly_percent"], "100")
        screen_read.assert_not_called()

    def test_a_failed_read_falls_back_marked_estimated(self) -> None:
        """A failure, or a reply with no window, uses the status screen and says the figures are estimated."""
        for read in ({"side_effect": TimeoutError("slow")}, {"return_value": {"rateLimits": {}}}):
            with self.subTest(read=read):
                usage, screen_read = self.fetch(read=read)
                screen_read.assert_called_once()
                self.assertEqual((usage["source"], usage["weekly_percent"]), ("status_screen", "70"))
                self.assertEqual(usage["note"], server.ESTIMATED_NOTE)
                self.assertTrue(usage["rate_limits_error"])

    def test_no_note_when_the_fallback_also_failed(self) -> None:
        """When the status screen fails too, its error stands and no estimate is claimed."""
        usage, _ = self.fetch(read={"side_effect": RuntimeError("x")}, screen={"status": "error", "error": "no panel"})
        self.assertEqual((usage["status"], usage["note"]), ("error", ""))

    def test_a_running_task_defers_without_reading(self) -> None:
        """While a task runs the deferred answer is returned, keeping the last source and note."""
        with patch.object(server, "read_codex_rate_limits") as read:
            usage, _ = self.fetch(active="task", screen={"status": "deferred"})
            read.assert_not_called()
        self.assertEqual(usage, {"status": "deferred"})


if __name__ == "__main__":
    unittest.main()
