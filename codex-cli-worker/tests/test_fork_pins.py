"""Pins the fatherdoctor fork keeps (CG-6): base image by digest, packaged Codex CLI by version and checksum,
no self-update, and one version for the app and the integration."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "codex-cli-worker"
DOCKERFILE = (WORKER / "Dockerfile").read_text(encoding="utf-8")

CODEX_VERSION = "0.166.0"


def arg(name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE, re.MULTILINE)
    if not match:
        raise AssertionError(f"Dockerfile has no ARG {name}=...")
    return match.group(1)


def yaml_field(text: str, key: str) -> str:
    match = re.search(rf"^{key}:\s*\"?([^\"\n]+)\"?\s*$", text, re.MULTILINE)
    if not match:
        raise AssertionError(f"no {key}: field")
    return match.group(1).strip()


class ForkPinTests(unittest.TestCase):
    def test_base_image_is_pinned_by_digest(self) -> None:
        self.assertRegex(arg("BUILD_FROM"), r"^ghcr\.io/home-assistant/base(:[\w.-]+)?@sha256:[0-9a-f]{64}$")

    def test_codex_cli_is_the_pinned_packaged_build(self) -> None:
        self.assertEqual(arg("CODEX_VERSION"), CODEX_VERSION)
        for name in ("CODEX_SHA256_AMD64", "CODEX_SHA256_AARCH64"):
            self.assertRegex(arg(name), r"^[0-9a-f]{64}$", name)
        self.assertNotEqual(arg("CODEX_SHA256_AMD64"), arg("CODEX_SHA256_AARCH64"))
        self.assertIn("codex-package-${triple}.tar.gz", DOCKERFILE)
        self.assertIn("sha256sum -c -", DOCKERFILE)
        self.assertNotIn("@openai/codex", DOCKERFILE, "the npm build cannot run remote control")

    def test_cli_cannot_replace_itself(self) -> None:
        self.assertIn('chmod -R a-w "/opt/codex/${CODEX_VERSION}"', DOCKERFILE)
        self.assertIn('ln -s "${CODEX_VERSION}" /opt/codex/current', DOCKERFILE, "current must be a relative link")
        server = (WORKER / "server.py").read_text(encoding="utf-8")
        self.assertIn('"check_for_update_on_startup = false",', server)

    def test_packaged_sandbox_goes_through_the_wrapper(self) -> None:
        self.assertIn('ln -sf /opt/codex-sandbox/bwrap "/opt/codex/${CODEX_VERSION}/codex-resources/bwrap"', DOCKERFILE)

    def test_app_and_integration_carry_one_version_and_point_at_the_fork(self) -> None:
        config = (WORKER / "config.yaml").read_text(encoding="utf-8")
        manifest = json.loads((ROOT / "custom_components/codex_cli/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(yaml_field(config, "version"), manifest["version"])
        self.assertEqual(yaml_field(config, "url"), "https://github.com/fatherdoctor/ha_codex")
        self.assertEqual(yaml_field(config, "image"), "ghcr.io/fatherdoctor/codex-cli-worker")
        self.assertEqual(manifest["codeowners"], ["@fatherdoctor"])
        repository = (ROOT / "repository.yaml").read_text(encoding="utf-8")
        self.assertEqual(yaml_field(repository, "maintainer"), "David Allison")


if __name__ == "__main__":
    unittest.main()
