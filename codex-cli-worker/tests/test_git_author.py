"""Commit in two repositories with the environment the worker gives its app-server."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_server import server
from app_folders import isolated_app_folders


class GitAuthorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.enterContext(isolated_app_folders(server, root))
        parent_env = {
            "PATH": os.environ["PATH"],
            "GIT_AUTHOR_NAME": "Inherited Author",
            "GIT_COMMITTER_EMAIL": "inherited@example.invalid",
        }
        self.enterContext(patch.dict(os.environ, parent_env, clear=True))
        self.options = {}
        self.enterContext(
            patch.object(server, "read_options", return_value=self.options)
        )
        self.repo = root / "checkout"
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)

    def commit_identity(self):
        server.ensure_runtime_files()
        env = server.codex_env()
        subprocess.run(
            ["git", "commit", "--quiet", "--allow-empty", "-m", "Example"],
            cwd=self.repo,
            env=env,
            check=True,
        )
        return subprocess.check_output(
            ["git", "log", "-1", "--format=%an <%ae>%n%cn <%ce>"],
            cwd=self.repo,
            env=env,
            text=True,
        ).splitlines()

    def test_default_author_and_committer(self):
        self.options.update(git_author_name=" ", git_author_email="")
        self.assertEqual(
            self.commit_identity(), ["ChatGPT Codex <noreply@openai.com>"] * 2
        )

    def test_owner_override_and_repository_identity(self):
        self.options.update(
            git_author_name="Example Bot", git_author_email="bot@example.invalid"
        )
        self.assertEqual(
            self.commit_identity(), ["Example Bot <bot@example.invalid>"] * 2
        )
        subprocess.run(
            ["git", "config", "--local", "user.name", "Project Author"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "--local", "user.email", "project@example.invalid"],
            cwd=self.repo,
            check=True,
        )
        self.assertEqual(
            self.commit_identity(), ["Project Author <project@example.invalid>"] * 2
        )

    def test_repository_name_can_be_combined_with_the_owner_email(self):
        self.options.update(git_author_email="bot@example.invalid")
        subprocess.run(
            ["git", "config", "--local", "user.name", "Project Author"],
            cwd=self.repo,
            check=True,
        )
        self.assertEqual(
            self.commit_identity(), ["Project Author <bot@example.invalid>"] * 2
        )
