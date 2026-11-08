"""scripts/fork_release.py: a tag only after a lab pass, and a rollback that is the old code under a new number."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fork_release", ROOT / "scripts" / "fork_release.py")
fr = importlib.util.module_from_spec(SPEC)
sys.modules["fork_release"] = fr
SPEC.loader.exec_module(fr)

CONFIG = 'name: Codex CLI Worker\nversion: "{v}"\nslug: codex_cli_worker\n'
MANIFEST = '{{\n  "domain": "codex_cli",\n  "name": "Codex",\n  "version": "{v}"\n}}\n'


def run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class ForkReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        run(self.repo, "init", "-q", "-b", "main")
        run(self.repo, "config", "user.email", "test@example.com")
        run(self.repo, "config", "user.name", "Test")
        run(self.repo, "config", "commit.gpgsign", "false")
        run(self.repo, "config", "tag.gpgsign", "false")
        self.write_version("0.1.81.1", code="print('good')\n", notes="- First fork build.")
        self.good = self.commit("0.1.81.1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # helpers

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def write_version(self, version: str, code: str | None = None, notes: str = "- Changes.") -> None:
        self.write(fr.WORKER_CONFIG, CONFIG.format(v=version))
        self.write(fr.MANIFEST, MANIFEST.format(v=version))
        if code is not None:
            self.write("codex-cli-worker/server.py", code)
        old = (self.repo / fr.CHANGELOG).read_text(encoding="utf-8") if (self.repo / fr.CHANGELOG).exists() else ""
        body = old.split("\n", 2)[2] if old.startswith("# Changelog") else ""
        self.write(fr.CHANGELOG, f"# Changelog\n\n## {version}\n\n{notes}\n\n{body}")

    def commit(self, message: str) -> str:
        run(self.repo, "add", "-A")
        run(self.repo, "commit", "-q", "-m", message)
        return run(self.repo, "rev-parse", "HEAD")

    def record(self, version: str, commit: str, result: str = "pass") -> str:
        self.write(f"{fr.RECORD_DIR}/v{version}.json",
                   json.dumps({"version": version, "commit": commit, "result": result, "lab": "HAOS lab VM"}))
        return self.commit(f"lab pass {version}")

    def release(self, version: str) -> str:
        commit, _ = fr.plan_tag(version, self.repo)
        run(self.repo, "tag", "-a", f"v{version}", commit, "-m", f"v{version}")
        return commit

    # tagging

    def test_no_tag_without_a_lab_pass(self) -> None:
        with self.assertRaisesRegex(fr.ReleaseError, "no lab-pass record"):
            fr.plan_tag("0.1.81.1", self.repo)

    def test_tag_goes_on_the_tested_commit_not_on_the_record(self) -> None:
        self.record("0.1.81.1", self.good)
        commit, why = fr.plan_tag("0.1.81.1", self.repo)
        self.assertEqual(commit, self.good)
        self.assertIn("lab pass", why)

    def test_failed_lab_run_is_not_tagged(self) -> None:
        self.record("0.1.81.1", self.good, result="fail")
        with self.assertRaisesRegex(fr.ReleaseError, "not 'pass'"):
            fr.plan_tag("0.1.81.1", self.repo)

    def test_record_must_name_a_full_commit(self) -> None:
        self.record("0.1.81.1", self.good[:12])
        with self.assertRaisesRegex(fr.ReleaseError, "40-character"):
            fr.plan_tag("0.1.81.1", self.repo)

    def test_tested_commit_must_be_merged(self) -> None:
        run(self.repo, "checkout", "-q", "-b", "side")
        self.write("codex-cli-worker/server.py", "print('side')\n")
        side = self.commit("side")
        run(self.repo, "checkout", "-q", "main")
        self.record("0.1.81.1", side)
        with self.assertRaisesRegex(fr.ReleaseError, "not on HEAD"):
            fr.plan_tag("0.1.81.1", self.repo)

    def test_versions_must_agree_at_the_tested_commit(self) -> None:
        self.write(fr.MANIFEST, MANIFEST.format(v="0.1.81.0"))
        bad = self.commit("mismatch")
        self.record("0.1.81.1", bad)
        with self.assertRaisesRegex(fr.ReleaseError, "integration says 0.1.81.0"):
            fr.plan_tag("0.1.81.1", self.repo)

    def test_a_version_is_never_tagged_twice_or_backwards(self) -> None:
        self.record("0.1.81.1", self.good)
        self.release("0.1.81.1")
        with self.assertRaisesRegex(fr.ReleaseError, "already exists"):
            fr.plan_tag("0.1.81.1", self.repo)
        self.write_version("0.1.81.0")
        older = self.commit("older")
        self.record("0.1.81.0", older)
        with self.assertRaisesRegex(fr.ReleaseError, "not newer"):
            fr.plan_tag("0.1.81.0", self.repo)

    # rollback

    def make_bad_release(self, notes: str = "- Broke something.") -> None:
        self.record("0.1.81.1", self.good)
        self.release("0.1.81.1")
        self.write_version("0.1.81.2", code="print('bad')\n", notes=notes)
        self.write("codex-cli-worker/new_module.py", "x = 1\n")
        bad = self.commit("0.1.81.2")
        self.record("0.1.81.2", bad)
        self.release("0.1.81.2")

    def test_next_version_adds_one_fork_build(self) -> None:
        self.assertEqual(fr.next_version("0.1.81.1"), "0.1.81.2")
        self.assertEqual(fr.next_version("0.1.82"), "0.1.82.1")
        self.assertEqual(fr.next_version("0.1.81.9"), "0.1.81.10")

    def test_rollback_is_the_good_code_under_the_next_number(self) -> None:
        self.make_bad_release()
        new = fr.rollback("0.1.81.1", None, False, self.repo)
        self.assertEqual(new, "0.1.81.3")
        self.assertEqual((self.repo / "codex-cli-worker/server.py").read_text(), "print('good')\n")
        self.assertFalse((self.repo / "codex-cli-worker/new_module.py").exists(), "files added since are removed")
        changelog = (self.repo / fr.CHANGELOG).read_text()
        self.assertLess(changelog.index("## 0.1.81.3"), changelog.index("## 0.1.81.2"), "history is kept, newest first")
        self.assertIn("exactly 0.1.81.1 again", changelog)
        self.commit("rollback to 0.1.81.1 as 0.1.81.3")
        commit, why = fr.plan_tag("0.1.81.3", self.repo)
        self.assertEqual(commit, run(self.repo, "rev-parse", "HEAD"))
        self.assertIn("same app and integration code as v0.1.81.1", why)

    def test_rollback_tag_refused_if_the_code_was_touched(self) -> None:
        self.make_bad_release()
        fr.rollback("0.1.81.1", None, False, self.repo)
        self.write("codex-cli-worker/server.py", "print('good, mostly')\n")
        self.commit("rollback plus an edit")
        with self.assertRaisesRegex(fr.ReleaseError, "differs from v0.1.81.1 in codex-cli-worker/server.py"):
            fr.plan_tag("0.1.81.3", self.repo)

    def test_rollback_only_to_a_build_that_passed(self) -> None:
        self.make_bad_release()
        with self.assertRaisesRegex(fr.ReleaseError, "v0.1.80 is not tagged"):
            fr.rollback("0.1.80", None, False, self.repo)

    def test_rollback_stops_at_a_stored_data_change(self) -> None:
        self.make_bad_release(notes="- Stored data: conversations move to a new format.")
        with self.assertRaisesRegex(fr.ReleaseError, "0.1.81.2 changed stored data"):
            fr.rollback("0.1.81.1", None, False, self.repo)
        self.assertEqual(fr.rollback("0.1.81.1", None, True, self.repo), "0.1.81.3")

    def test_rollback_needs_a_clean_tree(self) -> None:
        self.make_bad_release()
        self.write("codex-cli-worker/server.py", "print('dirty')\n")
        with self.assertRaisesRegex(fr.ReleaseError, "working tree has changes"):
            fr.rollback("0.1.81.1", None, False, self.repo)


class RepositoryLayoutTests(unittest.TestCase):
    """What the Supervisor needs to offer this repository's app as an update."""

    def test_add_on_repository_and_app_versions(self) -> None:
        repository = (ROOT / "repository.yaml").read_text(encoding="utf-8")
        self.assertIn("url: https://github.com/fatherdoctor/ha_codex", repository)
        config = (ROOT / fr.WORKER_CONFIG).read_text(encoding="utf-8")
        version = fr.CONFIG_VERSION.search(config).group("version")
        self.assertRegex(version, fr.VERSION_PATTERN)
        self.assertIn("image: ghcr.io/fatherdoctor/codex-cli-worker", config, "the Supervisor pulls a built image")
        manifest = json.loads((ROOT / fr.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], version)

    def test_current_version_has_changelog_notes(self) -> None:
        config = (ROOT / fr.WORKER_CONFIG).read_text(encoding="utf-8")
        version = fr.CONFIG_VERSION.search(config).group("version")
        sections = dict(fr.changelog_sections((ROOT / fr.CHANGELOG).read_text(encoding="utf-8")))
        self.assertIn(version, sections)


if __name__ == "__main__":
    unittest.main()
