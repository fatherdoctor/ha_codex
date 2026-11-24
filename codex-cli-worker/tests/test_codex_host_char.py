"""Characterize the warm host's public interface with an in-memory app server."""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_host  # noqa: E402
from test_codex_host import FakeSession, HostTest, TOOLS, wait_until  # noqa: E402


class FingerprintsAndFiles(unittest.TestCase):
    def test_fingerprints_are_stable_per_row_and_diff_names_are_sorted(self):
        before = codex_host.fingerprint({"b": {"z": 1, "a": 2}, "a": [1, 2]})
        reordered = codex_host.fingerprint({"a": [1, 2], "b": {"a": 2, "z": 1}})
        self.assertEqual(before, reordered)
        self.assertEqual(codex_host.changed_rows(before, codex_host.fingerprint({"b": {}, "c": 3})),
                         ["a", "b", "c"])
        self.assertTrue(all(len(value) == 64 for value in before.values()))

    def test_signature_handles_missing_and_changed_files(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "session.jsonl"
            self.assertIsNone(codex_host.file_signature(None))
            self.assertIsNone(codex_host.file_signature(path))
            path.write_text("a", encoding="utf-8")
            first = codex_host.file_signature(path)
            self.assertEqual(first[0], 1)
            path.write_text("longer", encoding="utf-8")
            self.assertEqual(codex_host.file_signature(path)[0], 6)
            self.assertNotEqual(codex_host.file_signature(path), first)

    def test_chat_working_tracks_session_or_running_turn(self):
        chat = codex_host.Chat("sample")
        self.assertFalse(chat.working)
        chat.turn_running = True
        self.assertTrue(chat.working)
        chat.turn_running = False
        chat.session = object()
        self.assertTrue(chat.working)


class HostInterface(HostTest):
    def test_start_select_follow_pause_and_stop(self):
        self.assertFalse(self.host.running())
        self.assertTrue(self.host.ready_for_message())
        self.assertTrue(self.host.running())
        self.assertEqual(self.spawned_args[0][-2:], codex_host.UNLOAD_ARGS)
        self.assertTrue(self.host.select("a", {"cwd": "/config"}, TOOLS))
        first = self.host.chat("a").thread_id
        self.assertEqual(self.host.chat_for_thread(first), "a")
        self.assertEqual(self.host.chat_for_thread(None), "")
        self.assertEqual(self.host.live_chats(), ["a"])
        self.host.follow("a", first)
        self.assertEqual(self.host.live_chats(), ["a"])
        self.host.follow("a", "other")
        self.assertEqual(self.host.live_chats(), [])
        self.assertEqual(self.host.chat("a").thread_id, "")
        self.assertEqual(self.host.chat_for_thread(first), "")
        second = self.wake("a")
        self.assertNotEqual(first, second)
        self.host.pause("a")
        self.assertEqual(self.host.live_chats(), [])
        self.host.stop("test")
        self.assertFalse(self.host.running())
        self.assertTrue(self.processes[0].reaped)
        self.host.close()
        self.assertFalse(self.host.ready_for_message())

    def test_attach_detach_cancel_and_deferred_restart(self):
        self.start()
        self.wake("a")
        session = FakeSession("session")
        self.host.attach("a", session)
        self.assertTrue(self.host.chat("a").working)
        with self.assertRaises(codex_host.HostError):
            self.host.pause("a")
        self.host.request_restart("settings")
        self.assertTrue(self.host.restart_due)
        self.assertFalse(self.host.ready_for_message())
        self.host.detach("a")
        self.assertEqual(len(self.processes), 2)
        self.assertFalse(self.host.restart_due)
        self.assertTrue(self.host.ready_for_message())
        self.wake("b")
        next_session = FakeSession("next")
        self.host.attach("b", next_session)
        self.assertTrue(self.host.cancel("b", timeout=0))
        self.assertTrue(next_session.closed)
        self.assertFalse(self.host.chat("b").working)
        self.assertTrue(self.host.cancel("missing", timeout=0))

    def test_busy_limit_and_failed_selection(self):
        self.host.live_max = 1
        self.start()
        self.wake("a")
        self.host.attach("a", FakeSession("a"))
        with self.assertRaises(codex_host.AllChatsBusy):
            self.wake("b")
        self.assertFalse(self.host.select("b", {"cwd": "/config"}, TOOLS))
        self.assertEqual(self.host.live_chats(), ["a"])
        self.host.detach("a")
        self.assertTrue(self.host.select("b", {"cwd": "/config"}, TOOLS))
        self.assertEqual(self.host.live_chats(), ["b"])

    def test_stderr_marks_redaction_and_generation_guard(self):
        self.start()
        mark = self.host.stderr_mark()
        self.codex.stderr.put("a token-value-1 line\n")
        self.assertTrue(wait_until(lambda: self.host.stderr_mark() > mark))
        self.assertEqual(self.host.stderr_since(mark), ["a [hidden] line"])
        self.assertEqual(self.host.stderr_since(self.host.stderr_mark()), [])
        self.host.stop("old generation", generation=-1)
        self.assertTrue(self.host.running())
        self.host.stop("current generation")
        self.assertFalse(self.host.running())


if __name__ == "__main__":
    unittest.main()
