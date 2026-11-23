"""The add-on keeps no readable copy of secrets.yaml or of any file under .storage.

Home Assistant keeps sign-ins, tokens and integration credentials in .storage, beside
dashboards and other state, and secrets.yaml holds the values that `!secret` names.
The worker leaves both out of the full snapshot and deletes any copy of them in an
exchange's backup folder, in every permission mode. In a sandbox, secrets.yaml
and the sign-in files in .storage are hidden; the rest of .storage is writable
like the rest of /config, as in the Claude add-on.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app_server_fixture import StubAppServer
import test_approvals
import test_backups
from test_server import server

WORKER = Path(__file__).resolve().parents[1]


class SecretFilePathTests(unittest.TestCase):
    """Which paths under /config count as secret files."""

    def test_every_secrets_file_and_everything_under_storage_is_secret(self):
        for rel in ("secrets.yaml", "secrets.yml", "packages/secrets.yaml", ".storage/auth",
                    ".storage/lovelace.kitchen", ".storage/core.config_entries", ".storage/icloud/jar.cookiejar",
                    ".storage/core.restore_state"):
            with self.subTest(rel=rel):
                self.assertTrue(server.secret_file_path(rel))

    def test_ordinary_files_and_lookalike_names_are_not_secret(self):
        for rel in ("automations.yaml", "storage/lovelace", ".storage_notes.yaml", "packages/.storage/x",
                    "secrets.yaml.example", "my_secrets.yaml", ""):
            with self.subTest(rel=rel):
                self.assertFalse(server.secret_file_path(rel))


class SnapshotAndBackupTests(unittest.TestCase):
    """The full snapshot and the backup folder never hold a secret file."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = root / "config"
        self.run_dir = root / "run"
        self.backups = self.run_dir / server.BACKUP_DIR
        self.backups.mkdir(parents=True)
        patcher = patch.object(server, "CONFIG_ROOT", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, base, rel, content="{}"):
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_full_snapshot_leaves_out_secrets_and_all_of_storage(self):
        for rel in ("automations.yaml", "secrets.yaml", "packages/secrets.yaml", ".storage/lovelace.kitchen",
                    ".storage/core.restore_state"):
            self.write(self.config, rel)
        with patch.object(server, "get_run_dir", return_value=self.run_dir):
            snapshot = server.create_snapshot("chat")
        with tarfile.open(snapshot["path"]) as archive:
            self.assertEqual(archive.getnames(), ["automations.yaml"])
        self.assertEqual(snapshot["file_count"], 1)

    def test_copies_of_secret_files_are_deleted_and_never_offered_as_saved(self):
        for rel in ("secrets.yaml", "packages/secrets.yaml", ".storage/lovelace.kitchen", ".storage/icloud/a.cookiejar"):
            self.write(self.config, rel)
            self.write(self.backups, rel)
        kept = self.write(self.backups, "automations.yaml", "x")
        server.drop_credential_backups(self.run_dir)
        self.assertEqual([path.relative_to(self.backups).as_posix() for path in self.backups.rglob("*") if path.is_file()],
                         ["automations.yaml"])
        self.assertTrue(kept.exists())
        before = {".storage/lovelace.kitchen": {"sha256": server.file_hash(self.config / ".storage/lovelace.kitchen")}}
        self.write(self.backups, ".storage/lovelace.kitchen")
        self.assertEqual(server.previous_versions(self.run_dir, [".storage/lovelace.kitchen"], before),
                         [{"path": ".storage/lovelace.kitchen", "status": "excluded", "copy": ""}])

    def test_codex_is_told_never_to_copy_secret_files(self):
        text = server.backup_instructions(self.backups)
        self.assertIn("Never copy secrets.yaml or any file under .storage", text)
        self.assertNotIn(".storage/lovelace", text)


