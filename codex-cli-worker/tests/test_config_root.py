"""Home Assistant's configuration folder has one name in the worker: CONFIG_ROOT, /homeassistant (CX-HA-PATH).

Versions before 0.6 mapped it at /config. A path left at /config fails at run time, so this test lists every
"/config" in the worker's code, page, shell and protection files and fails on one that is not a known non-folder
name (a frontend address, an API path, a message type, a file name).
"""
import re
import sys
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER))

import app_paths  # noqa: E402

# Files outside the check: app_paths.py holds OLD_CONFIG_ROOT, the one place the old name is kept in code.
# builtin_instructions.md is not read either: it tells Codex that /config does not exist, so it names it on purpose.
SKIPPED = {"app_paths.py"}
# "/config" as the folder: at the start of a path, not after a name ("api/config", "lovelace/config", ".git/config",
# "<slug>/config"), and not Home Assistant's frontend address of the app's pages ("/config/app/<slug>/...").
OLD_FOLDER = re.compile(r"(?<![\w}>.\-/])/config(?![\w-])(?!/app/)")
SCRIPTS = ("codex-bwrap", "relay-launcher", "relay-tool-launcher", "apparmor.txt", "Dockerfile", "config.yaml")


def checked_files():
    """Every worker file in which a /config path would reach the container or the owner."""
    files = [path for path in WORKER.glob("*.py")] + [WORKER / name for name in SCRIPTS]
    files += list(WORKER.glob("*.sh")) + list((WORKER / "gate").glob("*.toml"))
    files.append(WORKER / "starter_agents.md")  # A new install's AGENTS.md: Codex follows its paths.
    files += list((WORKER / "translations").glob("*.yaml"))
    files += [path for path in (WORKER / "web").rglob("*") if path.is_file() and "vendor" not in path.parts]
    return sorted(path for path in files if path.name not in SKIPPED and path.is_file())


def stray_config_lines():
    """Return "file:line: text" for each /config that names the old folder."""
    found = []
    for path in checked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if OLD_FOLDER.search(line):
                found.append(f"{path.relative_to(WORKER)}:{number}: {line.strip()}")
    return found


class ConfigRootTests(unittest.TestCase):
    def test_no_worker_file_names_the_old_folder(self):
        self.assertEqual(stray_config_lines(), [])

    def test_the_check_reads_the_files_that_reach_the_container(self):
        names = {path.relative_to(WORKER).as_posix() for path in checked_files()}
        for name in ("server.py", "codex-bwrap", "apparmor.txt", "Dockerfile", "config.yaml", "run.sh",
                     "web/index.html", "web/assets/chat.js", "translations/en.yaml", "gate/requirements.toml",
                     "starter_agents.md"):
            self.assertIn(name, names)

    def test_the_check_finds_a_planted_path_and_keeps_the_frontend_address(self):
        for line in ('cwd="/config"', "ls /config/automations.yaml", "under /share or /config.", "/config/** rix,"):
            self.assertRegex(line, OLD_FOLDER)
        for line in ('f"/config/app/{slug}/config"', '"http://supervisor/core/api/config/core/check_config"',
                     '{"type": "lovelace/config"}', "the task's .git/config", "/configuration.yaml"):
            self.assertNotRegex(line, OLD_FOLDER)

    def test_the_map_puts_home_assistants_folder_at_homeassistant(self):
        text = (WORKER / "config.yaml").read_text()
        block = re.search(r"- type: homeassistant_config\n(?:\s+\w+:.*\n)*", text).group(0)
        self.assertIn("path: /homeassistant\n", block)
        self.assertEqual(app_paths.CONFIG_ROOT, Path("/homeassistant"))

    def test_nothing_in_the_image_makes_a_config_folder_or_link(self):
        for name in ("Dockerfile", "run.sh"):
            for line in (WORKER / name).read_text().splitlines():
                if line.lstrip().startswith("#"):
                    continue
                self.assertNotRegex(line, r"(mkdir|ln)\b.*\s/config\b", name)

    def test_an_old_saved_path_maps_to_the_same_path_under_the_root(self):
        root = Path("/homeassistant")
        self.assertEqual(app_paths.from_old_config_path("/config/codex_tasks", root), "/homeassistant/codex_tasks")
        self.assertEqual(app_paths.from_old_config_path("/config", root), "/homeassistant")
        self.assertEqual(app_paths.from_old_config_path("/configs/x", root), "/configs/x")
        self.assertEqual(app_paths.from_old_config_path("/share/x", root), "/share/x")


if __name__ == "__main__":
    unittest.main()
