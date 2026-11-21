"""The chat's files folder: what Codex saves there for the person to download, and nothing else (CX-FILE-LINK)."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server

TASK_ID = "20261121T120000Z-0a1b2c3d"


class ChatFilesTests(unittest.TestCase):
    """Listing, serving and the sandbox root of the files folder, with invented files only."""

    def setUp(self):
        """Keep every chat folder in a temporary task root, with one saved chat."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(server, "tasks", {TASK_ID: {"task_id": TASK_ID, "status": "completed"}}))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "write_task_log"))
        self.task_dir = self.root / "tasks" / TASK_ID
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "task.json").write_text("{}")
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}

    def make_folder(self):
        """Create the folder as the worker does before a message, with a workbook in a subfolder."""
        self.assertTrue(server.prepare_chat_files_dir(TASK_ID))
        folder = server.chat_files_dir(TASK_ID)
        (folder / "2025").mkdir()
        (folder / "2025" / "visits.xlsx").write_bytes(b"PK invented")
        return folder

    def get(self, relative):
        """Ask the worker for one file of the chat's files folder."""
        return self.client.get(f"/tasks/{TASK_ID}/files/{relative}", headers=self.headers)

    def test_listing_names_regular_files_only(self):
        """Hidden names and links are left out, so the page never offers them."""
        folder = self.make_folder()
        (folder / ".notes.txt").write_text("hidden")
        (folder / ".cache").mkdir()
        (folder / ".cache" / "inside.txt").write_text("hidden folder")
        (folder / "task-link.json").symlink_to(self.task_dir / "task.json")
        (folder / "outside").symlink_to(self.task_dir, target_is_directory=True)
        self.assertEqual(server.chat_files(TASK_ID), {
            "folder": str(folder), "files": [{"path": "2025/visits.xlsx", "size": 11}]})

    def test_listing_is_capped(self):
        """A folder with very many files lists only the first CHAT_FILES_LIST_MAX of them."""
        folder = self.make_folder()
        for index in range(server.CHAT_FILES_LIST_MAX + 5):
            (folder / f"part-{index:03}.csv").write_text("a")
        self.assertEqual(len(server.chat_files(TASK_ID)["files"]), server.CHAT_FILES_LIST_MAX)

    def test_no_folder_lists_nothing(self):
        """A chat that never ran a message since this version has no folder and no listing."""
        self.assertIsNone(server.chat_files(TASK_ID))
        self.assertIsNone(server.chat_files("unknown-chat"))

    def test_a_folder_that_is_a_link_is_refused(self):
        """If the folder itself is a link, it is never listed, served or made writable."""
        (self.task_dir / server.CHAT_FILES_DIR).symlink_to(self.root, target_is_directory=True)
        (self.root / "secret.txt").write_text("outside")
        self.assertFalse(server.prepare_chat_files_dir(TASK_ID))
        self.assertIsNone(server.chat_files(TASK_ID))
        self.assertEqual(self.get("secret.txt").status_code, 404)
        self.assertEqual(server.chat_files_note(TASK_ID), "")

    def test_download_serves_the_file_as_an_attachment(self):
        """The file comes back as a download with a neutral type, never as a page."""
        self.make_folder()
        response = self.get("2025/visits.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"PK invented")
        self.assertEqual(response.mimetype, "application/octet-stream")
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn("visits.xlsx", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()

    def test_paths_outside_the_folder_are_refused(self):
        """Climbing out, hidden names, links, folders, missing files and unknown chats all answer 404."""
        folder = self.make_folder()
        (folder / ".notes.txt").write_text("hidden")
        (folder / "task-link.json").symlink_to(self.task_dir / "task.json")
        (folder / "linked").symlink_to(self.task_dir, target_is_directory=True)
        for relative in ["../task.json", "2025/../../task.json", "%2E%2E/task.json", ".notes.txt", "task-link.json",
                         "linked/task.json", "2025", "missing.xlsx", ""]:
            with self.subTest(relative=relative):
                self.assertEqual(self.get(relative).status_code, 404)
        self.assertIsNone(server.open_chat_file(TASK_ID, "/etc/hostname"))
        self.assertEqual(self.client.get("/tasks/other-chat/files/2025/visits.xlsx", headers=self.headers).status_code,
                         404)
        self.assertEqual(self.client.get(f"/tasks/{TASK_ID}/files/2025/visits.xlsx").status_code, 401)

    def test_open_checks_the_opened_file_again(self):
        """A file swapped for a link after the path was checked is still refused: the open does not follow it."""
        folder = self.make_folder()
        target = folder / "2025" / "visits.xlsx"
        target.unlink()
        target.symlink_to(self.task_dir / "task.json")
        self.assertIsNone(server.open_chat_file(TASK_ID, "2025/visits.xlsx"))
        # A parent folder swapped for a link resolves outside the folder, so the opened file is refused too.
        (folder / "2025").rename(folder / "old")
        (folder / "2025").symlink_to(self.task_dir, target_is_directory=True)
        self.assertIsNone(server.open_chat_file(TASK_ID, "2025/task.json"))

    def test_workspace_write_can_write_in_the_folder_only(self):
        """workspace-write gets the chat's files folder as one more writable root; read-only gets none."""
        folder = self.make_folder()
        proc = type("Process", (), {"stdin": io.StringIO(), "poll": lambda _: None})()
        final = self.task_dir / "turns" / "t1" / "final.json"
        final.parent.mkdir(parents=True)
        options = {**server.DEFAULT_OPTIONS, "codex_sandbox": "workspace-write"}
        with patch.object(server, "task_mode_settings", return_value={
                "sandbox": "workspace-write", "approval_policy": "on-request", "approvals_reviewer": "user"}):
            session = server.create_approval_session(TASK_ID, proc, "Make a table", final, None, {}, options)
        self.assertEqual(session.turn_params["sandboxPolicy"]["writableRoots"], ["/config", "/share", str(folder)])
        with patch.object(server, "task_mode_settings", return_value={
                "sandbox": "read-only", "approval_policy": "on-request", "approvals_reviewer": "user"}):
            reader = server.create_approval_session(TASK_ID, proc, "Read", final, None, {}, options)
        self.assertNotIn("writableRoots", reader.turn_params["sandboxPolicy"])

    def test_codex_is_told_where_to_save_files(self):
        """exchange_info names the folder once it exists, and the page rules say how to link a file."""
        self.assertNotIn("files", server.exchange_info(TASK_ID, None))
        folder = self.make_folder()
        note = server.exchange_info(TASK_ID, None)["files"]
        self.assertIn(f"{folder}/", note)
        self.assertIn("Only a link into that folder becomes a download.", note)
        self.assertIn("exchange_info's files text", server.build_page_instructions())

    def test_task_payload_carries_the_listing(self):
        """The page reads the listing from GET /tasks/<id> to turn links into download cards."""
        folder = self.make_folder()
        with patch.object(server, "retention_context", return_value=None), \
                patch.object(server, "task_payload", return_value={"task_id": TASK_ID}):
            data = self.client.get(f"/tasks/{TASK_ID}", headers=self.headers).get_json()
        self.assertEqual(data["task"]["chat_files"]["folder"], str(folder))
        self.assertEqual(os.path.basename(data["task"]["chat_files"]["files"][0]["path"]), "visits.xlsx")


if __name__ == "__main__":
    unittest.main()
