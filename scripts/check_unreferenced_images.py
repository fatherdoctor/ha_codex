"""Fail when the repository holds an image that no file uses.

An image earns its place when a tracked text file names it: by its path from the repository root (a README link
or a raw.githubusercontent.com address), or by its path from the folder of the file that names it (an `<img>` in
`web/index.html`, a table in `screenshots/SCREENSHOTS.md`). A picture kept only as a record of a pull request or a
draft names no such file, and this check fails on it, so it leaves the product repository.

Some images are used by a convention, not by a link: the add-on store reads `icon.png` and `logo.png` next to the
app's `config.yaml`, and Home Assistant reads the integration's `brand/` folder. Those and the brand source art in
`brand/` are on ALLOWED below.

Usage (the verification workflow runs it through codex-cli-worker/tests/test_check_unreferenced_images.py):

  check_unreferenced_images.py [--root <repository>]

Exit 0 when every image is used, 1 when one or more is not (each printed on its own line), 2 when git cannot list
the files.
"""

from __future__ import annotations

import argparse
import fnmatch
from pathlib import Path
import posixpath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")
# Images used by a convention instead of a link. Keep each entry narrow and say who reads it.
ALLOWED = (
    "brand/*",  # brand source art, from which the app and integration icons are made
    "codex-cli-worker/icon.png",  # the add-on store reads it next to config.yaml
    "codex-cli-worker/logo.png",  # the add-on store reads it next to config.yaml
    "custom_components/*/brand/*",  # Home Assistant reads a custom integration's brand folder
    "codex-cli-worker/web/assets/vendor/*",  # third-party files, kept as shipped
)


def tracked_files(root: Path) -> list[str]:
    """Every file git tracks under root, as a path from root with forward slashes."""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    )
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def is_allowed(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in ALLOWED)


def names_for(image: str, text_file: str) -> tuple[str, ...]:
    """The spellings by which text_file can name image: from the root, and from text_file's own folder."""
    folder = posixpath.dirname(text_file) or "."
    return (image, posixpath.relpath(image, folder))


def mentions(text: str, name: str) -> bool:
    """True when name appears in text with no file-name character just before it ("x-dots.png" is not "dots.png")."""
    return re.search(r"(?<![\w.-])" + re.escape(name), text) is not None


def read_text_files(root: Path, files: list[str]) -> dict[str, str]:
    """The tracked files that read as UTF-8 text; binary files cannot name an image."""
    texts = {}
    for name in files:
        try:
            texts[name] = (root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return texts


def unreferenced_images(root: Path = ROOT) -> list[str]:
    """The tracked images that no other tracked text file names and ALLOWED does not cover, sorted."""
    files = tracked_files(root)
    texts = read_text_files(root, files)
    images = [
        name
        for name in files
        if name.lower().endswith(IMAGE_SUFFIXES) and not is_allowed(name)
    ]
    unused = []
    for image in images:
        used = any(
            mentions(text, name)
            for text_file, text in texts.items()
            if text_file != image
            for name in names_for(image, text_file)
        )
        if not used:
            unused.append(image)
    return sorted(unused)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="the repository to check (default: this one)",
    )
    args = parser.parse_args(argv)
    try:
        unused = unreferenced_images(args.root)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"cannot list the files: {error}", file=sys.stderr)
        return 2
    for image in unused:
        print(f"{image}: no file uses this image; link it from a document or remove it")
    if unused:
        return 1
    print("ok: every image is used")
    return 0


if __name__ == "__main__":
    sys.exit(main())
