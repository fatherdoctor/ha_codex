#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
export INKSCAPE_PROFILE_DIR="${TMPDIR:-/tmp}/brand-proposal-inkscape"
export XDG_CACHE_HOME="${TMPDIR:-/tmp}/brand-proposal-cache"
for option in brand/concepts/*; do
  for theme in "" dark_; do
    inkscape "$option/${theme}icon.svg" --export-width=256 --export-height=256 --export-filename="$option/${theme}icon.png"
    inkscape "$option/${theme}logo.svg" --export-width=320 --export-height=128 --export-filename="$option/${theme}logo.png"
    magick "$option/${theme}icon.png" -strip "$option/${theme}icon.png"
    magick "$option/${theme}logo.png" -strip "$option/${theme}logo.png"
    magick "$option/${theme}icon.png" -resize 32x32 -strip -define png:color-type=6 "$option/${theme}icon-32.png"
    magick "$option/${theme}logo.png" -resize 80x32 -strip -define png:color-type=6 "$option/${theme}logo-32.png"
  done
done

python3 docs/brand/sources/build_comparison.py
inkscape docs/brand/choices.svg --export-filename=docs/brand/choices.png
magick docs/brand/choices.png -strip docs/brand/choices.png
