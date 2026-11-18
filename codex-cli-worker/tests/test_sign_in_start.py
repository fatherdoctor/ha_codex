"""At start the app never makes a sign-in code by itself; it points to Sign in, and the owner starts one."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server


class StartSignInTests(unittest.TestCase):
    """auto_start_login_if_needed, with the sign-in and the notifications replaced by recorders."""

    def setUp(self):
        """Point the old mark at an empty folder; record sign-in starts and notifications."""
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

    def test_a_signed_out_start_never_starts_a_sign_in_and_points_to_the_button(self):
        """Every start while signed out posts the same notice under the code notice's id and starts nothing."""
        for _ in range(3):
            server.auto_start_login_if_needed()
        self.starts.assert_not_called()
        self.assertEqual(self.notices.call_count, 3)
        _title, message, notification_id = self.notices.call_args.args
        self.assertEqual(notification_id, server.AUTH_NOTIFY_ID)
        self.assertIn("**Sign in**", message)
        self.assertNotIn("code:", message)

    def test_a_signed_in_start_posts_nothing(self):
        """Signed in, the start refreshes the usage status only."""
        self.signed_in = True
        server.auto_start_login_if_needed()
        self.starts.assert_not_called()
        self.notices.assert_not_called()

    def test_the_mark_of_an_older_version_is_removed(self):
        """Versions before 0.3.2 left a mark after their own sign-in; a start removes it, signed in or not."""
        self.mark.touch()
        server.auto_start_login_if_needed()
        self.assertFalse(self.mark.exists())
        self.starts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
