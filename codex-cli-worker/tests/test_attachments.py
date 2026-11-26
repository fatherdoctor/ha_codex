"""Generated image capture, storage, serving and persistence regressions."""
from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
import unittest
import zlib
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from app_server_fixture import StubAppServer
from test_server import server

SESSION_ID = "019fc242-910a-7c92-a17d-54c014e19fc4"
# The real name of the upload folder, read before any test patches ATTACHMENTS_ROOT to a temporary folder.
UPLOAD_FOLDER_NAME = server.ATTACHMENTS_ROOT.name


def png_bytes(width=4, height=3, shade=200):
    """Build a tiny valid PNG so tests never depend on binary fixtures."""
    def chunk(tag, data):
        """Frame one PNG chunk with its length and CRC."""
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes([shade, 80, 40]) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def rollout_line(item_id, saved_path="", result=b"", status="completed", failure=None, revised="A cartoon sheep"):
    """Mirror the item_completed record Codex CLI 0.154.0 writes for image_gen.generation."""
    item = {"type": "Extension", "kind": "image_gen.generation", "id": item_id, "status": status,
            "revisedPrompt": revised, "result": base64.b64encode(result).decode() if result else "",
            "transparentBackground": False, "failure": failure, "savedPath": saved_path}
    return json.dumps({"timestamp": "2026-09-22T09:21:54.198Z", "ordinal": 22, "type": "event_msg",
                       "payload": {"type": "item_completed", "thread_id": SESSION_ID, "turn_id": "turn-1", "item": item},
                       "started_at_ms": 1}) + "\n"


