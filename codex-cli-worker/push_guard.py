#!/usr/bin/env python3
"""push_guard.py - refuse a push that carries a secret or a forbidden path. Exit 0 = clean, 1 = refused, 2 = error.

Status: template (O4, 2026-10-14; names list KIT-GRD, 2026-10-23; more shapes, photo location and the shrink check
KIT-SAFE, 2026-11-01; token values, fewer false refusals and long lines KIT-SAFE.f, 2026-11-02; YAML blocks, binary
and UTF-16 files, escapes, photos by their bytes and names in paths KIT-XR-GUARD.f, 2026-11-11). Goes to guard/push_guard.py; installed as a
pre-push hook (see templates/guard/README.md). Standard library only.

What it reads. As the pre-push hook (`--pushed-refs`, the hook in templates/guard/README.md), the lines git gives
the hook on standard input: one per ref the push sends (a branch that is not checked out, a tag, every ref of
`--all` or `--tags`). A ref that is not a commit (a tag of a blob or a tree) is refused. For each other ref, the
commits from what the remote has of that ref up to the sent commit; for a new ref, the commits the remote has on no
branch (pushed_ranges). A ref being deleted sends nothing. The by-hand route (`--push REMOTE REF...`, for a computer
that cannot run the hook) builds the same lines itself from the named refs, checks them the same way, and pushes
the checked commits by their ids only when the check is clean (hook_lines), with push.followTags and
push.recurseSubmodules turned off so nothing unread is added. Without either (a check, not a push) it
reads the checked-out branch only, as below. The message of each commit in a range, and of each annotated tag a
push sends, is scanned like a file's lines (message_lines), up to a signed tag's closing signature block; the author
and committer names are not. A chain of more than MAX_TAG_DEPTH tags is refused (tag_objects). A bare carriage return or another line separator inside a line does not hide the
rest of it: each piece is judged as its own line too, and a YAML block value after one is still read with its key.
The diff is read with fixed a/ and b/ prefixes, so a diff preference (diff.noprefix) cannot hide the paths.
A character that prints as nothing (a byte-order mark, a zero-width space) does not hide a key: each line is also
judged without them. A YAML block value is judged with its key when either line is new, so a changed key over an old
value is read, and a tag or an anchor between the key and the block indicator (`! |`, `&a !!str >-`) does not hide
it.

Scans the ADDED lines of EVERY commit in that range, each against its parent (by hand: the upstream branch up
to HEAD; with no upstream, the commits no remote has yet; with no remote-tracking branch at all, a true first push,
every tracked file and every commit in its history), and the paths they touch. Given the remote being pushed to
(`--remote NAME`; the pre-push hook passes git's first argument), only that remote counts: the upstream when it lives
on that remote, else the commits that remote does not have, so a commit only another remote holds is still scanned: a secret added in one commit and
removed in the next is still in the pushed history. A merge commit adds only the lines new against every parent, so
a pull that merges does not charge the pusher with lines already upstream. A photo is read as the commit that
touched it left it. It scans for:
  - forbidden paths: secrets.yaml, .storage/, *.key, *.pem, id_rsa/id_ed25519 (the private half), backup archives,
    the backup emergency kit, the recorder database (home-assistant_v2.db), git and agent credential files
    (.git-credentials, .credentials.json, .claudecode/);
  - credential shapes: a JWT (Home Assistant long-lived tokens are JWTs), "Bearer <long value>", a private key
    block, a line that starts with a key such as password, token, api_key, pin or code (also inside a longer key,
    e.g. wifi_password) set to a literal value (not a call, an index, a YAML tag or a GPIO pin; a bare code, pin or
    token key only with a quoted value or four to eight digits); a password, token or API key anywhere on the line
    (JSON, a YAML flow map, a URL's user:password@, "the password is <value with a digit>"), and a 32-hex value
    under a key naming a token, key, secret or auth; a Basic authorization header; a door code or PIN written as
    digits after the word ("code" or "PIN" followed by four to eight digits); a `!secret` reference is fine, and
    prose mentioning the word is not a value;
  - API keys: GitHub tokens (ghp_ and the other gh?_ kinds, github_pat_), sk- keys (Anthropic and others), AWS
    access key ids;
  - a long hex string (40 or more: a token or a hash), except a full commit id in a GitHub commit/blob/tree link,
    a GitHub Actions `uses: owner/repo@<commit>` pin, a commit id under a key naming a commit or ref and a SHA-256
    digest under a key naming a digest;
  - phone-number shapes (North American with separators, or ten bare digits after a word such as "call" or
    "phone"; +international with or without);
  - a photo that carries its location: a JPEG, PNG, TIFF, HEIC or WebP whose EXIF block holds GPS coordinates
    (phones write them by default), known by its bytes whatever the file is called; strip the metadata first;
  - a file git calls binary (a UTF-16 file, a `-diff` attribute) is read from its bytes and its new lines scanned
    like any other; one that is not text and not a photo, font, GIF, icon or Python bytecode is refused as
    unreadable until its path is on guard/binary.txt (`--binary PATH`; same format as shrink.txt);
  - the household's own names: every entry in guard/names.txt (people, street, town; one per line; a line starting
    with `#`, or a ` #` after whitespace, starts a comment), matched as whole words, case-insensitive. The file
    lives next to this script, or `--names PATH`; with no file the check is off and the guard says so. The names
    file itself (the one `--names` points at) is never scanned; keep it out of git (README, "Household names").
    A listed word in a file's path is refused too.
A YAML block scalar under a secret key (`wifi_password: |-` and the value on the next line) is judged as one line,
and a line with \\u, \\U or \\x escapes is scanned as written and again with the escapes turned into characters
(json.dumps writes an accented name that way).
A file on the shrink list (guard/shrink.txt next to this script, or `--shrink PATH`; one repository path per line,
`#` comments as in names.txt; no file = off) may not end the push under half its line count at the start when it had
20 lines or more, nor be deleted: the trace of a truncating write that wiped a shared list (principles/
change-control.md rule 11 is the same idea for live files). A deliberate cut is pushed with the list entry
commented out for that one push, and the CHANGELOG says why.
A line that must stay can carry `guard: allow` with a reason in the same line (a bare marker counts for nothing: the
reason holds a word of three letters or more); the CHANGELOG says why.
A line over 4,000 characters is scanned in overlapping pieces, so no shape can stall the hook on a minified file.

The guard's verdict is its EXIT STATUS. Never chain it with something that ignores the status (a grep, `; git push`):
principles/working-with-the-agent.md rule 9.
"""
import argparse
import os
import re
import subprocess
import sys

