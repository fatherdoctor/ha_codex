"""Record the launcher's argument, environment, and failure behavior."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hass_mcp_launch as launcher  # noqa: E402


class CredentialSourceTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixture_file = root / "fixture"

    def test_environment_value_takes_precedence_over_the_file(self):
        self.fixture_file.write_text("sample-file-value\n", encoding="utf-8")
        value = launcher.token(
            {launcher.KEY_ENV: " sample-relay-value\n"}, self.fixture_file
        )
        self.assertEqual(value, "sample-relay-value")

    def test_blank_environment_value_uses_the_file(self):
        self.fixture_file.write_text(" sample-file-value\n", encoding="utf-8")
        value = launcher.token({launcher.KEY_ENV: " \n"}, self.fixture_file)
        self.assertEqual(value, "sample-file-value")

    def test_missing_or_blank_file_returns_an_empty_value(self):
        self.assertEqual(launcher.token({}, self.fixture_file), "")
        self.fixture_file.write_text(" \n", encoding="utf-8")
        self.assertEqual(launcher.token({}, self.fixture_file), "")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.execve = Mock()
        self.environ = {}
        self.enterContext(
            patch.object(launcher, "os", SimpleNamespace(environ=self.environ, execve=self.execve))
        )

    def test_server_environment_sets_its_options_and_preserves_unrelated_values(self):
        source = {
            launcher.KEY_ENV: "sample-relay-value",
            "HA_URL": "previous-url",
            "HA_TOKEN": "previous-value",
            "HASS_MCP_BACKUP_DIR": "/sample/previous",
            "LANG": "C",
        }
        before = source.copy()
        self.assertEqual(
            launcher.server_env(source, "sample-value"),
            {
                "HA_URL": launcher.CORE_URL,
                "HA_TOKEN": "sample-value",
                "HASS_MCP_BACKUP_DIR": launcher.BACKUP_DIR,
                "LANG": "C",
            },
        )
        self.assertEqual(source, before)

    def test_exec_uses_the_server_as_the_only_argument(self):
        self.environ[launcher.KEY_ENV] = " sample-value\n"
        self.environ["LANG"] = "C"
        launcher.main()
        self.execve.assert_called_once_with(
            launcher.SERVER,
            [launcher.SERVER],
            {
                "HA_URL": launcher.CORE_URL,
                "HA_TOKEN": "sample-value",
                "HASS_MCP_BACKUP_DIR": launcher.BACKUP_DIR,
                "LANG": "C",
            },
        )

    def test_missing_credential_reports_error_without_starting_the_server(self):
        stderr = io.StringIO()
        with patch.object(launcher, "token", return_value=""), redirect_stderr(stderr):
            self.assertEqual(launcher.main(), 1)
        self.execve.assert_not_called()
        self.assertIn("no Supervisor token is available", stderr.getvalue())

    def test_exec_failure_reaches_the_caller(self):
        self.environ[launcher.KEY_ENV] = "sample-value"
        self.execve.side_effect = FileNotFoundError("sample executable missing")
        with self.assertRaisesRegex(FileNotFoundError, "sample executable missing"):
            launcher.main()
        self.execve.assert_called_once()
