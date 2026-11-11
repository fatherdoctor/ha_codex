"""scripts/build_hacs_release.py: an "(unreleased)" changelog heading passes validation and never a release."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_hacs_release", ROOT / "scripts" / "build_hacs_release.py")
bhr = importlib.util.module_from_spec(SPEC)
sys.modules["build_hacs_release"] = bhr
SPEC.loader.exec_module(bhr)


def run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class BuildHacsReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.out = Path(self.tmp.name) / "out"
        self.repo.mkdir()
        self.saved = (bhr.ROOT, bhr.CHANGELOG)
        bhr.ROOT = self.repo
        bhr.CHANGELOG = self.repo / "codex-cli-worker" / "CHANGELOG.md"
        run(self.repo, "init", "-q", "-b", "main")
        run(self.repo, "config", "user.email", "test@example.com")
        run(self.repo, "config", "user.name", "Test")
        run(self.repo, "config", "commit.gpgsign", "false")

    def tearDown(self) -> None:
        bhr.ROOT, bhr.CHANGELOG = self.saved
        self.tmp.cleanup()

    def make(self, version: str, heading: str) -> None:
        files = {
            "custom_components/codex_cli/__init__.py": "",
            "custom_components/codex_cli/manifest.json": json.dumps({"domain": "codex_cli", "version": version}),
            "codex-cli-worker/config.yaml": f'name: Codex CLI Worker\nversion: "{version}"\n',
            "codex-cli-worker/CHANGELOG.md": f"# Changelog\n\n{heading}\n\n- Changes.\n\n## 0.1.81\n\n- Older.\n",
            "hacs.json": json.dumps({"zip_release": True, "filename": "codex_cli.zip", "hide_default_branch": True}),
        }
        for path, text in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        run(self.repo, "add", "-A")
        run(self.repo, "commit", "-q", "-m", version)

    def test_released_heading_builds(self) -> None:
        self.make("0.1.81.2", "## 0.1.81.2")
        self.assertTrue(bhr.build("v0.1.81.2", self.out).is_file())
        self.assertEqual((self.out / "release_notes.md").read_text(encoding="utf-8"), "- Changes.\n")

    def test_unreleased_heading_passes_validation(self) -> None:
        self.make("0.1.81.2", "## 0.1.81.2 (unreleased)")
        self.assertTrue(bhr.build("v0.1.81.2", self.out, allow_unreleased=True).is_file())
        self.assertEqual((self.out / "release_notes.md").read_text(encoding="utf-8"), "- Changes.\n")

    def test_unreleased_heading_is_never_released(self) -> None:
        self.make("0.1.81.2", "## 0.1.81.2 (unreleased)")
        with self.assertRaisesRegex(ValueError, r"still marks 0\.1\.81\.2 as \(unreleased\)"):
            bhr.build("v0.1.81.2", self.out)

    def test_missing_heading_fails_either_way(self) -> None:
        self.make("0.1.81.2", "## 0.1.81.3 (unreleased)")
        for allow in (False, True):
            with self.assertRaisesRegex(ValueError, "has no heading for v0.1.81.2"):
                bhr.build("v0.1.81.2", self.out, allow_unreleased=allow)

    def test_command_line(self) -> None:
        self.make("0.1.81.2", "## 0.1.81.2 (unreleased)")
        # The script finds the repository from its own path, so run a copy placed inside the test repository.
        copy = self.repo / "scripts" / "build_hacs_release.py"
        copy.parent.mkdir()
        copy.write_text((ROOT / "scripts" / "build_hacs_release.py").read_text(encoding="utf-8"), encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if k != "GITHUB_REF_TYPE"}

        def call(*args: str, ref_type: str | None = None) -> subprocess.CompletedProcess:
            env = dict(base, GITHUB_REF_TYPE=ref_type) if ref_type else base
            return subprocess.run([sys.executable, str(copy), *args, "v0.1.81.2", str(self.out)],
                                  capture_output=True, text=True, env=env)

        for result in (call(ref_type="branch"), call()):
            self.assertEqual(result.returncode, 0, result.stderr)
        for result in (call("--release"), call(ref_type="tag")):
            self.assertEqual(result.returncode, 1)
            self.assertIn("(unreleased)", result.stderr)

if __name__ == "__main__":
    unittest.main()