FORBIDDEN_PATHS = [
    re.compile(p, re.I) for p in (
        r"(^|/)secrets\.yaml$", r"(^|/)\.storage/", r"\.(key|pem|p12|pfx)$", r"(^|/)id_(rsa|ed25519|ecdsa)$",
        r"\.tar(\.gz)?$", r"emergency[_ -]?kit", r"(^|/)\.env$",
        r"\.db(-wal|-shm|-journal)?$", r"(^|/)\.git-credentials", r"(^|/)\.credentials\.json$",
        r"(^|/)\.claudecode/",
    )
]
# A value that is not a value: a secret reference or any other YAML tag (`!secret`, `!lambda`, `!include`), a variable,
# a placeholder, an empty word.
_NOT_A_VALUE = r"(?!![A-Za-z])(?!\$\{?)(?!null\b|none\b|true\b|false\b|<)"
_VAL = r"[A-Za-z0-9!@#$%^&*._/+-]"  # the characters of an unquoted literal value


def _literal(name, cls=_VAL, least=4):
    """An unquoted literal of `cls` characters, taken whole (the lookahead and back-reference make it atomic), that
    is not the start of an expression: not followed by `(` or `[` (a call, an index: `os.environ["X"]`,
    `m.group(1)`). A GPIO pin name is not a value (ESPHome's `interrupt_pin: GPIO4`)."""
    return (_NOT_A_VALUE + r"(?!GPIO\d)(?=(?P<" + name + r">" + cls + r"{" + str(least) + r",}))(?P=" + name + r")"
            r"(?![(\[])")


