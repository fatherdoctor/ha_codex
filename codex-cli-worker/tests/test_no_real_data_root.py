"""A unittest run of the chat tests leaves the app's real data folder alone.

Under pytest, conftest.py fails any test that writes under /data. A plain `python -m unittest` run has no such
guard, and a run as root used to leave /data/schedules.sqlite3 behind: the chat tests list chats, listing chats
reads the schedule store under DATA_ROOT, and those tests did not move DATA_ROOT. The leftover file then made
later sandbox tests fail on that machine. This test runs those modules here and fails if anything below the real
DATA_ROOT appears, changes or goes away.

Without root (or another way to create /data) nothing can be written there, so the test passes on such a machine
either way; it guards the runs that matter, in containers and on CI runners that run as root.
"""

import io
import unittest

from test_server import server
from app_folders import changes, snapshot

# The real folder, read when this module loads and before any test patches DATA_ROOT.
REAL_DATA_ROOT = server.DATA_ROOT

# Every module whose tests list chats through the API or open the chat page.
CHAT_MODULES = (
    "test_attachments",
    "test_conversations",
    "test_cx_ui",
    "test_integration_notice",
)


class RealDataRootTests(unittest.TestCase):
    def test_chat_tests_write_nothing_under_the_real_data_root(self):
        self.assertEqual(str(REAL_DATA_ROOT), "/data")
        before = snapshot((REAL_DATA_ROOT,))
        suite = unittest.defaultTestLoader.loadTestsFromNames(CHAT_MODULES)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertTrue(result.testsRun > 0)
        self.assertEqual(
            changes(before, snapshot((REAL_DATA_ROOT,))),
            [],
            f"a chat test wrote under the real {REAL_DATA_ROOT}; patch server.DATA_ROOT in its setUp",
        )


if __name__ == "__main__":
    unittest.main()
