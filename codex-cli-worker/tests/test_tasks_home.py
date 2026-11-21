"""The app keeps its working files in its private /data, never in Home Assistant's configuration folder.

Chats (prompts, logs, per-turn backups, attachments) live in /data/codex_tasks. Versions before 0.4 kept
them in /config/codex_tasks (or the folder the removed task_root option named) and wrote the sign-in QR
images to /config/www/codex_cli_auth, which Home Assistant serves at /local/ without a sign-in. The first
start of 0.4 moves the chats (never copies them) and deletes those images.
"""

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_folders import isolated_app_folders  # noqa: E402
from test_server import server  # noqa: E402

WORKER = Path(__file__).resolve().parents[1]


def tree(root: Path) -> dict[str, str]:
    """Every file under root by its relative path, with its text."""
    return {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*")) if path.is_file()}


class Sandbox(unittest.TestCase):
    """A temporary /config and /data for one test."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.config.mkdir()
        self.data.mkdir()
        for key, value in {"CONFIG_ROOT": self.config, "DATA_ROOT": self.data,
                           "OLD_AUTH_QR_DIR": self.config / "www" / "codex_cli_auth"}.items():
            patcher = patch.object(server, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def old_chat(self, root: Path, task_id: str, log: str = "invented log line\n") -> None:
        """A chat folder as an older version left it."""
        folder = root / task_id
        (folder / "turns" / "t1" / "backups").mkdir(parents=True)
        (folder / "task.json").write_text('{"task_id": "%s"}' % task_id, encoding="utf-8")
        (folder / "codex.log").write_text(log, encoding="utf-8")
        (folder / "turns" / "t1" / "backups" / "automations.yaml").write_text("- alias: before\n", encoding="utf-8")


class TaskFolder(Sandbox):

    def test_the_task_root_is_in_the_private_data_folder(self):
        self.assertEqual(server.task_root(), self.data / "codex_tasks")
        self.assertEqual(server.get_task_dir("chat"), self.data / "codex_tasks" / "chat")

    def test_the_task_root_option_is_gone(self):
        config = (WORKER / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("task_root", config)
        self.assertNotIn("task_root", (WORKER / "translations" / "en.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("task_root", server.DEFAULT_OPTIONS)
        # A saved value from an older version changes nothing.
        with patch.object(server, "read_options", return_value={"task_root": str(self.config / "elsewhere")}):
            self.assertEqual(server.task_root(), self.data / "codex_tasks")

    def test_start_script_makes_no_folder_in_the_configuration_folder(self):
        run = (WORKER / "run.sh").read_text(encoding="utf-8")
        for line in run.splitlines():
            if "mkdir" in line:
                self.assertNotIn("/config", line)
        self.assertIn("/data/codex_tasks", run)


class Migration(Sandbox):

    def test_chats_are_moved_and_the_old_folder_is_removed(self):
        old = self.config / "codex_tasks"
        self.old_chat(old, "chat-a")
        self.old_chat(old, "auth", log="invented sign-in log\n")
        before = tree(old)
        result = server.migrate_task_folders({})
        self.assertEqual(result["moved"], 2)
        self.assertFalse(old.exists(), "the old folder is removed, not left empty")
        self.assertEqual(tree(self.data / "codex_tasks"), before)
        self.assertEqual(os.stat(self.data / "codex_tasks").st_mode & 0o777, 0o700)

    def test_a_move_across_file_systems_copies_whole_then_deletes_the_source(self):
        old = self.config / "codex_tasks"
        self.old_chat(old, "chat-a")
        before = tree(old)
        real_rename = os.rename
        calls = []

        def rename(source, target):
            calls.append((Path(source), Path(target)))
            if Path(source).parent == old:  # /config and /data are different file systems on an install.
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(source, target)

        with patch.object(server.os, "rename", side_effect=rename):
            result = server.migrate_task_folders({})
        self.assertEqual(result, {"moved": 1, "kept": 0, "removed_folders": 1, "failed": 0})
        # The whole copy is renamed into place from the staging folder.
        self.assertEqual(calls[-1], (self.data / "codex_tasks.moving" / "chat-a", self.data / "codex_tasks" / "chat-a"))
        self.assertFalse(old.exists())
        self.assertFalse((self.data / "codex_tasks.moving").exists())
        self.assertEqual(tree(self.data / "codex_tasks"), before)

    def test_a_failed_copy_leaves_the_old_chat_and_no_half_copy(self):
        old = self.config / "codex_tasks"
        self.old_chat(old, "chat-a")
        before = tree(old)

        def rename(source, target):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with patch.object(server.os, "rename", side_effect=rename):
            result = server.migrate_task_folders({})
        self.assertEqual(result["failed"], 1)
        self.assertEqual(tree(old), before, "the source stays whole when the move did not finish")
        self.assertFalse((self.data / "codex_tasks" / "chat-a").exists())
        self.assertFalse((self.data / "codex_tasks.moving").exists())

    def test_a_chat_already_moved_by_an_interrupted_start_is_kept_and_the_old_one_deleted(self):
        old = self.config / "codex_tasks"
        self.old_chat(old, "chat-a", log="old\n")
        self.old_chat(self.data / "codex_tasks", "chat-a", log="moved\n")
        result = server.migrate_task_folders({})
        self.assertEqual(result["kept"], 1)
        self.assertFalse(old.exists())
        self.assertEqual((self.data / "codex_tasks" / "chat-a" / "codex.log").read_text(), "moved\n")

    def test_the_folder_the_old_option_named_is_moved_too(self):
        custom = self.root / "share" / "codex"
        self.old_chat(custom, "chat-b")
        server.migrate_task_folders({"task_root": str(custom)})
        self.assertFalse(custom.exists())
        self.assertTrue((self.data / "codex_tasks" / "chat-b" / "task.json").is_file())

    def test_the_data_folder_itself_is_never_a_source(self):
        (self.data / "options.json").write_text("{}", encoding="utf-8")
        for saved in (str(self.data), str(self.data / "codex_tasks"), str(self.data / "codex_tasks" / "x"), "relative"):
            self.assertEqual(server.old_task_roots({"task_root": saved}), [self.config / "codex_tasks"], saved)
        server.migrate_task_folders({"task_root": str(self.data)})
        self.assertTrue((self.data / "options.json").is_file())

    def test_a_second_start_changes_nothing(self):
        self.old_chat(self.config / "codex_tasks", "chat-a")
        server.migrate_task_folders({})
        after = tree(self.data)
        self.assertEqual(server.migrate_task_folders({}), {"moved": 0, "kept": 0, "removed_folders": 0, "failed": 0})
        self.assertEqual(tree(self.data), after)

    def test_a_link_in_place_of_the_old_folder_is_not_followed(self):
        elsewhere = self.root / "elsewhere"
        self.old_chat(elsewhere, "not-ours")
        (self.config / "codex_tasks").symlink_to(elsewhere)
        server.migrate_task_folders({})
        self.assertTrue((elsewhere / "not-ours" / "task.json").is_file())
        self.assertFalse((self.data / "codex_tasks" / "not-ours").exists())

    def test_the_moved_chats_load_at_start(self):
        self.old_chat(self.config / "codex_tasks", "chat-a")
        server.migrate_task_folders({})
        with patch.object(server, "tasks", {}), patch.object(server, "TASK_STATE_FILE", self.data / "index.json"):
            server.load_task_index()
            self.assertIn("chat-a", server.tasks)


class SavedFolder(Sandbox):
    """A folder saved in the removed task_root option may hold other files: only the chats leave it."""

    def test_the_configuration_folder_as_the_saved_value_keeps_its_files(self):
        (self.config / ".storage").mkdir()
        (self.config / ".storage" / "core.config_entries").write_text("{}", encoding="utf-8")
        (self.config / "configuration.yaml").write_text("homeassistant:\n", encoding="utf-8")
        (self.config / "packages").mkdir()
        self.old_chat(self.config, "chat-in-config")
        (self.config / "auth").mkdir()
        (self.config / "auth" / "codex.log").write_text("invented sign-in line\n", encoding="utf-8")
        result = server.migrate_task_folders({"task_root": str(self.config)})
        self.assertEqual(result["moved"], 2)
        self.assertTrue((self.config / "configuration.yaml").is_file())
        self.assertTrue((self.config / ".storage" / "core.config_entries").is_file())
        self.assertTrue((self.config / "packages").is_dir())
        self.assertFalse((self.config / "chat-in-config").exists())
        self.assertFalse((self.config / "auth").exists())
        self.assertTrue((self.data / "codex_tasks" / "chat-in-config" / "task.json").is_file())
        self.assertTrue((self.data / "codex_tasks" / "auth" / "codex.log").is_file())
        self.assertFalse((self.data / "codex_tasks" / "configuration.yaml").exists())
        self.assertFalse((self.data / "codex_tasks" / ".storage").exists())

    def test_a_shared_folder_is_removed_only_when_it_is_then_empty(self):
        shared = self.root / "share"
        self.old_chat(shared, "chat-b")
        (shared / "photo.jpg").write_text("invented", encoding="utf-8")
        (shared / "notes").mkdir()
        (shared / "notes" / "task.json.bak").write_text("{}", encoding="utf-8")
        server.migrate_task_folders({"task_root": str(shared)})
        self.assertTrue((shared / "photo.jpg").is_file())
        self.assertTrue((shared / "notes" / "task.json.bak").is_file())
        self.assertFalse((shared / "chat-b").exists())

    def test_a_linked_task_json_does_not_make_a_chat(self):
        shared = self.root / "share"
        (shared / "other").mkdir(parents=True)
        (self.root / "real.json").write_text("{}", encoding="utf-8")
        (shared / "other" / "task.json").symlink_to(self.root / "real.json")
        server.migrate_task_folders({"task_root": str(shared)})
        self.assertTrue((shared / "other" / "task.json").is_symlink())

    def test_the_old_default_folder_still_moves_everything(self):
        old = self.config / "codex_tasks"
        old.mkdir()
        (old / "loose.txt").write_text("invented", encoding="utf-8")
        self.old_chat(old, "chat-a")
        server.migrate_task_folders({})
        self.assertFalse(old.exists())
        self.assertTrue((self.data / "codex_tasks" / "loose.txt").is_file())


class UnreadableFolder(Sandbox):

    def test_a_folder_that_cannot_be_listed_is_logged_and_counted_and_the_start_goes_on(self):
        self.old_chat(self.config / "codex_tasks", "chat-a")
        real_iterdir = Path.iterdir

        def refuse(path):
            if path == self.config / "codex_tasks":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_iterdir(path)

        with patch.object(Path, "iterdir", refuse), patch("builtins.print") as printed:
            result = server.migrate_task_folders({})
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["moved"], 0)
        self.assertTrue((self.config / "codex_tasks" / "chat-a" / "task.json").is_file())
        self.assertTrue(any("Could not list" in str(call) for call in printed.call_args_list))


class StartOrder(unittest.TestCase):
    """main() moves the old chats before it makes the folders and before it reads the saved chats."""

    def test_the_migration_runs_before_the_folders_are_made_and_the_chats_are_read(self):
        calls = []

        class StopHere(Exception):
            """Ends main() once the saved chats would be read."""

        def record(name, stop=False):
            def step(*args, **kwargs):
                calls.append(name)
                if stop:
                    raise StopHere
                return {"version": "", "error": "", "message": ""}
            return step

        steps = ["write_starter_agents_file", "migrate_task_folders", "retire_public_sign_in_images",
                 "ensure_runtime_files", "write_full_access_requirements_at_start", "apply_interaction_limit",
                 "refresh_core_address", "codex_version_status", "sandbox_readiness",
                 # Steps a later version of main() adds around these; replaced when present so the test never
                 # touches the real /config or /data.
                 "migrate_agents_file", "link_agents_file"]
        patchers = [patch.object(server, name, side_effect=record(name)) for name in steps if hasattr(server, name)]
        # The steps above are replaced; the ones it does not name still run in a temporary copy of the folders.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(isolated_app_folders(server, Path(temp.name)).close)
        patchers += [
            patch.object(server, "read_options", return_value={}),
            patch.object(server.protected_policy, "policy_problem", return_value=""),
            patch.object(server, "make_worker_undumpable", return_value=True),
            patch.object(server.interaction_logs, "close_abandoned"),
            patch.object(server, "load_task_index", side_effect=record("load_task_index", stop=True)),
            patch("builtins.print"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(StopHere):
            server.main()
        self.assertLess(calls.index("migrate_task_folders"), calls.index("ensure_runtime_files"))
        self.assertLess(calls.index("migrate_task_folders"), calls.index("load_task_index"))


class SignInImages(Sandbox):

    def test_old_images_are_deleted_with_their_folder(self):
        folder = self.config / "www" / "codex_cli_auth"
        folder.mkdir(parents=True)
        for name in ("codex_login_a1.svg", "codex_login_b2.svg", "codex_login_c3.svg"):
            (folder / name).write_text("<svg/>", encoding="utf-8")
        self.assertEqual(server.retire_public_sign_in_images(), 3)
        self.assertFalse(folder.exists())
        self.assertTrue((self.config / "www").is_dir(), "www itself is the owner's and stays")

    def test_a_file_the_app_did_not_write_stays(self):
        folder = self.config / "www" / "codex_cli_auth"
        folder.mkdir(parents=True)
        (folder / "codex_login_a1.svg").write_text("<svg/>", encoding="utf-8")
        (folder / "owner.png").write_text("not ours", encoding="utf-8")
        self.assertEqual(server.retire_public_sign_in_images(), 1)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["owner.png"])

    def test_no_folder_is_no_work(self):
        self.assertEqual(server.retire_public_sign_in_images(), 0)
        self.assertFalse((self.config / "www").exists())

    def test_the_notification_has_no_image_and_writes_no_file(self):
        with patch.object(server, "create_persistent_notification") as notify:
            qr_url = server.notify_codex_login_ready("a1", "https://example.invalid/device", "ABCD-EFGH")
        self.assertEqual(qr_url, "")
        title, message, _ = notify.call_args.args
        self.assertNotIn("![", message)
        self.assertNotIn("/local/", message)
        self.assertIn("https://example.invalid/device", message)
        self.assertIn("ABCD-EFGH", message)
        self.assertEqual(list(self.config.rglob("*")), [])
        self.assertFalse(hasattr(server, "write_login_qr"))


class NothingInTheConfigurationFolder(Sandbox):
    """The start of the app and a sign-in leave Home Assistant's configuration folder as it was."""

    def test_start_and_sign_in_write_nothing_in_the_configuration_folder(self):
        (self.config / "configuration.yaml").write_text("homeassistant:\n", encoding="utf-8")
        before = tree(self.config)
        home = self.data / "codex-home"
        with patch.object(server, "CODEX_HOME", home), \
                patch.object(server, "CODEX_CONFIG_PATH", home / "config.toml"), \
                patch.object(server, "api_token", return_value="synthetic"), \
                patch.object(server, "read_options", return_value={}), \
                patch.object(server, "create_persistent_notification"):
            server.migrate_task_folders({})
            server.retire_public_sign_in_images()
            server.ensure_runtime_files()
            server.notify_codex_login_ready("a1", "https://example.invalid/device", "ABCD-EFGH")
            server.write_task_log("auth", "codex-login", "invented sign-in output\n")
        self.assertEqual(tree(self.config), before)
        self.assertEqual(sorted(path.name for path in self.config.iterdir()), ["configuration.yaml"])
        self.assertTrue((self.data / "codex_tasks" / "auth" / "codex.log").is_file())

    def test_the_worker_names_no_public_folder(self):
        source = (WORKER / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("/local/codex_cli_auth", source)
        www = [line for line in source.splitlines() if '"www"' in line and "CONFIG_ROOT" in line]
        self.assertEqual(www, ['OLD_AUTH_QR_DIR = CONFIG_ROOT / "www" / "codex_cli_auth"'])


class BackupFolder(Sandbox):
    """Codex still keeps its per-turn copies. Its writable roots in /data are the exchange's own backup folder and the
    chat's files folder (CX-FILE-LINK), nothing else."""

    def test_the_backup_folder_is_offered_in_workspace_write(self):
        run_dir = self.data / "codex_tasks" / "chat" / "turns" / "t1"
        folder = server.codex_backup_dir(run_dir, {"codex_sandbox": "workspace-write"})
        self.assertEqual(folder, run_dir / "backups")
        self.assertEqual(server.private_writable_root(folder), [])  # Not made yet.
        folder.mkdir(parents=True)
        self.assertEqual(server.private_writable_root(folder), [str(folder)])

    def test_nothing_else_in_data_becomes_writable(self):
        outside = self.data / "codex-home"
        (outside / "backups").mkdir(parents=True)
        self.assertEqual(server.private_writable_root(outside / "backups"), [])
        run_dir = self.data / "codex_tasks" / "chat" / "turns" / "t1"
        run_dir.mkdir(parents=True)
        (run_dir / "backups").symlink_to(self.data)
        self.assertEqual(server.private_writable_root(run_dir / "backups"), [],
                         "a link out of the task folder is never a root")

    def test_the_turn_policy_adds_only_that_folder(self):
        source = (WORKER / "server.py").read_text(encoding="utf-8")
        self.assertIn('policy["writableRoots"] = [*TASK_WRITABLE_ROOTS, *private_writable_root(final_file.parent / BACKUP_DIR),\n'
                      '                                   *private_writable_root(chat_files_dir(task_id))]', source)
        self.assertEqual(server.TASK_WRITABLE_ROOTS, ("/config", "/share"))


if __name__ == "__main__":
    unittest.main()
