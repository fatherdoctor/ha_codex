"""scripts/bundle_integration.py: the check refuses a stale bundled integration even when the version is the same."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "bundle_integration", ROOT / "scripts" / "bundle_integration.py"
)
bi = importlib.util.module_from_spec(SPEC)
sys.modules["bundle_integration"] = bi
SPEC.loader.exec_module(bi)

MANIFEST = '{"domain": "codex_cli", "version": "9.9.9"}'


class BundleIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (bi.ROOT, bi.SOURCE, bi.BUNDLE)
        bi.ROOT = base
        bi.SOURCE = base / "custom_components" / "codex_cli"
        bi.BUNDLE = base / "codex-cli-worker" / "integration" / "codex_cli"
        self.write(
            bi.SOURCE,
            {
                "manifest.json": MANIFEST,
                "__init__.py": "NEW = 1\n",
                "core_logs.py": "x = 1\n",
            },
        )

    def tearDown(self) -> None:
        bi.ROOT, bi.SOURCE, bi.BUNDLE = self.saved
        self.tmp.cleanup()

    @staticmethod
    def write(folder: Path, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = bi.main(list(args))
        return code, out.getvalue()

    def test_check_refuses_a_stale_copy_with_the_same_version(self) -> None:
        # The case found in a drop: same manifest version, but files removed, added and changed since.
        self.write(
            bi.BUNDLE,
            {
                "manifest.json": MANIFEST,
                "__init__.py": "OLD = 1\n",
                "kiosk.py": "y = 1\n",
            },
        )
        code, out = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("missing: core_logs.py", out)
        self.assertIn("extra: kiosk.py", out)
        self.assertIn("changed: __init__.py", out)
        self.assertTrue(
            (bi.BUNDLE / "kiosk.py").exists(), "--check must change nothing"
        )

    def test_check_refuses_a_missing_copy(self) -> None:
        code, out = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("no bundled copy", out)

    def test_copy_replaces_the_old_copy_entirely(self) -> None:
        self.write(bi.BUNDLE, {"manifest.json": MANIFEST, "kiosk.py": "y = 1\n"})
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertFalse((bi.BUNDLE / "kiosk.py").exists())
        self.assertEqual((bi.BUNDLE / "core_logs.py").read_text(), "x = 1\n")
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_python_caches_are_neither_copied_nor_compared(self) -> None:
        self.write(bi.SOURCE, {"__pycache__/a.cpython-314.pyc": "cache"})
        self.assertEqual(self.run_main()[0], 0)
        self.assertFalse((bi.BUNDLE / "__pycache__").exists())
        self.write(bi.BUNDLE, {"__pycache__/b.cpython-313.pyc": "other cache"})
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_no_source_cannot_run(self) -> None:
        (bi.SOURCE / "manifest.json").unlink()
        self.assertEqual(self.run_main("--check")[0], 2)


if __name__ == "__main__":
    unittest.main()
