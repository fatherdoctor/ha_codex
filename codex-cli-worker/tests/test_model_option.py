"""The codex_model option is free text in config.yaml, with no list to keep current."""

import unittest
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


class ModelOptionSchemaTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_codex_model_is_free_text_with_default_value(self):
        self.assertEqual(self.config["schema"]["codex_model"], "str")
        self.assertEqual(self.config["options"]["codex_model"], "default")

    def test_translation_says_how_to_find_model_names(self):
        translations = yaml.safe_load(
            (CONFIG_PATH.parent / "translations" / "en.yaml").read_text(encoding="utf-8"))
        description = translations["configuration"]["codex_model"]["description"]
        self.assertIn("free text", description)
        self.assertIn("model list", description)


if __name__ == "__main__":
    unittest.main()
