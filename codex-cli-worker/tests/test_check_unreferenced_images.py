"""scripts/check_unreferenced_images.py: every image in the repository is used by a file or by a convention."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_unreferenced_images", ROOT / "scripts" / "check_unreferenced_images.py"
)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class UnreferencedImageTests(unittest.TestCase):
    """A small repository that each test fills with images and the files that name them."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, name: str, content: str | bytes = PNG) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", name], cwd=self.repo, check=True)

    def unused(self) -> list[str]:
        return check.unreferenced_images(self.repo)

    def test_an_image_no_file_names_is_reported(self) -> None:
        self.add("screenshots/old-proposal.png")
        self.add("README.md", "No pictures here.\n")
        self.assertEqual(self.unused(), ["screenshots/old-proposal.png"])

    def test_a_path_from_the_root_counts(self) -> None:
        self.add("screenshots/chat.png")
        self.add("codex-cli-worker/README.md", "![chat](https://example.invalid/main/screenshots/chat.png)\n")
        self.assertEqual(self.unused(), [])

    def test_a_path_from_the_naming_files_folder_counts(self) -> None:
        self.add("web/assets/brand/icon.svg", "<svg/>")
        self.add("web/index.html", '<img src="assets/brand/icon.svg">\n')
        self.add("screenshots/chat.png")
        self.add("screenshots/SCREENSHOTS.md", "![chat](chat.png)\n")
        self.assertEqual(self.unused(), [])

    def test_the_same_file_name_in_another_folder_does_not_count(self) -> None:
        # A test that writes /tmp/dots-phone.png does not use screenshots/review/dots-phone.png.
        self.add("screenshots/review/dots-phone.png")
        self.add("tests/browser_dots.cjs", "page.screenshot({path: '/tmp/dots-phone.png'});\n")
        self.add("tests/notes.md", "See icon-dots-phone.png and review/dots-phone.png.\n")
        self.assertEqual(self.unused(), ["screenshots/review/dots-phone.png"])

    def test_an_svg_does_not_use_itself(self) -> None:
        self.add("docs/diagram.svg", "<svg><title>docs/diagram.svg</title></svg>")
        self.assertEqual(self.unused(), ["docs/diagram.svg"])

    def test_images_used_by_a_convention_are_allowed(self) -> None:
        for name in (
            "brand/courtyard/dark_logo.svg",
            "codex-cli-worker/icon.png",
            "codex-cli-worker/logo.png",
            "custom_components/codex_cli/brand/icon.png",
        ):
            self.add(name)
        self.assertEqual(self.unused(), [])

    def test_an_icon_elsewhere_is_not_allowed_by_its_name(self) -> None:
        self.add("docs/icon.png")
        self.assertEqual(self.unused(), ["docs/icon.png"])

    def test_an_untracked_image_is_not_checked(self) -> None:
        (self.repo / "scratch.png").write_bytes(PNG)
        self.assertEqual(self.unused(), [])

    def test_exit_codes_and_output(self) -> None:
        self.add("old.png")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(check.main(["--root", str(self.repo)]), 1)
        self.assertIn("old.png: no file uses this image", out.getvalue())
        self.add("README.md", "![old](old.png)\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(["--root", str(self.repo)]), 0)

    def test_a_folder_that_is_not_a_repository_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as empty, redirect_stderr(io.StringIO()):
            self.assertEqual(check.main(["--root", empty]), 2)


class ThisRepositoryTests(unittest.TestCase):
    """The check itself: the verification workflow runs this test, so an unused image fails the pull request."""

    def test_every_image_in_this_repository_is_used(self) -> None:
        self.assertEqual(check.unreferenced_images(ROOT), [])


if __name__ == "__main__":
    unittest.main()
