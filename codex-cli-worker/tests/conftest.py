"""Shared pytest setup for the worker tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_folders import changes, snapshot  # noqa: E402


@pytest.fixture(autouse=True)
def no_writes_under_the_real_app_folders(request):
    """Fail a test that creates, changes or deletes anything under /data, /homeassistant or /addon_config.

    A test that needs those folders uses app_folders.isolated_app_folders() with a temporary folder.
    """
    before = snapshot()
    yield
    changed = changes(before, snapshot())
    if changed:
        pytest.fail(f"{request.node.nodeid} wrote under the real {', '.join(changed)}; "
                    "use app_folders.isolated_app_folders()", pytrace=False)
