#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
export INKSCAPE_PROFILE_DIR="${TMPDIR:-/tmp}/brand-round2-inkscape"
export XDG_CACHE_HOME="${TMPDIR:-/tmp}/brand-round2-cache"
python3 brand/concepts-r2/sources/build_marks.py
for option in brand/concepts-r2/[a-d]-*; do
  for theme in "" dark_; do
    for kind in icon logo; do
      inkscape "$option/${theme}${kind}.svg" --export-filename="$option/${theme}${kind}.png"
      magick "$option/${theme}${kind}.png" -strip -define png:color-type=6 "$option/${theme}${kind}.png"
      magick "$option/${theme}${kind}.png" -resize x32 -strip -define png:color-type=6 "$option/${theme}${kind}-32.png"
    done
  done
done
python3 brand/concepts-r2/sources/build_comparison.py
inkscape brand/concepts-r2/choices.svg --export-filename=brand/concepts-r2/choices.png
magick brand/concepts-r2/choices.png -strip brand/concepts-r2/choices.png