_HAS_DIGIT = r"(?=[^\s'\"{},()\[\]]{0,200}\d)"
_TOKEN_CHARS = r"[A-Za-z0-9_~+/=-]"
SHAPES = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # A line that starts with a key set to a literal. The password kinds (also inside a longer key, wifi_password),
    # and token/pin/code inside a longer key (porch_api_token, alarm_pin): YAML `key: value`, or `KEY=value` /
    # `key = value` with a digit in it (code assigns names: `has_pin = PIN_SUBMIT`), or any quoted value.
    ("literal secret", re.compile(
        r"^\s*(?:-\s*|export\s+)?['\"]?(?:[\w-]*(?:password|passwd|secret|api_key|apikey)|[\w-]+(?:token|pin|code))"
        r"['\"]?\s*(?::\s*" + _literal("a") + r"|=\s*" + _HAS_DIGIT + _literal("b") + r"|[:=]\s*['\"]"
        + _NOT_A_VALUE + _VAL + r"{4,})", re.I)),
    # A bare code, pin or token key (KIT-SAFE.f F3): only a quoted value or four to eight digits is a literal;
    # `code = m.group(1)`, `"code": code`, `pin: GPIO18` and `Code: revert the PR` are not.
    ("literal secret", re.compile(
        r"^\s*(?:-\s*|export\s+)?['\"]?(?:code|pin|token)['\"]?\s*[:=]\s*(?:['\"]" + _NOT_A_VALUE + _VAL
        + r"{4,}|\d{4,8}(?![\w.]))", re.I)),
    # The password kinds anywhere on the line (KIT-SAFE, from the 2026-10-31 sweep: JSON, flow maps, URLs and prose
    # all went through): a quoted value, or an unquoted one with a digit in it (`the NUT password: they stay` and
    # `"password": password}` are words, not values). One finding per kind per line. A longer key counts
    # (`{"wifi_password": "..."}`, `mqtt_password: ...` mid-line; KIT-SAFE.fc, KIT-SAFE.f2).
    ("literal secret", re.compile(
        r"\b[\w-]{0,40}?(?:password|passwd|pwd)['\"]?\s*[:=]\s*(?:['\"]" + _NOT_A_VALUE + r"[^\s'\"{},]{4,}|" + _HAS_DIGIT
        + _literal("c", r"[^\s'\"{},()\[\]]") + r")", re.I)),
    # Token and key kinds anywhere on the line (KIT-SAFE.f F2: `{"token": "..."}`, `{"api_key": "..."}` mid-line,
    # `auth_token: <32 hex>`): a quoted value of 8 or more with a digit, or an unquoted one of 16 or more.
    ("literal secret", re.compile(
        r"\b(?:token|api_key|apikey|secret|auth_token|access_token|refresh_token|client_secret)['\"]?\s*[:=]\s*"
        r"(?:['\"]" + _NOT_A_VALUE + r"(?=[^\s'\"]{0,200}\d)[A-Za-z0-9._~+/=-]{8,}['\"]|" + _HAS_DIGIT
        + _literal("d", _TOKEN_CHARS, 16) + r"(?!\.))", re.I)),
    # A 32-hex value (an auth token's usual shape, under the long-hex floor) under a key that names it.
    ("literal secret", re.compile(
        r"(?:token|key|secret|auth)[\w-]{0,40}['\"]?\s*[:=]\s*['\"]?[0-9a-fA-F]{32,}(?![0-9A-Za-z])",
        re.I)),
    # A URL's user:password@; the scheme is bounded so a long run of letters cannot make it backtrack (F1).
    ("literal secret", re.compile(
        r"\b[a-z][a-z0-9+.-]{0,30}://[^\s/:@]{1,200}:" + _NOT_A_VALUE + r"[^\s/@]{3,200}@", re.I)),
    ("literal secret", re.compile(r"\b(?:password|passcode|passphrase)\s+(?:is|was)\s*:?\s*['\"`]?(?=\S*\d)\S{4,}",
                                  re.I)),
    ("literal secret", re.compile(
        r"\b(?:door\s+|gate\s+|garage\s+|alarm\s+|lock\s+|entry\s+)?(?:code|pin|passcode)\b(?:\s+(?:is|was))?"
        r"\s*[:=]?\s*['\"`]?\d{4,8}(?![-:/\d]|\.\d)", re.I)),
    ("api key", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("api key", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}")),
    # The digit lookahead is bounded so a long run of `sk-sk-sk-` stays linear (F1).
    ("api key", re.compile(r"\bsk-(?=[A-Za-z0-9_-]{0,80}\d)[A-Za-z0-9_-]{20,}")),
    ("api key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),  # an AWS access key id
    ("literal secret", re.compile(r"\bAuthorization:\s*Basic\s+[A-Za-z0-9+/]{12,}={0,2}", re.I)),
    ("long hex", re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{40,}(?![0-9A-Za-z])")),
    ("phone", re.compile(r"(?<![\w.])(\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])")),
    ("phone", re.compile(r"(?<![\w+.-])\+[1-9]\d{9,14}(?![\w.])")),
    # Ten bare digits only after a phone word (F5): alone they are as often a datestamp, a model or a firmware number.
    ("phone", re.compile(r"\b(?:call|phone|tel|text|cell|mobile|sms|fax|number)\b[^\w\n]{0,4}(?<![\w.+-])"
                         r"[2-9]\d{2}[2-9]\d{6}(?![\w.-])", re.I)),
]
# Masked before the shapes are tried (the rest of the line is still scanned): a full commit id in a GitHub link, an
# Actions step pinned to a commit (the safe way to use an action; a tag can be moved), a 40-hex value under a key
# that names a commit or ref (`"repo_commit": ...`, `"live_export": ...`), and a 64-hex value under a key that names
# a digest (`"sha256": ...`): a data file of checksums cannot carry an allow mark per line (F3).
# The key that may carry a masked value (KIT-SAFE.fc R1): the key word stands alone in it (`repo_commit`, `live_export`,
# `commit_sha`, `sha256`; not `refresh_`, `shared_`, `hashed_`) and nothing in the key names a secret (`export_token`,
# `api_key_ref`, `commit_token` are not masked). Bounded, so a long run of word characters stays linear.
_HEX_KEY = r"(?<![\w-])(?![\w-]{0,40}?(?:token|secret|key|pass|pwd|auth|cred))[\w-]{0,40}?(?<![a-z])"
HEX_EXCEPTIONS = [
    re.compile(r"(/(?:commit|commits|blob|tree)/)[0-9a-f]{40}(?![0-9a-fA-F])"),
    re.compile(r"(\buses:\s*[\w.-]+/[\w./-]+@)[0-9a-f]{40}(?![0-9a-fA-F])"),
    re.compile(r"(" + _HEX_KEY + r"(?:commit|export|ref|sha)(?![a-z])[\w-]{0,40}['\"]?\s*[:=]\s*['\"]?)"
               r"[0-9a-f]{40}(?![0-9a-fA-F])", re.I),
    re.compile(r"(" + _HEX_KEY + r"(?:sha256|checksum|digest|hash)(?![a-z])[\w-]{0,40}['\"]?\s*[:=]\s*['\"]?"
               r"(?:sha256:)?)[0-9a-f]{64}(?![0-9a-fA-F])", re.I),
]
# A longer line (a minified file, a base64 blob, a data URL) is scanned in pieces of this many characters that
# overlap by OVERLAP, so no shape's work grows with the square of the line (F1) and nothing past the cut is skipped.
LONG_LINE = 4000
OVERLAP = 300
IMAGE_EXT = re.compile(r"\.(jpe?g|png|tiff?|heic|heif|webp)$", re.I)
SHRINK_FILE = "shrink.txt"
BINARY_FILE = "binary.txt"
# A path item whose third slot is BINARY is a file git calls binary that the guard could not read as text (KIT-XR-GUARD
# F2): its bytes are looked at in scan (a photo by its bytes, F4; anything else of an unknown kind is refused).
BINARY = type("Binary", (), {"__repr__": lambda self: "BINARY"})()
# Containers the guard knows by their first bytes (F4: a photo renamed photo.dat is still a photo).
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"II*\x00", b"MM\x00*")
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif")
# Binary kinds that hold no text to leak (fonts, GIF, icons): passed without a finding.
_SAFE_MAGIC = (b"wOFF", b"wOF2", b"\x00\x01\x00\x00", b"OTTO", b"GIF87a", b"GIF89a", b"\x00\x00\x01\x00")
# A YAML key that names a secret, set to a block scalar (`wifi_password: |-`); its value is on the next lines (F1).
# YAML allows node properties between the colon and the block indicator: a tag (`!`, `!!str`, `!<tag:...>`, `!foo`)
# and an anchor (`&a`), in either order (KIT-PUBr.f.pr2 F3). A named local tag (`!secret`, `!vault`) is kept with
# the value, so the line shapes still treat it as a reference; a non-specific or standard tag and an anchor are not.
_BLOCK_KEY = re.compile(
    r"^(?P<indent>\s*(?:-\s+)?)(?P<key>['\"]?(?:[\w-]*(?:password|passwd|secret|api_key|apikey)|[\w-]*(?:token|pin|code))"
    r"['\"]?\s*:)\s*(?P<props>(?:(?:!\S*|&\S+)\s+){0,2})[|>][1-9+-]{0,2}\s*(?:#.*)?$", re.I)
_NAMED_TAG = re.compile(r"(?<!\S)![A-Za-z][\w-]*(?!\S)")
YAML_EXT = re.compile(r"\.ya?ml$", re.I)
# The escapes JSON, YAML and most languages write for a character (F3): é, \U0001f600, \x41.
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})|\\x([0-9a-fA-F]{2})")


def _safe_binary(path, data):
    """A binary kind that holds no text of its own to leak: a font, a GIF or an icon by its first bytes, or Python
    bytecode (a .pyc whose header has the \\r\\n mark), whose strings come from the .py file the guard reads."""
    return data.startswith(_SAFE_MAGIC) or (path.endswith(".pyc") and data[2:4] == b"\r\n")


def is_image(data):
    """True when the bytes are a JPEG, PNG, TIFF, WebP or HEIF/AVIF container, whatever the file is called."""
    return bool(data) and (data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                           or (data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS))


