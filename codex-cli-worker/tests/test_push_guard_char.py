"""Record the read-only push guard's verdicts and diagnostics on local Git ranges."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import push_guard  # noqa: E402


class PushGuardRangeTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = root / "repo"
        self.repo.mkdir()
        self.names = root / "names.txt"
        self.shrink = root / "shrink.txt"
        self.binary = root / "binary.txt"
        for path in (self.names, self.shrink, self.binary):
            path.touch()
        self.enterContext(
            patch.dict(
                os.environ,
                {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            )
        )
        self.git("init", "--quiet", "--template=")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture")
        self.write("notes.md", "".join(f"Note {number}\n" for number in range(24)))
        self.commit("Add notes for the example project")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def write(self, path, contents):
        (self.repo / path).write_text(contents, encoding="utf-8")

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)

    def check_range(self, revision=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = push_guard.main(
                [
                    "--repo",
                    str(self.repo),
                    "--range",
                    revision or f"{self.base}..HEAD",
                    "--names",
                    str(self.names),
                    "--shrink",
                    str(self.shrink),
                    "--binary",
                    str(self.binary),
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_ordinary_document_edit_is_clean(self):
        self.write("guide.md", "## Sample project\nAdd a label to the example card.\n")
        self.commit("Document an ordinary edit")
        self.assertEqual(self.check_range(), (0, "push_guard: clean\n", ""))

    def test_database_path_is_refused(self):
        self.write("sample.db", "plain text fixture\n")
        self.commit("Add a database file by mistake")
        code, stdout, stderr = self.check_range()
        self.assertEqual(code, 1)
        self.assertIn("ABORT forbidden path: sample.db\n", stdout)
        self.assertIn("1 finding(s); push refused.", stdout)
        self.assertEqual(stderr, "")

    def test_literal_assignment_is_refused_without_echoing_the_value(self):
        value = "sample-value-123"
        field = "password"
        self.write("settings.yaml", f"enabled: true\n{field}: {value}\n")
        self.commit("Add example settings")
        code, stdout, stderr = self.check_range()
        self.assertEqual(code, 1)
        self.assertIn("ABORT literal secret: settings.yaml:2\n", stdout)
        self.assertNotIn(value, stdout + stderr)

    def test_listed_name_is_refused_in_added_text(self):
        self.names.write_text("samplemember\n", encoding="utf-8")
        self.write("guide.md", "The samplemember checks the example.\n")
        self.commit("Add an example person")
        code, stdout, stderr = self.check_range()
        self.assertEqual(code, 1)
        self.assertIn("ABORT household name: guide.md:1\n", stdout)
        self.assertNotIn("samplemember", stdout + stderr)

    def test_large_cut_to_a_listed_file_is_refused(self):
        self.shrink.write_text("notes.md\n", encoding="utf-8")
        self.write("notes.md", "".join(f"Note {number}\n" for number in range(5)))
        self.commit("Shorten the notes")
        code, stdout, _ = self.check_range()
        self.assertEqual(code, 1)
        self.assertIn(
            "ABORT shrink (24 -> 5 lines; a truncating write?): notes.md\n", stdout
        )

    def test_bad_range_reports_a_git_error(self):
        code, stdout, stderr = self.check_range("missing-example-ref..HEAD")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("push_guard: git failed:", stderr)
