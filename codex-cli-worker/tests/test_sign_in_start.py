"""At start the app signs in by itself once while Codex is not signed in, instead of posting a code every start."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server


class StartSignInTests(unittest.TestCase):
    """auto_start_login_if_needed, with the sign-in and the notifications replaced by recorders."""

    def setUp(self):
        """Point the mark at an empty folder; record sign-in starts and notifications."""
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mark = folder / "auto_sign_in_started"
        self.enterContext(patch.object(server, "AUTO_SIGN_IN_MARK", self.mark))
        self.signed_in = False
        self.enterContext(
            patch.object(server, "codex_login_status", side_effect=lambda: {"status_ok": self.signed_in})
        )
        self.starts = self.enterContext(patch.object(server, "start_codex_login_flow"))
        self.notices = self.enterContext(patch.object(server, "create_persistent_notification"))
        self.enterContext(patch.object(server, "refresh_usage_status_async"))
        self.enterContext(patch.object(server, "update_auth_state"))
        self.enterContext(patch("builtins.print"))

    def test_the_first_start_signs_in_and_later_starts_do_not_post_a_new_code(self):
        """Start 1 starts a sign-in; starts 2 and 3 only point to Sign in, under the code notice's id."""
        for _ in range(3):
            server.auto_start_login_if_needed()
        self.assertEqual(self.starts.call_count, 1)
        self.assertEqual(self.notices.call_count, 2)
        _title, message, notification_id = self.notices.call_args.args
        self.assertEqual(notification_id, server.AUTH_NOTIFY_ID)
        self.assertIn("**Sign in**", message)
        self.assertNotIn("code:", message)

    def test_a_signed_in_start_clears_the_mark(self):
        """After a sign-in, a later sign-out or expiry gets one automatic sign-in again."""
        server.auto_start_login_if_needed()
        self.signed_in = True
        server.auto_start_login_if_needed()
        self.assertFalse(self.mark.exists())
        self.signed_in = False
        server.auto_start_login_if_needed()
        self.assertEqual(self.starts.call_count, 2)
        self.notices.assert_not_called()

    def test_a_mark_that_cannot_be_written_still_signs_in(self):
        """If the mark cannot be saved, the sign-in starts anyway (the old behavior)."""
        self.mark.parent.rmdir()
        server.auto_start_login_if_needed()
        self.assertEqual(self.starts.call_count, 1)


if __name__ == "__main__":
    unittest.main()
