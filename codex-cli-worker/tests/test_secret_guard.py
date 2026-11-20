"""The add-on keeps no readable copy of secrets.yaml or of any file under .storage.

Home Assistant keeps sign-ins, tokens and integration credentials in .storage, beside
dashboards and other state, and secrets.yaml holds the values that `!secret` names.
The worker leaves both out of the full snapshot and deletes any copy of them in an
exchange's backup folder, in every permission mode. In a sandbox, .storage is
read-only and secrets.yaml is hidden, so Codex can change them only after the
person approves the change on a permission card.
"""
from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_server_fixture import StubAppServer
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


class SandboxTests(unittest.TestCase):
    """In every sandbox .storage is read-only, so a write there fails and Codex must ask for a permission card."""

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
        self.assertIn('READ_ONLY_DIRS="/config/.storage"', source)
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
        return args, self.record.read_text(encoding="utf-8").split("\n")

    def test_storage_is_bound_read_only_after_codex_binds_and_before_the_masks(self):
        self.storage.mkdir(parents=True)
        (self.storage / "auth").write_text("{}", encoding="utf-8")
        (self.root / "config" / "secrets.yaml").write_text("a: b\n", encoding="utf-8")
        args, recorded = self.run_wrapper()
        separator = recorded.index("--")
        before = recorded[:separator]
        storage = str(self.storage)
        read_only = before.index(storage)
        self.assertEqual(before[read_only - 1:read_only + 2], ["--ro-bind", storage, storage])
        # After Codex's own writable bind of /config, so that bind cannot make it writable again.
        self.assertGreater(read_only, before.index(f"{self.root}/config", before.index("--bind")))
        # Before the masks of the hidden files inside it, so they stay hidden.
        self.assertLess(read_only, before.index(str(self.storage / "auth")))
        self.assertIn(str(self.root / "config" / "secrets.yaml"), before)

    def test_a_missing_or_linked_storage_folder_is_not_bound(self):
        _, recorded = self.run_wrapper()
        self.assertNotIn(str(self.storage), recorded)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.storage.parent.mkdir(parents=True, exist_ok=True)
        self.storage.symlink_to(elsewhere)
        _, recorded = self.run_wrapper()
        self.assertNotIn(str(self.storage), recorded)


if __name__ == "__main__":
    unittest.main()