def decode_text(data):
    """The text of a file git calls binary, or None when it is not text the guard can read (F2). A byte-order mark
    names UTF-8, UTF-16 or UTF-32; without one, NUL bytes in a UTF-16 or UTF-32 pattern are tried as those, and a file
    with no NUL byte is UTF-8 (Latin-1 when it is not valid UTF-8, as git itself shows it)."""
    if data is None:
        return None
    for bom, codec in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                       (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(codec)
            except UnicodeDecodeError:
                return None
    if b"\x00" not in data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")
    for codec in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        try:
            text = data.decode(codec)
        except UnicodeDecodeError:
            continue
        if text and "\x00" not in text and sum(c.isprintable() or c in "\t\r\n" for c in text) >= 0.95 * len(text):
            return text
    return None


# Characters that print as nothing: a byte-order mark (U+FEFF, also at the start of a UTF-8 file that git reads as
# text), zero-width spaces and joiners, a soft hyphen. One before `alarm_pin` stops a line-start shape from matching,
# so each line is also judged with them removed (KIT-PUBr.f.pr2 F2).
_INVISIBLE = re.compile("[\u00ad\u180e\u200b-\u200f\u2060-\u2064\ufeff]")


def visible(text):
    """The text without the characters that print as nothing (_INVISIBLE)."""
    return _INVISIBLE.sub("", text)


# What YAML reads as a line break. A bare carriage return is one, so a block key and its value can share one line
# of the diff (KIT-PUBr.f.pr F2).
_YAML_BREAK = re.compile("\r\n|[\r\x85\u2028\u2029]")


def block_values(text):
    """(lineno, joined, key_lineno) for each YAML block scalar under a secret key: the key and the first line of its
    value as one line (`wifi_password: <value>`), numbered as the line of the file that holds the value, so the line
    shapes judge it with their usual exceptions (a `!secret` reference, a placeholder, a variable). key_lineno is the
    line that holds the key: a change to the key alone makes an old value a secret (KIT-PUBr.f.pr2 F1), so a caller
    judges the pair when either line is new. Lines start at 1. The key and the value are found on YAML's own lines,
    which a bare carriage return also ends, after the characters that print as nothing are removed."""
    lines = [(n, visible(piece)) for n, line in enumerate(text_lines(text), 1) for piece in _YAML_BREAK.split(line)]
    out = []
    for i, (key_n, line) in enumerate(lines):
        m = _BLOCK_KEY.match(line)
        if not m:
            continue
        indent = len(m.group("indent"))
        tags = "".join(t + " " for t in _NAMED_TAG.findall(m.group("props")))
        for n, value in lines[i + 1:]:
            if not value.strip():
                continue
            if len(value) - len(value.lstrip()) > indent:
                out.append((n, m.group("indent") + m.group("key") + " " + tags + value.strip(), key_n))
            break
    return out


def unescape(line):
    """The line with \\uXXXX, \\UXXXXXXXX and \\xXX escapes turned into their characters (a surrogate pair into one)."""
    def char(m):
        return chr(int(m.group(1) or m.group(2) or m.group(3), 16))
    try:
        return _ESCAPE.sub(char, line).encode("utf-16", "surrogatepass").decode("utf-16")
    except (UnicodeDecodeError, ValueError):
        return _ESCAPE.sub(char, line)
ALLOW = "guard: allow"
# The marker counts only with a reason after it: a word of three letters or more (`guard: allow no literal`).
ALLOW_RX = re.compile(r"guard: allow\b.*?[A-Za-z]{3,}")
NAMES_FILE = "names.txt"


def has_gps(data):
    """True when an EXIF block in the image bytes holds GPS coordinates (latitude or longitude). Reads the TIFF
    structure EXIF uses, wherever it sits: after "Exif\\0\\0" (JPEG, HEIC, some WebP writers), in a PNG eXIf chunk,
    in a WebP EXIF chunk (8 bytes after its name), or as a TIFF file. A damaged block is treated as no GPS; the point is phones' default location tags, not a forensic tool."""
    starts = [i + 6 for i in _find_all(data, b"Exif\x00\x00")]
    starts += [i + 4 for i in _find_all(data, b"eXIf")]
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # a WebP EXIF chunk holds the TIFF header bare (F4)
        starts += [i + 8 for i in _find_all(data, b"EXIF")]
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        starts.append(0)
    return any(_tiff_gps(data, s) for s in starts)


def _find_all(data, needle):
    i = data.find(needle)
    while i != -1:
        yield i
        i = data.find(needle, i + 1)


def _tiff_gps(data, base):
    import struct
    try:
        order = {b"II": "<", b"MM": ">"}[data[base:base + 2]]
        if struct.unpack(order + "H", data[base + 2:base + 4])[0] != 42:
            return False

        def entries(off):
            n = struct.unpack(order + "H", data[base + off:base + off + 2])[0]
            for k in range(min(n, 512)):
                e = base + off + 2 + 12 * k
                tag, _typ, _cnt = struct.unpack(order + "HHI", data[e:e + 8])
                yield tag, struct.unpack(order + "I", data[e + 8:e + 12])[0]

        ifd0 = struct.unpack(order + "I", data[base + 4:base + 8])[0]
        gps = dict(entries(ifd0)).get(0x8825)
        if not gps:
            return False
        return any(tag in (0x0002, 0x0004) for tag, _ in entries(gps))  # GPSLatitude, GPSLongitude
    except (KeyError, struct.error, IndexError):
        return False


def load_list(path):
    """Lines of a guard list file (names.txt, shrink.txt) with comments and blanks dropped; None if no file."""
    try:
        with open(path, encoding="utf-8") as fh:
            return [e for e in (re.sub(r"(^|\s)#.*$", "", ln).strip() for ln in fh) if e]
    except FileNotFoundError:
        return None


def load_names(path):
    """Compile the household names file into whole-word, case-insensitive patterns; None if there is no file."""
    entries = load_list(path)  # a whole-line or " #" comment is dropped
    if entries is None:
        return None
    return [re.compile(r"(?<!\w)" + re.escape(e) + r"(?!\w)", re.I) for e in entries]


# core.quotePath=false: git writes a path with a non-ASCII letter as it is, not as "caf\303\251.md" (KIT-GUARD-FIX4).
# Paths are read as UTF-8 whatever the locale (a hook may run under LANG=C); a byte that is not UTF-8 survives as a
# surrogate and goes back to git unchanged.
GIT = ["git", "-c", "core.quotePath=false"]


def git(repo, *args):
    # Bytes first, then decoded: text mode would turn a bare carriage return into a new line, and the part of an
    # added line after it would lose its `+` mark and go unscanned (KIT-PUBr F3).
    out = subprocess.run([*GIT, "-C", repo, *args], capture_output=True, check=True).stdout
    return out.decode("utf-8", "surrogateescape")


def text_lines(text):
    """The lines of a text, split on \n only (a Windows \r\n ending loses its \r). str.splitlines() also splits on
    a bare \r and other separators, which would show a line the way an editor does but number it wrongly; scan()
    looks at each piece between those separators instead (KIT-PUBr F3)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


# Characters that an editor or `cat` may show as a line break inside one line of the file (KIT-PUBr F3).
LINE_SEPARATORS = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def git_bytes(repo, *args):
    r = subprocess.run([*GIT, "-C", repo, *args], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def git_paths(repo, *args):
    """The paths a --name-only / ls-files style command lists, read NUL-separated (-z), so no name is quoted."""
    return [p for p in git(repo, *args, "-z").split("\0") if p]


_C_ESC = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_path(text):
    """A path as a diff header gives it: git still C-quotes one with a double quote, a backslash or a control
    character ("b/say \\"hi\\".md"), and puts a tab after one that has a space. Returns the bare name."""
    text = text[:-1] if text.endswith("\t") else text  # a real tab in a name is always quoted, so this one is git's
    if not (len(text) >= 2 and text[0] == '"' and text[-1] == '"'):
        return text
    out, body, i = bytearray(), text[1:-1], 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 == len(body):
            out += c.encode("utf-8", "surrogateescape")
            i += 1
        elif body[i + 1] in "01234567" and len(body) >= i + 4:
            out.append(int(body[i + 1:i + 4], 8) & 0xFF)
            i += 4
        else:
            esc = _C_ESC.get(body[i + 1])
            out += bytes([esc]) if esc is not None else body[i + 1].encode("utf-8", "surrogateescape")
            i += 2
    return out.decode("utf-8", "surrogateescape")


def range_args(rng):
    """The range as git arguments. The remote in `<tip> --not --remotes=<remote>` is one argument even with a space in
    it (the hook's $1 is a URL or path when the remote has no name; KIT-GUARD-FIX4); a range given by hand is split
    on spaces."""
    tip, marker, remote = rng.partition(NOT_ON + "=")
    if marker and tip and " " not in tip:
        return [tip, "--not", "--remotes=" + remote]
    return rng.split()


NOT_ON = " --not --remotes"
NOT_ON_A_REMOTE = "HEAD" + NOT_ON
ZERO = re.compile(r"0+")


def resolve_range(repo, rng, remote=None):
    """The range to scan: the one given, else @{u}..HEAD, else the commits no remote has (a new branch: history the
    remote already holds was checked when it was pushed, and nobody can add an allow marker to an old commit), else
    None (no remote-tracking branch at all: nothing pushed yet).
    remote: the remote being pushed to (the pre-push hook's first argument). Then only that remote counts: the
    upstream only when it is one of that remote's branches, else the commits that remote has not got
    (`HEAD --not --remotes=<name>`; a remote with no branch yet, or a URL, matches nothing: the whole history is
    scanned). A commit that only a second remote holds was never checked on its way to this one."""
    if rng is not None:
        return rng
    try:
        up = git(repo, "rev-parse", "--verify", "--symbolic-full-name", "@{u}").strip()
        if not remote or up.startswith(f"refs/remotes/{remote}/"):
            return "@{u}..HEAD"
    except subprocess.CalledProcessError:
        pass
    if remote:
        return f"{NOT_ON_A_REMOTE}={remote}"
    if git(repo, "for-each-ref", "--count=1", "refs/remotes").strip():
        return NOT_ON_A_REMOTE
    return None


def pushed_ranges(repo, remote, lines):
    """The ranges a push sends, from the lines git gives a pre-push hook on standard input
    (`<local ref> <local sha> <remote ref> <remote sha>`, one per ref; KIT-PUBr F2). A push of another branch, of a
    tag, `--all` or `--tags` sends refs that are not the checked-out branch, so each line is scanned on its own:
      - a delete (local sha all zeros) sends nothing;
      - an update of a ref the remote has, at a commit this clone holds: `<remote sha>..<local sha>`;
      - a new ref, or a remote commit this clone does not hold (a force push): the commits the remote has not got,
        `<local sha> --not --remotes=<remote>` (a remote given as a URL or path matches no branch: all of history);
      - a ref that is not a commit when its tags are peeled (a tag of a blob or a tree): no range; it goes to
        `refused` as (ref, None, kind), because the guard reads commits (KIT-PUBr.f.pr F1);
      - an annotated tag: its tag object, and any tag it points through, goes to `tags`, so the caller scans the
        tag message; a tag of a commit the remote already has still sends that message (KIT-PUBr.f.pr3 F2).
    Returns (ranges, refused, tags)."""
    out, refused, tags = [], [], []
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            if line.strip():
                raise ValueError("a pre-push line that is not four fields")
            continue
        local_sha, remote_sha = fields[1], fields[3]
        if ZERO.fullmatch(local_sha):
            continue
        kind = subprocess.run([*GIT, "-C", repo, "cat-file", "-t", local_sha + "^{}"], capture_output=True)
        if kind.stdout.strip() != b"commit":
            refused.append((fields[0], None, "a ref that is not a commit (a tag of a blob or a tree); push commits only"))
            continue
        tags += [t for t in tag_objects(repo, local_sha) if t not in tags]
        known = not ZERO.fullmatch(remote_sha) and subprocess.run(
            [*GIT, "-C", repo, "cat-file", "-e", remote_sha + "^{commit}"], capture_output=True).returncode == 0
        rng = f"{remote_sha}..{local_sha}" if known else (f"{local_sha}{NOT_ON}={remote}" if remote else f"{local_sha}{NOT_ON}")
        if rng not in out:
            out.append(rng)
    return out, refused, tags


def hook_lines(repo, remote, refs):
    """The by-hand route (KIT-PUBr.f.pr3 F1): the lines a pre-push hook would get for `git push <remote> <refs>`, and
    the refspecs that push exactly those objects. A ref is a branch or tag name, `HEAD`, or `<src>:<dst>`; a leading
    `+` forces. Each source is resolved to its object id now, so the push sends what was checked, not the branch
    checked out and not a commit made later. The remote's side of each ref is read with ls-remote.
    Returns (lines, specs)."""
    lines, specs = [], []
    for ref in refs:
        force = ref.startswith("+")
        src, _, dst = ref.lstrip("+").partition(":")
        sha = git(repo, "rev-parse", "--verify", src).strip()
        full = git(repo, "rev-parse", "--symbolic-full-name", src).strip()
        if not dst:
            if not full.startswith(("refs/heads/", "refs/tags/")):
                raise ValueError(f"name where {src} goes on the remote: {src}:refs/heads/<branch>")
            dst = full
        elif not dst.startswith("refs/"):
            dst = ("refs/tags/" if full.startswith("refs/tags/") else "refs/heads/") + dst
        remote_side = git(repo, "ls-remote", remote, dst).split()
        remote_sha = remote_side[0] if remote_side else "0" * 40
        lines.append(f"{full or src} {sha} {dst} {remote_sha}")
        specs.append(("+" if force else "") + f"{sha}:{dst}")
    return lines, specs


def range_ends(rng):
    """(base, tip) of an "a..b" range; a bare revision, or `<tip> --not --remotes...`, is its own tip with no base."""
    if rng and NOT_ON in rng:
        return None, rng.split()[0]
    if rng and ".." in rng:
        a, b = rng.split("...", 1) if "..." in rng else rng.split("..", 1)
        return a or "HEAD", b or "HEAD"
    return None, rng.split()[0] if rng else "HEAD"


def reader(repo, rng):
    """read(path) -> the bytes of that path as it will be pushed (the range's tip, or the working tree)."""
    if rng is None:
        def read(path, rev=None):
            if rev:
                return git_bytes(repo, "show", f"{rev}:{path}")
            try:
                with open(os.path.join(repo, path), "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        return read
    tip = range_ends(rng)[1]
    return lambda path, rev=None: git_bytes(repo, "show", f"{rev or tip}:{path}")


def shrink_findings(repo, rng, paths):
    """(path, None, kind) for each listed path the range cuts under half (from 20+ lines) or deletes."""
    if rng is None or not paths:
        return []
    base, tip = range_ends(rng)
    if base is None:
        return []
    out = []
    for path in paths:
        before = git_bytes(repo, "show", f"{base}:{path}")
        if before is None:
            continue  # not there at the start: nothing to shrink
        after = git_bytes(repo, "show", f"{tip}:{path}")
        if after is None:  # deleted, whatever its size
            out.append((path, None, "shrink (deleted; a listed file may not be deleted)"))
            continue
        b, a = before.count(b"\n"), after.count(b"\n")
        if b >= 20 and a < b // 2:
            out.append((path, None, f"shrink ({b} -> {a} lines; a truncating write?)"))
    return out


def changes(repo, rng):
    """Yield (path, lineno, line) for every line added by any commit in the range, and (path, None, None) for every
    path one touches; a path from a commit carries that commit in the line-number slot, (path, commit, None), for
    the photo check. rng None =
    nothing pushed yet (resolve_range): every tracked file as it is now, then every commit in the history, which is
    pushed too. A path or line is yielded once across both passes."""
    seen = set()
    if rng is None:  # nothing pushed yet: scan every tracked file as it is now
        for path in git_paths(repo, "ls-files"):
            seen.add((path, None))
            try:
                with open(f"{repo}/{path}", "rb") as fh:
                    data = fh.read()
            except (FileNotFoundError, IsADirectoryError):
                yield path, None, None
                continue
            # Read as bytes, so a UTF-16 file is decoded and a file that is not text is looked at (F2).
            text = None if is_image(data) else decode_text(data)
            yield path, None, (BINARY if text is None else None)
            if text is None:
                continue
            numbered = list(enumerate(text_lines(text), 1))
            if YAML_EXT.search(path):
                numbered += [(n, joined) for n, joined, _ in block_values(text)]
            for n, line in numbered:
                if (path, line) not in seen:
                    seen.add((path, line))
                    yield path, n, line
        try:
            git(repo, "rev-parse", "--verify", "-q", "HEAD")
        except subprocess.CalledProcessError:
            return  # no commit yet: no history
        rng = "HEAD"
    for commit in git(repo, "rev-list", "--reverse", *range_args(rng)).split():
        yield from message_lines(repo, commit, "commit")
        for item in _commit_changes(repo, commit):
            key = (item[0], item[2])
            if item[2] is BINARY or (item[2] is None and IMAGE_EXT.search(item[0])):
                key += (commit,)  # every version of a photo or a binary file is pushed, so each is read (F3)
            if key not in seen:  # the same path or line in several commits is scanned once
                seen.add(key)
                yield item if item[2] is not None else (item[0], commit, None)


# A signed tag's signature: one armored block at the very end of the message, holding base64 lines only
# (KIT-PUBr.f.pr4 F2). Text a person can type into a message never matches it, so it cannot end the scan early.
TAG_SIGNATURE = re.compile(r"(?:\A|\n)-----BEGIN (PGP SIGNATURE|SSH SIGNATURE|SIGNED MESSAGE)-----\n"
                           r"[A-Za-z0-9+/=\n]*-----END \1-----\n?\Z")


def message_lines(repo, sha, kind):
    """(label, lineno, line) for each line of a commit's or an annotated tag's message. The message travels with the
    push like a file does, so it is scanned with the same shapes (KIT-PUBr.f.pr3 F2). The label stands in for a path:
    `commit 1a2b3c4d5e6f message`. The header (tree, parents, author and committer names) is not scanned: the
    owner's own name is on the names list and on every commit. A commit's signature is in its header, so the whole
    commit message is scanned. A signed tag's signature ends its message; only a well-formed block at the end is left
    out (TAG_SIGNATURE). A signature marker anywhere else is text like any other line (KIT-PUBr.f.pr4 F2)."""
    raw = git(repo, "cat-file", kind, sha)
    _, _, body = raw.partition("\n\n")
    if kind == "tag":
        body = TAG_SIGNATURE.sub("\n", body)
    label = f"{kind} {sha[:12]} message"
    for n, line in enumerate(text_lines(body), 1):
        yield label, n, line


MAX_TAG_DEPTH = 20


def tag_objects(repo, sha):
    """The annotated tag objects a ref points through, outermost first (a tag of a tag gives two); none for a commit.
    A chain longer than MAX_TAG_DEPTH raises ValueError, so the push is refused: git sends every tag in the chain,
    and a tag the guard has not read must not go through (KIT-PUBr.f.pr4 F3)."""
    out = []
    while git(repo, "cat-file", "-t", sha).strip() == "tag":
        if len(out) == MAX_TAG_DEPTH:
            raise ValueError(f"a chain of more than {MAX_TAG_DEPTH} annotated tags; push refused, not checked")
        out.append(sha)
        header = git(repo, "cat-file", "tag", sha)
        sha = re.match(r"object (\S+)", header).group(1)  # the first header line names what the tag points at
    return out


def _commit_changes(repo, commit):
    """(path, None, None) for each path a commit adds or changes and (path, lineno, line) for each line it adds,
    against its parent (a root commit: against the empty tree). A merge adds only what is new against EVERY parent:
    after `git pull` the first parent is the local branch, and the lines the merge brought from the remote are not
    the pusher's."""
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    if len(parents) > 1:
        per_parent = [list(_diff_changes(repo, p, commit)) for p in parents]
        keep = set.intersection(*({(i[0], i[2]) for i in items} for items in per_parent))
        yield from (i for i in per_parent[0] if (i[0], i[2]) in keep)
        return
    base = parents[0] if parents else git(repo, "hash-object", "-t", "tree", "/dev/null").strip()
    yield from _diff_changes(repo, base, commit)


def _binary_paths(repo, base, commit):
    """The paths git shows as binary between base and commit (numstat `-`): a UTF-16 file, a photo, or a text file
    a .gitattributes `-diff` hides. Their lines never reach the text diff, so their bytes are read instead (F2)."""
    out, fields = set(), git(repo, "diff", "--numstat", "-z", "--no-renames", "--no-textconv", "--no-ext-diff",
                             base, commit).split("\0")
    for record in fields:
        parts = record.split("\t", 2)
        if len(parts) == 3 and parts[0] == "-" and parts[1] == "-" and parts[2]:
            out.add(parts[2])
    return out


def _decoded_lines(repo, base, commit, path):
    """(text, added) for a binary path: its text at the commit, and the line numbers whose text the base lacked;
    (None, None) when it is not text the guard can read."""
    data = git_bytes(repo, "show", f"{commit}:{path}")
    if is_image(data):
        return None, None
    text = decode_text(data)
    if text is None:
        return None, None
    old = decode_text(git_bytes(repo, "show", f"{base}:{path}")) or ""
    before = set(text_lines(old))
    return text, {n for n, line in enumerate(text_lines(text), 1) if line not in before}


def _diff_changes(repo, base, commit):
    # Paths from --name-only, so an empty new file (no hunk) is still seen.
    paths = git_paths(repo, "diff", "--name-only", "--diff-filter=ACMR", base, commit)
    binary = _binary_paths(repo, base, commit) & set(paths)
    added, texts = {}, {}  # path -> {lineno: line}; path -> its text at the commit (read for a YAML block, F1)
    for path in paths:
        if path in binary:
            text, numbers = _decoded_lines(repo, base, commit, path)
            if text is None:
                yield path, commit, BINARY
                continue
            texts[path] = text
            lines = text_lines(text)
            added[path] = {n: lines[n - 1] for n in sorted(numbers)}
        yield path, None, None
    for path, lines in added.items():
        for n, line in lines.items():
            yield path, n, line
    yield from _text_diff(repo, base, commit, added)
    for path, lines in added.items():
        if not YAML_EXT.search(path) or not lines:
            continue
        text = texts.get(path)
        if text is None:
            text = decode_text(git_bytes(repo, "show", f"{commit}:{path}")) or ""
        for n, joined, key_n in block_values(text):
            if n in lines or key_n in lines:  # a new value, or an old value under a changed key (pr2 F1)
                yield path, n, joined


def _text_diff(repo, base, commit, added):
    """(path, lineno, line) for each line the text diff adds; each is also recorded in added[path]."""
    # A `+++ ` line is a file header only between a `diff ` line and that file's first hunk: inside a hunk it is an
    # added line that starts with `++ ` (KIT-GUARD-FIX3), which must not end the file or rename it.
    # No textconv or external diff: a local diff driver must not change what is scanned. The prefixes are set here,
    # so a preference such as diff.noprefix or diff.mnemonicPrefix cannot hide every path (KIT-PUBr.f.pr F3).
    path, n, header = None, 0, False
    for line in git(repo, "diff", "--no-color", "--no-textconv", "--no-ext-diff", "--no-relative", "--src-prefix=a/",
                    "--dst-prefix=b/", "--unified=0", base, commit).split("\n"):
        if line.startswith("diff "):
            path, header = None, True
        elif header and line.startswith("+++ "):
            name = unquote_path(line[4:])  # `b/x`, `"b/x \"y\""`, `b/x y<TAB>` or /dev/null
            path = name[2:] if name.startswith("b/") else None
        elif line.startswith("@@"):
            header = False
            m = re.search(r"\+(\d+)", line)
            n = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            added.setdefault(path, {})[n] = line[1:]
            yield path, n, line[1:]
            n += 1


def scan(items, names=(), names_path=None, read=None, notes=None, binary_ok=()):
    """items: (path, lineno, line); line None = a path check, whose second item is the commit that touched the
    path (None = as it will be pushed); line BINARY = the same for a file git calls binary that is not text the
    guard can read. names: patterns from load_names; names_path: the repo-relative path of the names file, which is
    not scanned. read: path -> bytes for the photo check (None = off). notes: a list that gets a one-line notice when
    a long line was scanned in pieces. binary_ok: paths whose unreadable bytes the owner has looked at (binary.txt).
    Returns (path, lineno, kind), each once."""
    findings = []
    long_lines = 0
    for path, n, line in items:
        if line is None or line is BINARY:
            if path != names_path and any(rx.search(path) for rx in names):  # F5: a listed word in a file name
                findings.append((path, None, "household name in the path"))
            if any(p.search(path) for p in FORBIDDEN_PATHS):
                findings.append((path, None, "forbidden path"))
            elif read and (IMAGE_EXT.search(path) or line is BINARY):
                # F3: as the commit that touched it left it (n), not at the tip: a copy stripped in a later commit
                # does not take the first one out of the pushed history. A photo is known by its bytes too (F4).
                data = read(path, n) if n else read(path)
                if data and (IMAGE_EXT.search(path) or is_image(data)):
                    if has_gps(data):
                        findings.append((path, None, "photo location (GPS in EXIF)"))
                elif data and not _safe_binary(path, data) and path not in binary_ok:
                    findings.append((path, None, "unreadable content (binary of a kind the guard cannot read; "
                                                 "list it in binary.txt once checked)"))
            continue
        split_line = bool(LINE_SEPARATORS.search(line))
        if ALLOW_RX.search(line) and not split_line:
            continue
        # F3: an escaped character (`"password"`, a name as json.dumps writes it) is scanned as its character too.
        variants = [line]
        if "\\" in line:
            plain = unescape(line)
            if plain != line:
                variants.append(plain)
        # F3 (KIT-PUBr): a bare carriage return or another separator inside the line starts what an editor shows as
        # a new line; each piece is judged on its own, so a key at the start of a piece is still at a line start.
        for text in list(variants):
            if LINE_SEPARATORS.search(text):
                variants.extend(piece for piece in LINE_SEPARATORS.split(text) if piece and piece not in variants)
        # pr2 F2: a byte-order mark or a zero-width character in front of a key hides it from the line-start shapes.
        variants.extend(v for v in (visible(text) for text in list(variants)) if v not in variants)
        if split_line:  # an allow marker covers only the piece it is in, not the pieces shown as other lines
            variants = [text for text in variants if not ALLOW_RX.search(text)]
        kinds = []
        for text in variants:
            masked = text
            for rx in HEX_EXCEPTIONS:
                masked = rx.sub(lambda m: m.group(1) + "<commit>", masked)
            if text is line and len(masked) > LONG_LINE:
                long_lines += 1
            for name, rx in SHAPES:
                if name not in kinds and _search(rx, masked):
                    kinds.append(name)
        findings.extend((path, n, k) for k in kinds)
        if path != names_path and any(rx.search(text) for rx in names for text in variants):
            findings.append((path, n, "household name"))
    if long_lines and notes is not None:
        notes.append(f"{long_lines} line(s) over {LONG_LINE} characters scanned in overlapping pieces")
    return list(dict.fromkeys(findings))


def _search(rx, text):
    """rx.search over the text, a long line in overlapping pieces (endpos keeps `^` at the real line start)."""
    if len(text) <= LONG_LINE:
        return rx.search(text)
    return any(rx.search(text, i, i + LONG_LINE + OVERLAP) for i in range(0, len(text), LONG_LINE))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--range", dest="rng", default=None, help="git range; default @{u}..HEAD")
    ap.add_argument("--remote", default=None,
                    help="the remote being pushed to (the pre-push hook's $1); only that remote's branches count")
    ap.add_argument("--pushed-refs", action="store_true",
                    help="read the refs being pushed from standard input, as git gives them to a pre-push hook, and "
                         "scan each one (the hook mode)")
    ap.add_argument("--push", nargs="+", metavar=("REMOTE", "REF"),
                    help="the by-hand route: check exactly what pushing these refs to REMOTE sends, the same way the "
                         "hook does, and push those commits only when the check is clean (KIT-PUBr.f.pr3 F1)")
    ap.add_argument("--names", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), NAMES_FILE),
                    help="household names file; default names.txt next to this script")
    ap.add_argument("--shrink", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), SHRINK_FILE),
                    help="files that may not be cut under half; default shrink.txt next to this script")
    ap.add_argument("--binary", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), BINARY_FILE),
                    help="binary files the owner has checked; default binary.txt next to this script")
    a = ap.parse_args(argv)
    names = load_names(a.names)
    if names is None:
        print(f"push_guard: no names file at {a.names}; the household-names check is off", file=sys.stderr)
    shrink = load_list(a.shrink)
    try:
        findings, tags, push_specs = [], [], None
        if a.push:
            if len(a.push) < 2:
                raise ValueError("--push needs a remote and at least one branch or tag")
            a.remote, (push_lines, push_specs) = a.push[0], hook_lines(a.repo, a.push[0], a.push[1:])
            ranges, findings, tags = pushed_ranges(a.repo, a.remote, push_lines)
        elif a.pushed_refs:
            ranges, findings, tags = pushed_ranges(a.repo, a.remote, sys.stdin.read().splitlines())
        else:
            ranges = [resolve_range(a.repo, a.rng, a.remote)]
        names_path = os.path.relpath(os.path.abspath(a.names), os.path.abspath(a.repo)).replace(os.sep, "/")
        notes = []
        for rng in ranges:
            findings += scan(changes(a.repo, rng), names or (), names_path, reader(a.repo, rng), notes,
                             set(load_list(a.binary) or ()))
            findings += shrink_findings(a.repo, rng, shrink)
        for tag in tags:
            findings += scan(message_lines(a.repo, tag, "tag"), names or (), names_path)
        findings = list(dict.fromkeys(findings))
    except subprocess.CalledProcessError as e:
        print(f"push_guard: git failed: {e.stderr.decode('utf-8', 'replace').strip()}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"push_guard: {e}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"push_guard: {note}", file=sys.stderr)
    for path, n, kind in findings:
        # The file, line and kind only: the point is to stop the push, not to copy a secret into a log.
        print(f"ABORT {kind}: {path}" + (f":{n}" if n else ""))
    if findings:
        print(f"push_guard: {len(findings)} finding(s); push refused. Fix them, or mark a false positive "
              f"with '{ALLOW} <reason>' on that line.")
        return 1
    print("push_guard: clean")
    if push_specs:
        # The objects checked above, by their ids, so a commit made after the check is not sent (pr3 F1).
        # --no-verify: this route is for a computer that cannot run the hook, and the check has just run.
        # --no-follow-tags and --recurse-submodules=no: push.followTags would add annotated tags, and
        # push.recurseSubmodules other repositories, that the check did not read (KIT-PUBr.f.pr4 F1).
        r = subprocess.run([*GIT, "-C", a.repo, "push", "--no-verify", "--no-follow-tags", "--recurse-submodules=no",
                            a.remote, *push_specs])
        return 0 if r.returncode == 0 else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
