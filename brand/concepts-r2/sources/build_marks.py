"""Write original flat SVG masters. Run from any directory; no third-party paths."""
from pathlib import Path

OUT = Path(__file__).resolve().parents[1]
COLORS = {"": ("#233447", "#137A72"), "dark_": ("#F3F6FA", "#58CFC3")}


def path(d, color, width=28, mask=""):
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round" {mask}/>')


def cut(name, d, width=42):
    return (f'<mask id="{name}" maskUnits="userSpaceOnUse" x="-20" y="-20" width="360" height="300">'
            '<rect x="-20" y="-20" width="360" height="300" fill="white"/>'
            + path(d, "black", width) + '</mask>')


def geometry(name, ink, accent):
    if name == "a-roofweave":
        # The two localized cuts alternate over/under without a painted backdrop.
        defs = cut("under-roof", "M154 78V132") + cut("under-wall", "M102 174V214")
        body = path("M102 148L166 94Q174 88 182 94L234 138V216Q234 238 212 238H124Q102 238 102 216Z", accent,
                    mask='mask="url(#under-roof)"')
        body += path("M22 98L82 46Q90 39 98 46L154 94V178Q154 200 132 200H44Q22 200 22 178Z", ink,
                     mask='mask="url(#under-wall)"')
    elif name == "b-courtyard":
        defs = ""
        body = path("M232 26H62Q24 26 24 64V192Q24 232 64 232H174Q216 232 216 190V112L148 58L80 112V160Q80 178 98 178H152", ink)
        body += '<rect x="132" y="113" width="28" height="28" rx="5" fill="'+accent+'"/>'
    elif name == "c-cloudloom":
        defs = cut("under-left", "M144 114L170 140", 40)
        body = path("M82 72Q100 30 137 34Q179 36 190 88Q234 88 236 130Q238 170 200 170H164V136L128 108", accent,
                    mask='mask="url(#under-left)"')
        body += path("M102 170H66Q20 170 20 126Q20 83 60 82Q86 80 108 94", ink)
        body += path("M90 218V150L132 118L174 150V218", ink)
    else:
        defs = cut("under-orbit", "M101 148L130 125", 38)
        body = path("M96 150H64Q22 150 22 108Q22 66 64 66H158Q200 66 200 108Q200 130 184 143", ink,
                    mask='mask="url(#under-orbit)"')
        body += path("M214 112Q240 122 240 153Q240 194 198 194H104Q62 194 62 160Q62 146 78 134L136 91L180 121", accent)
        body += '<rect x="125" y="140" width="22" height="22" rx="4" fill="'+ink+'"/>'
    return defs, body


def landscape(name, ink, accent):
    """Redraw wide compositions, rather than squash the square marks."""
    if name == "a-roofweave":
        defs = cut("under-roof", "M178 42V74", 26) + cut("under-wall", "M134 89V115", 26)
        body = path("M134 74L216 34Q222 31 228 34L310 74V104Q310 120 294 120H150Q134 120 134 104Z", accent, 16,
                    'mask="url(#under-roof)"')
        body += path("M10 50L88 12Q94 9 100 12L178 50V90Q178 106 162 106H26Q10 106 10 90Z", ink, 16,
                     'mask="url(#under-wall)"')
    elif name == "b-courtyard":
        defs = ""
        body = path("M310 10H38Q10 10 10 38V90Q10 118 38 118H248Q278 118 278 90V54L228 20L178 54V82Q178 92 188 92H232", ink, 16)
        body += '<rect x="217" y="54" width="22" height="22" rx="4" fill="'+accent+'"/>'
    elif name == "c-cloudloom":
        defs = cut("under-left", "M168 55L187 69", 25)
        body = path("M114 40Q130 8 174 10Q221 12 230 50Q310 50 310 82Q310 112 264 112H200V78L164 52", accent, 16,
                    'mask="url(#under-left)"')
        body += path("M112 112H64Q10 112 10 80Q10 48 62 48Q96 48 122 59", ink, 16)
        body += path("M112 120V78L156 48L200 78V120", ink, 16)
    else:
        defs = cut("under-orbit", "M108 78L130 63", 26)
        body = path("M106 78H46Q10 78 10 44Q10 10 46 10H224Q260 10 260 44Q260 62 246 73", ink, 16,
                    'mask="url(#under-orbit)"')
        body += path("M284 50Q310 58 310 84Q310 118 274 118H98Q62 118 62 90Q62 78 78 67L160 26L238 63", accent, 16)
        body += '<rect x="149" y="74" width="22" height="22" rx="4" fill="'+ink+'"/>'
    return defs, body


for name in ("a-roofweave", "b-courtyard", "c-cloudloom", "d-orbit"):
    folder = OUT / name
    folder.mkdir(exist_ok=True)
    for theme, colors in COLORS.items():
        for kind, width, height, draw in (
            ("icon", 256, 256, geometry),
            ("logo", 320, 128, landscape),
        ):
            defs, body = draw(name, *colors)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                   f'viewBox="0 0 {width} {height}"><defs>{defs}</defs><g>{body}</g></svg>\n')
            (folder / f"{theme}{kind}.svg").write_text(svg)
