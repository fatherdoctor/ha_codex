"""Lay out existing concept assets; render the resulting SVG with Inkscape."""
from pathlib import Path
import base64
import html

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs/brand"
OPTIONS = [
    ("a-gate", "A", "Gate", "A clear boundary with a deliberate route."),
    ("b-fold", "B", "Fold", "A simple work surface with one decisive fold."),
    ("c-beacon", "C", "Beacon", "A steady guide with a strong central silhouette."),
    ("d-relay", "D", "Relay", "Two panels exchange a message."),
]


def text(x, y, value, size=18, color="#233447", weight="normal"):
    return (f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
            f'font-weight="{weight}" font-family="DejaVu Sans">'
            f'{html.escape(value)}</text>')


def asset(path, x, y, width, height):
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return (f'<image x="{x}" y="{y}" width="{width}" height="{height}" '
            f'href="data:image/png;base64,{encoded}"/>')


parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1800" '
         'viewBox="0 0 1600 1800">', '<rect width="1600" height="1800" fill="#EBEDE9"/>']
parts += [text(40, 65, "Four directions", 42, weight="bold"),
          text(40, 103, "Original marks. No wordmark. Choose one direction before the full set.", 21)]
for index, (folder, letter, name, description) in enumerate(OPTIONS):
    top = 140 + index * 400
    parts += [text(40, top + 28, f"{letter}  {name}", 24, weight="bold"),
              text(260, top + 28, description, 18)]
    for theme, left, background, foreground in [
        ("", 150, "#FFFFFF", "#233447"),
        ("dark_", 865, "#142534", "#F3F6FA"),
    ]:
        parts.append(f'<rect x="{left}" y="{top + 42}" width="690" height="340" rx="16" fill="{background}"/>')
        parts += [text(left + 30, top + 70, "ICON  256 × 256", 14, foreground),
                  text(left + 340, top + 70, "LOGO  320 × 128", 14, foreground)]
        directory = ROOT / "brand/concepts" / folder
        parts += [asset(directory / f"{theme}icon.png", left + 30, top + 80, 256, 256),
                  asset(directory / f"{theme}logo.png", left + 340, top + 140, 320, 128),
                  asset(directory / f"{theme}icon-32.png", left + 30, top + 342, 32, 32),
                  asset(directory / f"{theme}logo-32.png", left + 340, top + 342, 80, 32),
                  text(left + 74, top + 365, "32 px", 14, foreground),
                  text(left + 432, top + 365, "32 px high", 14, foreground)]
parts += [text(40, 1780, "View at 100% for native 32 px samples. Light and dark use identical geometry.", 18), "</svg>"]
(OUT / "choices.svg").write_text("\n".join(parts) + "\n")