class FullAccessTurnTests(unittest.TestCase):
    """A Full access turn, which runs with no sandbox and no permission cards, still keeps no secret copy."""

    # The temporary config tree and the faked Home Assistant of the backup tests' runs.
    setUp = test_backups.RunTests.setUp

    def test_full_access_turn_leaves_no_copy_of_a_secret_file(self):
        self.options["full_snapshot"] = True
        (self.config / "secrets.yaml").write_text("api_key: invented\n", encoding="utf-8")
        server.tasks[self.task_id]["permission_mode"] = server.protected_policy.FULL_ACCESS_MODE
        backups = self.run_dir / "backups"

        def change_files():
            # Codex outside the sandbox copies the secret files before it edits them, against its instructions.
            for rel in ("secrets.yaml", ".storage/core.restore_state", "automations.yaml"):
                target = backups / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((self.config / rel).read_bytes())
            (self.config / "secrets.yaml").write_text("api_key: changed\n", encoding="utf-8")
            (self.config / ".storage" / "core.restore_state").write_text('{"data": 2}', encoding="utf-8")
            (self.config / "automations.yaml").write_text("- alias: new\n", encoding="utf-8")

        process = StubAppServer(
            "019fc242-910a-7c92-a17d-54c014e19fc4",
            {"status": "completed", "summary": "Done", "question": "", "details": "Notes"}, on_turn=change_files,
        )
        with patch.object(server.full_access, "is_on", return_value=True), \
             patch.object(server.subprocess, "Popen", return_value=process):
            self.assertEqual(server.task_mode_settings(self.task_id)["sandbox"], "danger-full-access")
            server.run_task(self.task_id, "Change the key")
        self.assertEqual(server.tasks[self.task_id]["status"], "completed")
        left = sorted(path.relative_to(backups).as_posix() for path in backups.rglob("*") if path.is_file())
        self.assertEqual(left, ["automations.yaml"])
        with tarfile.open(self.run_dir / server.SNAPSHOT_FILE) as archive:
            self.assertEqual(sorted(archive.getnames()), ["automations.yaml", "scripts.yaml"])


class StoppedExchangeTests(unittest.TestCase):
    """A message the person stops, or one a restart cuts off, keeps no copy of a secret file either."""

    setUp = test_backups.RunTests.setUp

    def copy_files(self):
        # Codex copies the secret files before it edits them, against its instructions.
        backups = self.run_dir / "backups"
        for rel in ("secrets.yaml", ".storage/core.restore_state", "automations.yaml"):
            target = backups / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.config / rel).read_bytes())

    def left_in_backups(self):
        backups = self.run_dir / "backups"
        return sorted(path.relative_to(backups).as_posix() for path in backups.rglob("*") if path.is_file())

    def test_stopping_a_full_access_message_deletes_the_secret_copies(self):
        (self.config / "secrets.yaml").write_text("api_key: invented\n", encoding="utf-8")
        server.tasks[self.task_id]["permission_mode"] = server.protected_policy.FULL_ACCESS_MODE
        process = StubAppServer("019fc242-910a-7c92-a17d-54c014e19fc4", on_turn=self.copy_files, hold=True)
        with patch.object(server.full_access, "is_on", return_value=True), \
             patch.object(server.subprocess, "Popen", return_value=process):
            runner = threading.Thread(target=server.run_task, args=(self.task_id, "Change the key"))
            runner.start()
            try:
                self.assertTrue(process.started.wait(5))
                with patch.object(server, "api_token", return_value="test-token"):
                    response = server.app.test_client().post(
                        f"/tasks/{self.task_id}/cancel", headers={"Authorization": "Bearer test-token"})
                self.assertEqual(response.status_code, 200)
            finally:
                process.close()
                runner.join(timeout=10)
        self.assertFalse(runner.is_alive())
        self.assertEqual(server.tasks[self.task_id]["status"], "cancelled")
        self.assertEqual(self.left_in_backups(), ["automations.yaml"])

    def test_the_backup_cleanup_deletes_a_leftover_secret_copy(self):
        # A message that ended without the deletion, as after a crash or a restart, inside the retention time.
        server.tasks[self.task_id].update(status="failed")
        server.tasks[self.task_id]["turns"][0].update(status="failed", completed_at=server.utc_now())
        (self.config / "secrets.yaml").write_text("api_key: invented\n", encoding="utf-8")
        self.copy_files()
        server.cleanup_backups()
        self.assertEqual(self.left_in_backups(), ["automations.yaml"])