class AttachmentTests(unittest.TestCase):
    """Images in a chat: those Codex generates and those the user attaches to a message."""

    def setUp(self):
        """Isolate task storage, CODEX_HOME and Home Assistant calls in a temp directory."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(server.tasks, clear=True))
        for name, value in (("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "ATTACHMENTS_ROOT", self.root / "chat_uploads"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "CONFIG_ROOT", self.root / "config"))
        # Listing chats reads the schedule store under DATA_ROOT; keep it off the real /data.
        self.stack.enter_context(patch.object(server, "DATA_ROOT", self.root / "data"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.stack.enter_context(patch.object(server, "sandbox_readiness", return_value={"required": True, "ready": True}))
        self.stack.enter_context(patch.object(server, "read_options", return_value={"task_timeout_seconds": 30, "auto_save_lovelace": False}))
        self.stack.enter_context(patch.object(server, "build_manifest", return_value={}))
        self.stack.enter_context(patch.object(server, "validate_changed_files", return_value=[]))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.events = []
        self.stack.enter_context(patch.object(server, "fire_ha_event", side_effect=lambda _event, data: (self.events.append(data) or True, "")))
        (self.root / "config").mkdir()
        self.rollout = server.CODEX_HOME / "sessions" / "2026" / "09" / "22" / f"rollout-2026-09-22T12-20-55-{SESSION_ID}.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text("", encoding="utf-8")
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}

    def save_generated(self, name, data):
        """Write a file where Codex would save a generated image."""
        directory = server.generated_images_dir(SESSION_ID)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        return path

    def append_rollout(self, *lines):
        """Append raw records to the session rollout file."""
        with self.rollout.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))

    def create(self, prompt="Draw a sheep"):
        """Create a queued task through the API and return its id."""
        response = self.client.post("/tasks", json={"prompt": prompt}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def run_codex(self, task_id, *, on_wait=None, session_id=None, reply=None, final=None):
        """Run one exchange with a fake Codex process that writes the final response."""
        final = final or {"status": "completed", "summary": "Here is your sheep.", "details": "", "question": ""}

        process = StubAppServer(SESSION_ID, final, on_turn=on_wait)
        with patch.object(server.subprocess, "Popen", return_value=process):
            server.run_task(task_id, "Draw a sheep", session_id, reply)
        server.active_task_runners.discard(task_id)

    def generate_first_image(self, task_id, name="exec-1.png", data=None):
        """Complete a first exchange that produces one generated image."""
        data = data or png_bytes()

        def on_wait():
            """Simulate Codex saving an image and recording it in the rollout."""
            path = self.save_generated(name, data)
            self.append_rollout(rollout_line(name.removesuffix(".png"), saved_path=str(path)))

        self.run_codex(task_id, on_wait=on_wait)
        return data

    def test_generated_image_is_attached_served_and_survives_restart(self):
        """Capture, serve, download, authorize and persist a generated image."""
        task_id = self.create()
        png = self.generate_first_image(task_id)
        task = server.tasks[task_id]
        self.assertEqual(task["status"], "completed")
        attachments = task["attachments"]
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment["kind"], "image")
        self.assertEqual(attachment["mime_type"], "image/png")
        self.assertEqual(attachment["size"], len(png))
        self.assertEqual(attachment["revised_prompt"], "A cartoon sheep")
        self.assertEqual(attachment["generation_id"], "exec-1")
        self.assertEqual(attachment["url"], f"/tasks/{task_id}/attachments/{attachment['attachment_id']}")
        self.assertTrue(attachment["path"].startswith(f"turns/{task['current_turn_id']}/attachments/"))
        stored = server.get_task_dir(task_id) / attachment["path"]
        self.assertEqual(stored.read_bytes(), png)
        self.assertEqual(task["turns"][0]["attachments"], attachments)
        self.assertEqual(self.events[-1]["attachments"], attachments)
        self.assertNotIn("attachments", self.events[-1]["response"])
        # The original stays where Codex saved it so follow-up edits keep working.
        self.assertTrue((server.generated_images_dir(SESSION_ID) / "exec-1.png").exists())

        response = self.client.get(attachment["url"], headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, png)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("attachment", response.headers.get("Content-Disposition", ""))
        download = self.client.get(attachment["url"] + "?download=1", headers=self.headers)
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        self.assertIn(attachment["name"], download.headers["Content-Disposition"])
        self.assertEqual(self.client.get(attachment["url"]).status_code, 401)
        self.assertEqual(self.client.get(f"/tasks/{task_id}/attachments/{'0' * 32}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/tasks/{task_id}/attachments/not-an-id", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/tasks/missing/attachments/{attachment['attachment_id']}", headers=self.headers).status_code, 404)

        server.tasks.clear()
        server.load_task_index()
        loaded = self.client.get(f"/tasks/{task_id}", headers=self.headers).json["task"]
        self.assertEqual(loaded["turns"][0]["attachments"], attachments)
        self.assertEqual(loaded["attachments"], attachments)
        self.assertEqual(self.client.get(attachment["url"], headers=self.headers).status_code, 200)
        summary = self.client.get("/tasks?summary=true", headers=self.headers).json["tasks"][0]
        self.assertNotIn("attachments", summary)

        stored.unlink()
        missing = self.client.get(attachment["url"], headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        self.assertIn("missing", missing.json["error"])
        stored.write_bytes(b"not an image any more")
        self.assertEqual(self.client.get(attachment["url"], headers=self.headers).status_code, 404)
        # A valid image of the same type with different bytes is not the recorded attachment either.
        stored.write_bytes(png_bytes(shade=1))
        altered = self.client.get(attachment["url"], headers=self.headers)
        self.assertEqual(altered.status_code, 404)
        self.assertIn("not the recorded image", altered.json["error"])
        stored.write_bytes(png)
        self.assertEqual(self.client.get(attachment["url"], headers=self.headers).status_code, 200)

    def test_oversized_stored_file_is_not_served(self):
        """A stored file above the size limit is refused even when its hash matches."""
        task_id = self.create()
        server.active_task_runners.discard(task_id)
        png = png_bytes(width=64, height=64)
        run_dir = server.get_run_dir(task_id) / "attachments"
        run_dir.mkdir(parents=True)
        attachment_id = "a" * 32
        stored = run_dir / f"{attachment_id}.png"
        stored.write_bytes(png)
        server.update_task(task_id, status="completed", session_id=SESSION_ID, attachments=[{
            "attachment_id": attachment_id, "kind": "image", "mime_type": "image/png", "name": "big.png",
            "size": len(png), "sha256": server.file_hash(stored), "path": stored.relative_to(server.get_task_dir(task_id)).as_posix(),
        }])
        with patch.object(server, "ATTACHMENT_MAX_BYTES", len(png) - 1):
            self.assertEqual(self.client.get(f"/tasks/{task_id}/attachments/{attachment_id}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/tasks/{task_id}/attachments/{attachment_id}", headers=self.headers).status_code, 200)

    def test_resumed_turn_attaches_only_new_images_and_keeps_history(self):
        """A continued conversation attaches only images from its own exchange."""
        task_id = self.create()
        self.generate_first_image(task_id)
        first = server.tasks[task_id]["turns"][0]["attachments"]
        response = self.client.post(f"/tasks/{task_id}/continue", json={"message": "Make it blue"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.tasks[task_id]["attachments"], [])
        self.assertEqual(server.tasks[task_id]["turns"][0]["attachments"], first)
        blue = png_bytes(shade=20)

        def on_wait():
            """Simulate Codex saving an image and recording it in the rollout."""
            path = self.save_generated("exec-2.png", blue)
            self.append_rollout(rollout_line("exec-2", saved_path=str(path), revised="A blue sheep"))

        self.run_codex(task_id, on_wait=on_wait, session_id=SESSION_ID, reply="Make it blue")
        task = server.tasks[task_id]
        self.assertEqual([item["generation_id"] for item in task["turns"][1]["attachments"]], ["exec-2"])
        self.assertEqual(task["turns"][0]["attachments"], first)
        self.assertEqual(task["attachments"], task["turns"][1]["attachments"])
        self.assertEqual(self.client.get(task["attachments"][0]["url"], headers=self.headers).data, blue)
        self.assertEqual(self.client.get(first[0]["url"], headers=self.headers).status_code, 200)
        first_dir = server.get_task_dir(task_id) / Path(first[0]["path"]).parent
        second_dir = server.get_task_dir(task_id) / Path(task["attachments"][0]["path"]).parent
        self.assertNotEqual(first_dir, second_dir)

    def test_inline_result_fallback_and_unusable_items_are_skipped(self):
        """Decode the inline result when the file is gone; skip unusable items."""
        task_id = self.create()
        png = png_bytes()
        outside = self.root / "config" / "secrets.yaml"
        outside.write_bytes(png)

        def on_wait():
            """Simulate Codex saving an image and recording it in the rollout."""
            text_file = self.save_generated("exec-text.png", b"not an image at all")
            self.append_rollout(
                "not json\n",
                json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "m"}}}) + "\n",
                rollout_line("exec-inline", saved_path=str(self.root / "codex" / "generated_images" / SESSION_ID / "gone.png"), result=png),
                rollout_line("exec-outside", saved_path=str(outside)),
                rollout_line("exec-text", saved_path=str(text_file)),
                rollout_line("exec-failed", saved_path=str(text_file), status="failed", failure={"message": "blocked"}),
                rollout_line("exec-empty"),
            )

        self.run_codex(task_id, on_wait=on_wait)
        task = server.tasks[task_id]
        self.assertEqual([item["generation_id"] for item in task["attachments"]], ["exec-inline"])
        self.assertEqual(self.client.get(task["attachments"][0]["url"], headers=self.headers).data, png)
        self.assertEqual(task["status"], "completed")
        log = (server.get_task_dir(task_id) / "codex.log").read_text(encoding="utf-8")
        for item_id in ("exec-outside", "exec-text", "exec-failed", "exec-empty"):
            self.assertIn(f"Skipped image generation {item_id}", log)

    def test_size_and_count_limits_are_enforced(self):
        """Oversized images and images beyond the per-exchange limit are skipped."""
        task_id = self.create()
        small = png_bytes()
        large = png_bytes(width=64, height=64)

        def on_wait():
            """Simulate Codex saving an image and recording it in the rollout."""
            paths = [self.save_generated(name, data) for name, data in (("a.png", small), ("b.png", large), ("c.png", small))]
            self.append_rollout(*(rollout_line(path.stem, saved_path=str(path)) for path in paths))

        with patch.object(server, "ATTACHMENT_MAX_BYTES", len(large) - 1), patch.object(server, "ATTACHMENTS_PER_TURN_MAX", 1):
            self.run_codex(task_id, on_wait=on_wait)
        self.assertEqual([item["generation_id"] for item in server.tasks[task_id]["attachments"]], ["a"])
        log = (server.get_task_dir(task_id) / "codex.log").read_text(encoding="utf-8")
        self.assertIn("Skipped image generation b: attachment limit reached", log)

    def test_attachment_paths_outside_the_task_directory_are_rejected(self):
        """Recorded paths that escape the task directory are never served."""
        task_id = self.create()
        server.active_task_runners.discard(task_id)
        png = png_bytes()
        (self.root / "outside.png").write_bytes(png)
        attempts = ["../../outside.png", str(self.root / "outside.png"), ""]
        for index, relative in enumerate(attempts):
            attachment_id = f"{index:032x}"
            server.update_task(task_id, status="completed", session_id=SESSION_ID, attachments=[{
                "attachment_id": attachment_id, "kind": "image", "mime_type": "image/png", "name": "x.png", "path": relative,
            }])
            response = self.client.get(f"/tasks/{task_id}/attachments/{attachment_id}", headers=self.headers)
            self.assertEqual(response.status_code, 404, relative)

    def test_rollout_parsing_and_prompt_guidance(self):
        """Rollout parsing keeps the last record per item and the prompt mentions attachments."""
        self.append_rollout(rollout_line("exec-1", saved_path="/x/1.png", status="generating"), rollout_line("exec-1", saved_path="/x/1.png"))
        items = server.image_generation_items(SESSION_ID)
        self.assertEqual([(item["id"], item["status"], item["saved_path"]) for item in items], [("exec-1", "completed", "/x/1.png")])
        self.assertEqual(server.image_generation_items("not-a-session"), [])
        self.assertEqual(server.image_generation_items(""), [])
        self.assertIsNone(server.sniff_image(b"plain text"))
        self.assertEqual(server.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ("image/webp", ".webp"))
        prompt = server.PAGE_INSTRUCTIONS
        self.assertIn("image generation tool", prompt)
        self.assertIn("attached to this conversation", prompt)

    def test_cancellation_and_launch_failure_clear_attachments(self):
        """Cancelling a continued exchange clears only that exchange's attachments."""
        task_id = self.create()
        self.generate_first_image(task_id)
        self.assertEqual(len(server.tasks[task_id]["attachments"]), 1)
        self.client.post(f"/tasks/{task_id}/continue", json={"message": "Again"}, headers=self.headers)
        accepted, _, _ = server.request_task_cancellation(task_id)
        self.assertTrue(accepted)
        self.assertEqual(server.tasks[task_id]["attachments"], [])
        self.assertEqual(len(server.tasks[task_id]["turns"][0]["attachments"]), 1)
        self.assertEqual(server.tasks[task_id]["turns"][1]["attachments"], [])

    def upload(self, name, data):
        """Build one attachment entry for a message request."""
        return {"name": name, "data": base64.b64encode(data).decode()}

    def test_uploads_live_outside_the_task_folder(self):
        """A photo is stored under the app's data folder, never under the task folder in /homeassistant."""
        response = self.client.post("/tasks", json={"prompt": "Look", "attachments": [self.upload("a.png", png_bytes())]},
                                    headers=self.headers)
        task_id = response.json["task_id"]
        item = server.tasks[task_id]["turns"][0]["prompt_attachments"][0]
        stored = server.ATTACHMENTS_ROOT / task_id / item["path"]
        self.assertTrue(stored.is_file())
        self.assertFalse(stored.is_relative_to(server.get_task_dir(task_id)))
        self.assertFalse(list(server.get_task_dir(task_id).rglob("*.png")))
        server.tasks[task_id]["status"] = "completed"
        server.active_task_runners.discard(task_id)
        self.assertEqual(self.client.delete(f"/tasks/{task_id}", headers=self.headers).status_code, 200)
        self.assertFalse((server.ATTACHMENTS_ROOT / task_id).exists())

    def test_cleanup_removes_only_uploads_older_than_seven_days(self):
        """The cleanup age is 7 days by file time; newer files and the folder tree stay."""
        old = server.ATTACHMENTS_ROOT / "t1" / "turn1" / "old.png"
        new = server.ATTACHMENTS_ROOT / "t1" / "turn2" / "new.png"
        for path in (old, new):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x")
        now = 10_000_000.0
        os.utime(old, (now - 7 * 86400 - 60,) * 2)
        os.utime(new, (now - 6 * 86400,) * 2)
        self.assertEqual(server.cleanup_attachments(now), 1)
        self.assertFalse(old.exists())
        # The emptied folder was just changed, so the next run removes it.
        os.utime(old.parent, (now - 7200,) * 2)
        self.assertEqual(server.cleanup_attachments(now), 0)
        self.assertFalse(old.parent.exists())
        self.assertTrue(new.is_file())

    def test_cleanup_keeps_the_empty_folder_of_a_new_upload(self):
        """A folder just made for an upload, with its file not yet written, is not removed."""
        folder = server.ATTACHMENTS_ROOT / "t1" / "turn1"
        folder.mkdir(parents=True)
        self.assertEqual(server.cleanup_attachments(), 0)
        self.assertTrue(folder.is_dir())

    def legacy_upload(self, task_id):
        """Put an upload where earlier versions kept it, under the task folder, and record it on a new chat."""
        task_dir = server.get_task_dir(task_id)
        legacy = task_dir / "turns" / "turn1" / "attachments" / ("b" * 32 + ".png")
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(png_bytes())
        record = {"attachment_id": "b" * 32, "kind": "image", "origin": "user",
                  "path": legacy.relative_to(task_dir).as_posix()}
        server.tasks[task_id] = {"task_id": task_id, "turns": [{"turn_id": "turn1", "prompt_attachments": [record]}]}
        return legacy, record

    def test_a_failed_move_is_tried_again_at_the_next_start(self):
        """When a file does not move, the record keeps its old path, and the next start moves it."""
        legacy, record = self.legacy_upload("a" * 32)
        with patch.object(server.shutil, "move", side_effect=OSError("disk full")):
            self.assertEqual(server.migrate_attachments(), 0)
        self.assertTrue(legacy.is_file())
        self.assertNotIn("store", record)
        self.assertEqual(server.migrate_attachments(), 1)
        self.assertEqual(record["store"], "data")

    def test_a_chat_that_appears_at_a_later_start_gets_its_uploads_moved(self):
        """A chat that moves out of /homeassistant only at the second start still has its old uploads moved then."""
        self.assertEqual(server.migrate_attachments(), 0)
        legacy, record = self.legacy_upload("c" * 32)
        self.assertEqual(server.migrate_attachments(), 1)
        self.assertFalse(legacy.exists())
        self.assertTrue((server.ATTACHMENTS_ROOT / ("c" * 32) / record["path"]).is_file())

    def test_removed_upload_answers_with_the_removal_message(self):
        """A chat whose upload was cleaned up gets 410 and the plain message, not a broken image."""
        response = self.client.post("/tasks", json={"prompt": "Look", "attachments": [self.upload("a.png", png_bytes())]},
                                    headers=self.headers)
        task_id = response.json["task_id"]
        item = server.tasks[task_id]["turns"][0]["prompt_attachments"][0]
        (server.ATTACHMENTS_ROOT / task_id / item["path"]).unlink()
        gone = self.client.get(item["url"], headers=self.headers)
        self.assertEqual(gone.status_code, 410)
        self.assertEqual(gone.json["error"], "attachment removed after 7 days")

    def test_old_uploads_move_out_of_the_task_folder_once(self):
        """An upload that an earlier version kept under the task folder moves once, and the record follows."""
        task_id = "a" * 32
        legacy, record = self.legacy_upload(task_id)
        self.assertEqual(server.migrate_attachments(), 1)
        self.assertFalse(legacy.exists())
        self.assertEqual(record["store"], "data")
        self.assertTrue((server.ATTACHMENTS_ROOT / task_id / record["path"]).is_file())
        self.assertEqual(server.migrate_attachments(), 0)

    def test_the_backup_leaves_out_uploads_only(self):
        """Each backup_exclude pattern, applied as the Supervisor applies it, leaves out the uploads and nothing else.

        The Supervisor tests every path it archives with PurePath.match, which matches a relative pattern from the
        right of the full path, and it does not enter a folder it leaves out. So a path is left out when it or any
        folder above it matches a pattern. The data path below is made up; only the part from /data on is the app's.
        """
        config = server.yaml.safe_load(Path(server.__file__).with_name("config.yaml").read_text(encoding="utf-8"))
        patterns = config["backup_exclude"]
        data = PurePosixPath("/mnt/data/supervisor/apps/data/local_codex_cli")
        uploads = data / UPLOAD_FOLDER_NAME

        def left_out(path):
            """True when the Supervisor would leave this path out of the app backup."""
            folders = [path, *path.parents]
            return any(folder.match(pattern) for folder in folders if folder.is_relative_to(data) for pattern in patterns)

        chat = data / "codex_tasks" / ("a" * 32)
        self.assertTrue(left_out(uploads))
        self.assertTrue(left_out(uploads / ("a" * 32) / "turn1" / ("b" * 32 + ".png")))
        self.assertFalse(left_out(chat / "turns" / "turn1" / "attachments" / ("c" * 32 + ".png")))
        self.assertFalse(left_out(chat / "turns" / "turn1" / "attachments"))
        self.assertFalse(left_out(chat / "codex.log"))
        self.assertFalse(left_out(chat / "task.json"))

    def test_attached_images_are_stored_served_and_passed_to_codex(self):
        """Images attached to a message stay with the exchange and reach Codex as localImage inputs."""
        png, jpeg = png_bytes(), b"\xff\xd8\xff\xe0" + b"\x00" * 64
        response = self.client.post(
            "/tasks",
            json={"prompt": "Fix the card in this screenshot",
                  "attachments": [self.upload("../../Screen shot.PNG", png), self.upload("photo", jpeg)]},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        task_id = response.json["task_id"]
        turn = server.tasks[task_id]["turns"][0]
        sent = turn["prompt_attachments"]
        self.assertEqual([(item["name"], item["mime_type"], item["origin"]) for item in sent],
                         [("Screen shot.PNG", "image/png", "user"), ("photo.jpg", "image/jpeg", "user")])
        for item in sent:
            path = server.ATTACHMENTS_ROOT / task_id / item["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent.name, turn["turn_id"])
            self.assertEqual(item["store"], "data")
        # Served like generated images, and reported by the task API.
        served = self.client.get(f"/tasks/{task_id}/attachments/{sent[0]['attachment_id']}", headers=self.headers)
        self.assertEqual((served.status_code, served.mimetype, served.data), (200, "image/png", png))
        self.assertEqual(self.client.get(sent[0]["url"]).status_code, 401)
        payload = self.client.get(f"/tasks/{task_id}", headers=self.headers).json["task"]
        self.assertEqual([item["attachment_id"] for item in payload["turns"][0]["prompt_attachments"]],
                         [item["attachment_id"] for item in sent])
        self.assertEqual(payload["attachments"], [])
        # exchange_info names the images and Codex receives them with that turn.
        info = server.exchange_info(task_id, None)
        self.assertIn("attached 2 image(s) to this message: Screen shot.PNG, photo.jpg", info["attachments"])
        options = {"codex_sandbox": "workspace-write"}
        session = server.create_approval_session(task_id, StubAppServer(SESSION_ID), "Fix the card", Path("final"), None, {}, options)
        paths = [str((server.ATTACHMENTS_ROOT / task_id / item["path"]).resolve()) for item in sent]
        self.assertEqual(session.turn_params["input"][1:], [{"type": "localImage", "path": path} for path in paths])
        # A continued exchange supplies its own images.
        self.run_codex(task_id)
        response = self.client.post(
            f"/tasks/{task_id}/continue",
            json={"message": "And this one?", "attachments": [self.upload("second.png", png)]},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        second = server.tasks[task_id]["turns"][1]["prompt_attachments"]
        self.assertEqual([item["name"] for item in second], ["second.png"])
        self.assertEqual(server.tasks[task_id]["turns"][0]["prompt_attachments"], sent)
        self.assertIn("attached 1 image(s) to this message: second.png", server.exchange_info(task_id, None)["attachments"])
        session = server.create_approval_session(task_id, StubAppServer(SESSION_ID), "Follow up", Path("final"), SESSION_ID, {}, options)
        second_path = str((server.ATTACHMENTS_ROOT / task_id / second[0]["path"]).resolve())
        self.assertEqual(session.thread_params["threadId"], SESSION_ID)
        self.assertEqual(session.turn_params["input"][1:], [{"type": "localImage", "path": second_path}])
        # Uploads survive a worker restart and both kinds resolve through find_attachment.
        server.tasks.clear()
        server.load_task_index()
        loaded = server.tasks[task_id]
        self.assertEqual([item["name"] for item in loaded["turns"][1]["prompt_attachments"]], ["second.png"])
        self.assertEqual(server.find_attachment(loaded, sent[1]["attachment_id"])["name"], "photo.jpg")
        self.assertEqual(self.client.get(sent[1]["url"], headers=self.headers).mimetype, "image/jpeg")
        # A message without attachments records an empty list and passes no images.
        self.run_codex(task_id, session_id=SESSION_ID, reply="And this one?")
        self.client.post(f"/tasks/{task_id}/continue", json={"message": "Thanks"}, headers=self.headers)
        self.assertEqual(server.tasks[task_id]["turns"][2]["prompt_attachments"], [])
        session = server.create_approval_session(task_id, StubAppServer(SESSION_ID), "Follow up", Path("final"), SESSION_ID, {}, options)
        self.assertEqual(session.turn_params["input"], [{"type": "text", "text": "Follow up"}])
        self.assertNotIn("attachments", server.exchange_info(task_id, None))

    def test_attached_image_validation(self):
        """Bad attachments are rejected with HTTP 400 before any task is created."""
        png = png_bytes()
        cases = [
            ({"attachments": "nope"}, "attachments must be a list"),
            ({"attachments": [self.upload("a.png", png)] * (server.UPLOADS_PER_MESSAGE_MAX + 1)}, "at most 6 images"),
            ({"attachments": [{"name": "a.png"}]}, "needs base64"),
            ({"attachments": [{"name": "a.png", "data": "not base64!"}]}, "not valid base64"),
            ({"attachments": [self.upload("notes.txt", b"just text")]}, "Only PNG, JPEG, GIF, and WebP"),
            ({"attachments": [self.upload("empty.png", b"")]}, "is empty"),
        ]
        for extra, message in cases:
            with self.subTest(message=message):
                response = self.client.post("/tasks", json={"prompt": "Look", **extra}, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json["error"])
        with patch.object(server, "UPLOAD_MAX_BYTES", 64):
            response = self.client.post(
                "/tasks", json={"prompt": "Look", "attachments": [self.upload("big.png", png_bytes(width=64, height=64))]},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("MB or smaller", response.json["error"])
        self.assertEqual(server.tasks, {})
        self.assertFalse((self.root / "tasks").exists())
        # Names are reduced to a safe basename and get the sniffed suffix.
        self.assertEqual(server.upload_name("  C:\\Users\\me\\..\\..\\Shot: 1?.png ", ".png"), "Shot 1.png")
        self.assertEqual(server.upload_name("", ".jpg"), "image.jpg")
        self.assertEqual(server.upload_name("x" * 300, ".png"), "x" * server.UPLOAD_NAME_MAX_LENGTH + ".png")
        self.assertEqual(server.upload_name("line\nbreak.gif", ".gif"), "linebreak.gif")
        # Data URLs are accepted; a request beyond the size limit is refused outright.
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()
        response = self.client.post(
            "/tasks", json={"prompt": "Look", "attachments": [{"name": "shot.png", "data": data_url}]}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        with patch.dict(server.app.config, {"MAX_CONTENT_LENGTH": 100}):
            response = self.client.post(
                "/tasks", json={"prompt": "Look", "attachments": [self.upload("a.png", png)]}, headers=self.headers,
            )
        self.assertEqual(response.status_code, 413)
        # Continuing checks the same rules and leaves the conversation untouched on failure.
        task_id = self.client.get("/tasks?summary=true", headers=self.headers).json["tasks"][0]["task_id"]
        self.run_codex(task_id)
        response = self.client.post(
            f"/tasks/{task_id}/continue", json={"message": "Again", "attachments": [self.upload("notes.txt", b"text")]},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(server.tasks[task_id]["turns"]), 1)
        self.assertEqual(server.tasks[task_id]["status"], "completed")

    def test_partial_attachment_writes_remove_only_the_failed_target(self):
        """Remove partial byte writes and copies while preserving the original exception and existing images."""
        task_id = self.create()
        png = png_bytes()
        _, existing, _ = server._write_attachment_file(task_id, png)
        source = self.root / "source.png"
        source.write_bytes(png)
        write_bytes = Path.write_bytes
        for value in (png, source):
            with self.subTest(source_type=type(value).__name__):
                failure = OSError("Partial attachment write")
                failed = []

                def partial_write(first, second):
                    """Write a partial unique attachment before simulating its I/O failure."""
                    target = second if isinstance(value, Path) else first
                    failed.append(target)
                    write_bytes(target, b"partial")
                    raise failure

                operation = patch.object(server.shutil, "copyfile", side_effect=partial_write) if isinstance(value, Path) else patch.object(Path, "write_bytes", autospec=True, side_effect=partial_write)
                with operation, self.assertRaises(OSError) as raised:
                    server._write_attachment_file(task_id, value)
                self.assertIs(raised.exception, failure)
                self.assertFalse(failed[0].exists())
                self.assertEqual(list(existing.parent.iterdir()), [existing])
                self.assertEqual(existing.read_bytes(), png)
                self.assertEqual(source.read_bytes(), png)

    def test_attachment_cleanup_failure_preserves_the_write_exception(self):
        """Raise the original attachment write error even when removing its partial target also fails."""
        task_id = self.create()
        failure = OSError("Original write failure")
        write_bytes = Path.write_bytes

        def partial_write(target, data):
            """Leave partial image bytes before raising the original I/O exception."""
            write_bytes(target, data[:8])
            raise failure

        with patch.object(Path, "write_bytes", autospec=True, side_effect=partial_write), \
             patch.object(Path, "unlink", side_effect=OSError("Cleanup failure")), \
             self.assertRaises(OSError) as raised:
            server._write_attachment_file(task_id, png_bytes())
        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
