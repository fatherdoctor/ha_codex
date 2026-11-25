"""Where Home Assistant's configuration folder is inside the app's container.

config.yaml maps homeassistant_config at /homeassistant, the Supervisor's default and the path the Claude add-on
uses, so a path copied from one assistant works in the other. Every module reads the folder from CONFIG_ROOT;
tests/test_config_root.py fails on any other spelling of it in the worker's code.
"""

from pathlib import Path

CONFIG_ROOT = Path("/homeassistant")

# Versions before 0.6 mapped the folder at /config. Nothing in the container is there now. The name is kept only to
# read text from those versions: a saved task_root option, the owner's own instructions and schedules (the one-time
# scan in server.py), and the page instructions' sentence that maps an old path for Codex. Home Assistant itself
# still calls its folder /config inside its own container, so /config/ in its YAML is correct and stays.
OLD_CONFIG_ROOT = "/config"


def from_old_config_path(value: str, root: Path) -> str:
    """Return a path saved by a version before 0.6 as the same path under root (CONFIG_ROOT); others as they are."""
    if value == OLD_CONFIG_ROOT or value.startswith(OLD_CONFIG_ROOT + "/"):
        return root.as_posix() + value[len(OLD_CONFIG_ROOT) :]
    return value