class ApprovalRecordTests(unittest.TestCase):
    """The chat's record of a permission card keeps no changed lines of a secret file after the answer."""

    def record(self, status, *paths):
        changes = [{"kind": "update", "path": path, "move_path": "", "diff": "-a: 1\n+a: 2\n"} for path in paths]
        return {"approval_id": "x", "status": status, "kind": "file_change", "changes": changes}

    def test_an_answered_request_drops_the_diff_of_secret_files_only(self):
        config = server.CONFIG_ROOT.as_posix()
        for status in ("allowed", "denied", "expired"):
            with self.subTest(status=status):
                records = [self.record(status, f"{config}/secrets.yaml", f"{config}/.storage/lovelace.kitchen",
                                       "packages/secrets.yaml", f"{config}/automations.yaml", "/share/secrets.yaml")]
                server.forget_secret_diffs(records)
                self.assertEqual([change["diff"] for change in records[0]["changes"]],
                                 ["", "", "", "-a: 1\n+a: 2\n", "-a: 1\n+a: 2\n"])

    def test_a_pending_request_keeps_the_diff_the_person_reads(self):
        records = [self.record("pending", f"{server.CONFIG_ROOT.as_posix()}/secrets.yaml")]
        server.forget_secret_diffs(records)
        self.assertEqual(records[0]["changes"][0]["diff"], "-a: 1\n+a: 2\n")

    def test_the_saved_chat_keeps_the_diff_only_while_the_card_waits(self):
        api = test_approvals.ApprovalApiTests("test_prompts_are_counted_by_kind_for_the_conversation")
        api.setUp()
        self.addCleanup(api.doCleanups)
        secret = f"{server.CONFIG_ROOT.as_posix()}/secrets.yaml"
        api.session.receive({"method": "item/fileChange/patchUpdated", "params": {
            "threadId": "thread", "turnId": "turn", "itemId": "edit", "changes": [
                {"path": secret, "diff": "-api_key: invented\n+api_key: changed\n", "kind": {"type": "update"}}]}})
        api.session.receive({"id": 11, "method": test_approvals.FILE_APPROVAL, "params": {
            "threadId": "thread", "turnId": "turn", "itemId": "edit", "reason": "Change the key"}})
        saved = Path(api.temp.name) / "task" / "task.json"
        self.assertIn("api_key: changed", saved.read_text(encoding="utf-8"))
        api.approval_id = server.tasks["task"]["approvals"][1]["approval_id"]
        self.assertEqual(api.answer().status_code, 200)
        self.assertEqual(server.tasks["task"]["approvals"][1]["status"], "allowed")
        self.assertNotIn("api_key", saved.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", json.dumps(server.tasks["task"]))

    def test_a_restart_drops_the_diff_of_a_card_that_waited_from_the_saved_chat(self):
        """A card for secrets.yaml that waited when the app stopped expires, and task.json loses its changed lines."""
        api = test_approvals.ApprovalApiTests("test_prompts_are_counted_by_kind_for_the_conversation")
        api.setUp()
        self.addCleanup(api.doCleanups)
        config = server.CONFIG_ROOT.as_posix()
        api.session.receive({"method": "item/fileChange/patchUpdated", "params": {
            "threadId": "thread", "turnId": "turn", "itemId": "edit", "changes": [
                {"path": f"{config}/secrets.yaml", "diff": "-api_key: invented\n+api_key: changed\n", "kind": {"type": "update"}},
                {"path": f"{config}/automations.yaml", "diff": "-alias: Old\n+alias: New\n", "kind": {"type": "update"}}]}})
        api.session.receive({"id": 11, "method": test_approvals.FILE_APPROVAL, "params": {
            "threadId": "thread", "turnId": "turn", "itemId": "edit", "reason": "Change the key"}})
        saved = Path(api.temp.name) / "task" / "task.json"
        self.assertIn("api_key: changed", saved.read_text(encoding="utf-8"))
        self.assertEqual(server.tasks["task"]["approvals"][1]["status"], "pending")

        server.tasks.clear()
        server.approval_sessions.clear()
        server.load_task_index()
        restored = server.tasks["task"]
        self.assertEqual(restored["approvals"][1]["status"], "expired")
        self.assertNotIn("api_key", json.dumps(restored))
        self.assertNotIn("api_key", saved.read_text(encoding="utf-8"))
        # Only the secret file's lines go: the card still shows the change to an ordinary file.
        self.assertIn("alias: New", saved.read_text(encoding="utf-8"))
        self.assertIn("alias: New", json.dumps(restored))

    def test_a_restart_drops_the_diff_of_a_secret_file_from_the_task_index(self):
        """The task index holds each chat's approvals too; it is written again without the changed lines."""
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp) / "task_index.json"
            config = server.CONFIG_ROOT.as_posix()
            card = {"approval_id": "x", "status": "pending", "kind": "file_change", "changes": [
                {"kind": "update", "path": f"{config}/.storage/core.config_entries", "move_path": "",
                 "diff": "-token: invented\n+token: changed\n"}]}
            index.write_text(json.dumps({"chat": {"task_id": "chat", "status": "completed", "approvals": [card]}}),
                             encoding="utf-8")
            with patch.object(server, "tasks", {}), patch.object(server, "task_root", return_value=Path(temp) / "tasks"), \
                    patch.object(server, "TASK_STATE_FILE", index):
                server.load_task_index()
                self.assertEqual(server.tasks["chat"]["approvals"][0]["status"], "expired")
                self.assertNotIn("token: changed", json.dumps(server.tasks["chat"]))
            self.assertNotIn("token: changed", index.read_text(encoding="utf-8"))

    def test_a_rename_into_a_secret_file_drops_the_diff(self):
        record = self.record("allowed", f"{server.CONFIG_ROOT.as_posix()}/notes.yaml")
        record["changes"][0]["move_path"] = f"{server.CONFIG_ROOT.as_posix()}/secrets.yaml"
        server.forget_secret_diffs([record])
        self.assertEqual(record["changes"][0]["diff"], "")


