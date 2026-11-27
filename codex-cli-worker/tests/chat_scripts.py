"""The chat page's own scripts in load order, for the tests that read the page as text.

The page script is cut into several files (CX-RF-JS); a test that looks for a line, or for its absence,
reads all of them joined, so a line that moves between them does not change what the test proves.
"""
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
CHAT_SCRIPTS = ("chat-core.js", "chat-settings.js", "chat.js")


def chat_source() -> str:
    """Return the text of every chat script, joined in load order."""
    return "\n".join((ASSETS / name).read_text(encoding="utf-8") for name in CHAT_SCRIPTS)
