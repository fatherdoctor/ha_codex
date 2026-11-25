"""Run a test module with Remote Control unpaused, to check the code that returns when the pause ends.

Remote Control is paused in this version (remote_control.PAUSED). The tests of its process, pairing and routes
import both hooks: `from remote_unpaused import setUpModule, tearDownModule`. test_remote_paused.py checks the pause.
"""

from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remote_control  # noqa: E402

_unpaused = patch.object(remote_control, "PAUSED", False)


def setUpModule() -> None:
    _unpaused.start()


def tearDownModule() -> None:
    _unpaused.stop()
