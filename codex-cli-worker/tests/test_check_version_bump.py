"""scripts/check_version_bump.py: only the release pull request changes the version."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("check_version_bump", ROOT / "scripts" / "check_version_bump.py")
cvb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cvb)
ReleaseError = sys.modules["fork_release"].ReleaseError

CONFIG = 'name: Codex CLI Worker\nversion: "{v}"\nslug: codex_cli_worker\n'
MANIFEST = '{{\n  "domain": "codex_cli",\n  "name": "Codex",\n  "version": "{v}"\n}}\n'


def run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class VersionRuleTests(unittest.TestCase):
    """A small repository: main at 0.3.5, and a branch from it that each test changes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        run(self.repo, "init", "-q", "-b", "main")
        run(self.repo, "config", "user.email", "test@example.com")
        run(self.repo, "config", "user.name", "Test")
        run(self.repo, "config", "commit.gpgsign", "false")
        self.set_versions("0.3.5")
        self.commit("start")
        run(self.repo, "checkout", "-q", "-b", "feature")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_versions(self, config: str, manifest: str | None = None) -> None:
        for path, text in ((cvb.WORKER_CONFIG, CONFIG.format(v=config)),
                           (cvb.MANIFEST, MANIFEST.format(v=manifest or config))):
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        run(self.repo, "add", "-A")
        run(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return run(self.repo, "rev-parse", "HEAD")

    def check(self, title: str) -> str:
        return cvb.check("main", "feature", title, self.repo)

    # pull requests that keep the version

    def test_a_feature_that_leaves_the_version_alone_passes(self) -> None:
        (self.repo / "codex-cli-worker" / "server.py").write_text("print('new')\n", encoding="utf-8")
        self.commit("feature")
        self.assertEqual(self.check("Add a feature"), "ok: the version is unchanged (0.3.5)")

    def test_a_release_merged_to_main_after_the_branch_started_does_not_count_against_it(self) -> None:
        self.commit("feature")
        run(self.repo, "checkout", "-q", "main")
        self.set_versions("0.3.6")
        self.commit("Release 0.3.6")
        run(self.repo, "checkout", "-q", "feature")
        self.assertIn("unchanged (0.3.5)", self.check("Add a feature"))

    def test_a_raise_and_a_return_to_the_same_version_is_no_change(self) -> None:
        self.set_versions("0.3.6")
        self.commit("raise")
        self.set_versions("0.3.5")
        self.commit("hold the version")
        self.assertIn("unchanged", self.check("Add a feature"))

    # feature pull requests that change the version

    def test_a_feature_that_raises_the_version_is_refused(self) -> None:
        self.set_versions("0.3.6")
        self.commit("feature and version")
        with self.assertRaisesRegex(cvb.VersionRuleError, "changes the version .*0.3.5 -> 0.3.6"):
            self.check("Remove the grant layer")

    def test_a_change_to_one_file_only_is_refused(self) -> None:
        self.set_versions("0.3.5", manifest="0.3.6")
        self.commit("manifest only")
        with self.assertRaises(cvb.VersionRuleError):
            self.check("Fix the manifest")

    def test_a_lower_version_is_refused_too(self) -> None:
        self.set_versions("0.3.4")
        self.commit("lower")
        with self.assertRaises(cvb.VersionRuleError):
            self.check("Hold the version")

    def test_titles_that_only_mention_a_release_are_not_release_titles(self) -> None:
        self.set_versions("0.3.6")
        self.commit("raise")
        for title in ("release 0.3.6", "Prepare Release 0.3.6", "Release notes for 0.3.6", "Releases 0.3.6",
                      "Release", "Release: 0.3.6", ""):
            with self.subTest(title=title), self.assertRaises(cvb.VersionRuleError):
                self.check(title)

    # release pull requests

    def test_the_release_pull_request_for_the_new_version_passes(self) -> None:
        self.set_versions("0.3.6")
        self.commit("release")
        for title in ("Release 0.3.6", "Release v0.3.6", "Release 0.3.6: roll back to 0.3.4",
                      "Release 0.3.6 with the new settings page", "  Release 0.3.6"):
            with self.subTest(title=title):
                self.assertIn("ok: release pull request for 0.3.6", self.check(title))

    def test_a_release_title_with_another_version_is_refused(self) -> None:
        self.set_versions("0.3.6")
        self.commit("release")
        with self.assertRaisesRegex(cvb.VersionRuleError, "the title names 0.3.7"):
            self.check("Release 0.3.7")
        with self.assertRaisesRegex(cvb.VersionRuleError, "the title names 0.3.6.1"):
            self.check("Release 0.3.6.1")

    def test_a_release_must_set_both_files(self) -> None:
        self.set_versions("0.3.6", manifest="0.3.5")
        self.commit("half a release")
        with self.assertRaisesRegex(cvb.VersionRuleError, "both must be 0.3.6"):
            self.check("Release 0.3.6")

    def test_a_beta_version_never_goes_to_main(self) -> None:
        self.set_versions("0.4.0-beta.1")
        self.commit("beta")
        with self.assertRaisesRegex(cvb.VersionRuleError, "beta branch"):
            self.check("Release 0.4.0-beta.1")

    def test_a_release_title_without_a_version_change_passes(self) -> None:
        self.commit("changelog heading only")
        self.assertIn("unchanged", self.check("Release 0.3.6"))

    # the command line

    def main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cvb.main(list(args), root=self.repo)
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes(self) -> None:
        self.set_versions("0.3.6")
        self.commit("raise")
        self.assertEqual(self.main("--base", "main", "--head", "feature", "--title", "Release 0.3.6")[0], 0)
        code, _, err = self.main("--base", "main", "--head", "feature", "--title", "Add a feature")
        self.assertEqual(code, 1)
        self.assertIn("refused: this pull request changes the version", err)
        code, _, err = self.main("--base", "main", "--head", "no-such-branch", "--title", "Add a feature")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_a_missing_version_file_is_an_error_not_a_pass(self) -> None:
        (self.repo / cvb.MANIFEST).unlink()
        self.commit("lose the manifest")
        with self.assertRaises(ReleaseError):
            self.check("Add a feature")


if __name__ == "__main__":
    unittest.main()
