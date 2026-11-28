"""Step 0 of the memory-home build: is a writable root inside a `.codex` folder writable with the pinned Codex?

0.6 keeps Codex's memory in `/homeassistant/.codex/memory`. In workspace-write, the pinned Codex makes `.git`,
`.codex` and `.agents` read-only when one of them sits directly in a writable root (kept limit L7 in DOCS.md). The
design adds `/homeassistant/.codex/memory` itself as the last writable root. Whether a root that sits inside another
root's read-only carve-out stays writable depends on how Codex lays the carve-outs over the roots, so this test runs
the claim on the pinned CLI, through `codex sandbox`, which in the image starts the app's own wrapper (codex-bwrap).

The tree is invented and lives under ~/.cache, not /tmp (Codex treats /tmp as writable on its own). Its folders stand
for the app's roots, in the app's order (server.py, TASK_WRITABLE_ROOTS), with the memory root last:

    config  share  media  media/.agents  addon_config  config/.codex/memory

Expected, in the app's order and in the reversed order:
- `config/.codex/memory/x` is written;
- `config/.codex/config.toml` and `config/.codex/y` are refused (Codex's own protection of its settings stays);
- `config/a` is written;
- a write through the S2 link (`media/.agents/codex/memory`, an absolute link to the memory root) and through the
  S1 link (`config/memory`, the relative link `memory -> .codex/memory`) lands in the memory root.

A third run gives a memory root that does not exist and records what Codex does; it is printed, not asserted.

It runs in the Verification checks container job (HA_TEST_REQUIRE_CODEX=1), and by hand with HA_TEST_CODEX
(SANDBOX_TESTING.md). It skips without the pinned CLI, except where HA_TEST_REQUIRE_CODEX=1 makes that a failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live_proofs import CODEX, needs_cli

ROOT_NAMES = ("config", "share", "media", "media/.agents", "addon_config", "config/.codex/memory")

# Each target is tried with one write; the probe prints which ones the sandbox allowed.
TARGETS = {
    "memory file": "config/.codex/memory/x",
    "codex settings": "config/.codex/config.toml",
    "other codex file": "config/.codex/y",
    "config file": "config/a",
    "through the S2 link": "media/.agents/codex/memory/from-s2",
    "through the S1 link": "config/memory/from-s1",
}

PROBE = r'''
import json, sys
result = {}
for label, path in json.loads(sys.argv[1]).items():
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("probe\n")
        result[label] = "written"
    except OSError as error:
        result[label] = "refused: " + (error.strerror or str(error.errno))
print("PROBE " + json.dumps(result))
'''


def make_tree(base: Path, with_memory_root: bool = True) -> None:
    """Build the invented folders, the owner's Codex settings and both links, as 0.6 leaves them after a move."""
    for name in ROOT_NAMES[:-1]:
        (base / name).mkdir(parents=True, exist_ok=True)
    (base / "config/.codex").mkdir()
    (base / "config/.codex/config.toml").write_text("# owner settings\n", encoding="utf-8")
    (base / "media/.agents/codex").mkdir()
    if with_memory_root:
        (base / "config/.codex/memory").mkdir(mode=0o700)
    (base / "media/.agents/codex/memory").symlink_to(base / "config/.codex/memory")
    (base / "config/memory").symlink_to(".codex/memory")


def run_probe(base: Path, roots: list[str]) -> dict[str, str]:
    """Run the probe in Codex's workspace-write sandbox with `roots` as the writable roots, from the tree's base."""
    targets = {label: str(base / path) for label, path in TARGETS.items()}
    result = subprocess.run(
        [CODEX, "sandbox", "--config", 'sandbox_mode="workspace-write"',
         "--config", "check_for_update_on_startup=false",
         "--config", "sandbox_workspace_write.writable_roots=" + json.dumps(roots),
         "--", "python3", "-I", "-c", PROBE, json.dumps(targets)],
        cwd=base / "media/.agents/codex", capture_output=True, text=True, timeout=60,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE ")]
    if not lines:
        return {"(no probe output)": f"exit {result.returncode}: {result.stderr.strip()[-400:]}"}
    return json.loads(lines[-1][len("PROBE "):])


@needs_cli
class NestedMemoryRoot(unittest.TestCase):
    def setUp(self) -> None:
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)

    def roots(self) -> list[str]:
        return [str(self.base / name) for name in ROOT_NAMES]

    def check(self, order: str, roots: list[str]) -> None:
        make_tree(self.base)
        result = run_probe(self.base, roots)
        print(f"\nstep 0, {order}: {json.dumps(result, indent=1)}")
        memory = self.base / "config/.codex/memory"
        self.assertEqual(result.get("memory file"), "written", result)
        self.assertEqual(result.get("config file"), "written", result)
        self.assertTrue(result.get("codex settings", "").startswith("refused"), result)
        self.assertTrue(result.get("other codex file", "").startswith("refused"), result)
        self.assertEqual(result.get("through the S2 link"), "written", result)
        self.assertEqual(result.get("through the S1 link"), "written", result)
        self.assertTrue((memory / "from-s2").is_file() and (memory / "from-s1").is_file())
        self.assertEqual((self.base / "config/.codex/config.toml").read_text(encoding="utf-8"), "# owner settings\n")

    def test_the_memory_root_is_writable_in_the_apps_order(self) -> None:
        self.check("app order", self.roots())

    def test_the_memory_root_is_writable_in_the_reversed_order(self) -> None:
        self.check("reversed order", list(reversed(self.roots())))

    def test_a_missing_memory_root_is_recorded(self) -> None:
        make_tree(self.base, with_memory_root=False)
        result = run_probe(self.base, self.roots())
        # Recorded for the pull request, not a gate: what the pinned Codex does with a root that is not there.
        print(f"\nstep 0, missing memory root: {json.dumps(result, indent=1)}")
        self.assertNotIn("codex settings", {label for label, value in result.items() if value == "written"})


if __name__ == "__main__":
    unittest.main()
