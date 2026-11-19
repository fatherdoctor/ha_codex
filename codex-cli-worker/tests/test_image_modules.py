"""The image holds every worker module that the modules it copies to / import.

The worker runs from the image's root folder, so a module that the source tree has and the
Dockerfile does not copy is found by the tests and missing at startup.
"""

import ast
from pathlib import Path
import unittest

WORKER = Path(__file__).resolve().parents[1]


def root_copies(dockerfile):
    """Return the file names that the Dockerfile's COPY lines put in the image's root folder."""
    names = set()
    for line in dockerfile.splitlines():
        words = line.split()
        if len(words) >= 3 and words[0] == "COPY" and words[-1] == "/":
            names.update(words[1:-1])
    return names


def local_imports(path):
    """Return the names of the worker's own top-level modules that the file at path imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return {name for name in names if (WORKER / f"{name}.py").is_file()}


class ImageModulesTests(unittest.TestCase):
    def test_every_imported_worker_module_is_copied_to_the_root(self):
        copied = root_copies((WORKER / "Dockerfile").read_text(encoding="utf-8"))
        self.assertIn("server.py", copied)
        missing = {}
        for name in sorted(item for item in copied if item.endswith(".py")):
            needed = {
                f"{module}.py" for module in local_imports(WORKER / name)
            } - copied
            if needed:
                missing[name] = sorted(needed)
        self.assertEqual(
            missing, {}, "modules imported in the image but not copied to /"
        )

    def test_the_call_tool_takes_its_name_matcher_into_the_image(self):
        self.assertIn("name_match", local_imports(WORKER / "ha_call.py"))
        self.assertIn(
            "name_match.py",
            root_copies((WORKER / "Dockerfile").read_text(encoding="utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
