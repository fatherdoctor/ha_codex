"""Five page styles that nothing used are removed (0.4.0), and none comes back without a page element.

The classes eyebrow, footnote, suggestions and welcome-icon (chat.css) and text-button (studio.css) were kept from
the first design of the welcome screen and the side panel. No page, script or test built an element with them.
Each test here fails on version 0.3, where the styles were still in the stylesheets.
"""
from pathlib import Path
import re
import unittest

WEB = Path(__file__).resolve().parents[1] / "web"
REMOVED_CLASSES = ("eyebrow", "footnote", "suggestions", "welcome-icon", "text-button")


def page_files():
    """Return the page, its scripts and its stylesheets, without the bundled third-party code."""
    files = [WEB / "index.html"]
    for path in sorted((WEB / "assets").glob("*")):
        if path.suffix in (".css", ".js"):
            files.append(path)
    return files


class UnusedStylesTest(unittest.TestCase):
    def test_no_stylesheet_has_a_rule_for_a_removed_class(self):
        for name in ("chat.css", "studio.css"):
            text = (WEB / "assets" / name).read_text(encoding="utf-8")
            for removed in REMOVED_CLASSES:
                with self.subTest(stylesheet=name, css_class=removed):
                    self.assertIsNone(re.search(r"\." + re.escape(removed) + r"(?![\w-])", text))

    def test_no_page_file_names_a_removed_class(self):
        for path in page_files():
            text = path.read_text(encoding="utf-8")
            for removed in REMOVED_CLASSES:
                with self.subTest(file=path.name, css_class=removed):
                    self.assertIsNone(re.search(r"(?<![\w-])" + re.escape(removed) + r"(?![\w-])", text))

    def test_settings_button_rule_keeps_its_other_exceptions(self):
        # The settings button rule named text-button among the buttons it leaves alone; the others stay.
        text = (WEB / "assets" / "chat.css").read_text(encoding="utf-8")
        self.assertIn(":is(#settings, .studio-sheet) button:not(.icon, .subtle, .settings-toggle, .permission-option) {", text)


if __name__ == "__main__":
    unittest.main()