class SandboxTests(unittest.TestCase):
    """In every sandbox .storage stays writable under Codex's bind of /config; only its sign-in files are hidden."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.record = self.root / "record.txt"
        real = self.root / "bwrap.real"
        real.write_text(f"#!{sys.executable}\nimport sys\nopen({str(self.record)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n",
                        encoding="utf-8")
        real.chmod(0o755)
        self.wrapper = self.root / "bwrap"
        source = (WORKER / "codex-bwrap").read_text(encoding="utf-8")
        self.wrapper.write_text(
            source.replace("REAL_BWRAP=/opt/codex-sandbox/bwrap.real", f"REAL_BWRAP={real}")
            .replace("/config/", f"{self.root}/config/"),
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)
        self.storage = self.root / "config" / ".storage"

    def run_wrapper(self):
        args = ["--ro-bind", "/", "/", "--bind", f"{self.root}/config", f"{self.root}/config", "--", "/bin/sh", "-c", "true"]
        subprocess.run([str(self.wrapper), *args], check=True)
        return self.record.read_text(encoding="utf-8").split("\n")

    def test_storage_gets_no_read_only_bind_and_its_sign_in_files_stay_hidden(self):
        self.storage.mkdir(parents=True)
        (self.storage / "auth").write_text("{}", encoding="utf-8")
        (self.storage / "auth_provider.homeassistant").write_text("{}", encoding="utf-8")
        (self.storage / "lovelace").write_text("{}", encoding="utf-8")
        recorded = self.run_wrapper()
        before = recorded[:recorded.index("--")]
        # No bind names the folder itself, so Codex's writable bind of /config covers it.
        self.assertNotIn(str(self.storage), before)
        for name in ("auth", "auth_provider.homeassistant"):
            with self.subTest(name=name):
                hidden = before.index(str(self.storage / name))
                self.assertEqual(before[hidden - 2:hidden + 1], ["--ro-bind", "/dev/null", str(self.storage / name)])
        self.assertNotIn(str(self.storage / "lovelace"), before)


if __name__ == "__main__":
    unittest.main()
