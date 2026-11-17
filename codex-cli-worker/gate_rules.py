"""The rules of the remote-control command check, shared by the Codex hook and the worker's config watch.

Commands from the ChatGPT app reach Codex through OpenAI's relay and skip this
app's Allow and Deny buttons. The check below holds outside the model:

- ``check_tool_call`` is what the managed Codex hook (``gate/gate_hook.py``)
  runs for every tool call and every permission request. It refuses named
  security restrictions and otherwise says nothing, preserving Codex's
  normal approval flow. It always refuses reads of process environments, the Supervisor token,
  Home Assistant's sign-in store and ``secrets.yaml``, and file edits that add
  a command that can reach anything or a templated action.
- ``find_refused`` is the scan both sides use on YAML text. The worker runs it
  over ``/config`` to catch edits the hook cannot see (a shell command that
  writes a file) and raises a Home Assistant repair for them.
- ``refused_blocks`` always hashes definition, platform and template blocks,
  while unchanged calls and templated targets follow normal approval.

The refused domains live in one file, ``policy/refused-domains.json``
(``/usr/share/ha_codex/refused-domains.json`` in the image). When that file is
missing the built-in list below is used; it is never looser than the file.

The scan reads lines, not parsed YAML, so it also works on half an edit. It
can miss an obfuscated reference; the native sandbox, managed policy and
configuration watch remain separate defenses.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import os
import shlex
import stat
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The hook and its tests know the join step by its old private name.
from shell_words import join_shell_continuations as _join_shell_continuations
from shell_words import split_words, strip_comments

IMAGE_DIR = Path("/usr/share/ha_codex")
SOURCE_DIR = Path(__file__).resolve().parent / "policy"
CONFIG_ROOT = Path("/config")


STRICT_RULES: dict[str, Any] = {
    "reaches_anything": ["hassio", "pyscript", "python_script", "rest_command", "shell_command"],
    "runs_on_load_platforms": ["command_line", "rest", "scrape"],
    "runs_on_load_keys": ["command_line", "rest", "rest_command", "scrape", "shell_command", "python_script", "pyscript"],
}
LIST_KEYS = ("reaches_anything", "runs_on_load_platforms", "runs_on_load_keys")


ACTION_RE = re.compile(r"""^\s*-?\s*(?:service|action)\s*:\s*["']?([a-z0-9_]+)\.([a-z0-9_]+)""")
TEMPLATE_ACTION_RE = re.compile(r"""^\s*-?\s*(?:(?:service|action)\s*:.*(?:\{\{|\{%)|service_template\s*:)""")
PLATFORM_RE = re.compile(r"""^\s*-?\s*platform\s*:\s*["']?([a-z0-9_]+)["']?\s*(?:#.*)?$""")
# A definition key can hold a block, include or inline value: all run on load.
DEFINITION_KEY_RE = re.compile(r"""^\s*-?\s*([a-z0-9_]+)\s*:""")
SCRIPT_REF_RE = re.compile(r"""\bscript\.([a-z0-9_]+)\b""")
SCRIPT_TARGET_RE = re.compile(r"""^\s*-?\s*(?:["']?script\.|entity_id\s*:|target\s*:\s*\{\s*entity_id\s*:)""")
# The script domain's own services are not scripts.
SCRIPT_SERVICES = {"turn_on", "turn_off", "toggle", "reload"}

# Reads refused from every client: process environments (the worker's holds the Supervisor token),
# the Supervisor itself, Home Assistant's sign-in store (its signing key) and secrets.yaml.
PROTECTED_COMMAND_RES = (
    (re.compile(r"/proc/\S*environ"), "reads a process environment, which can hold the Supervisor token"),
    (re.compile(r"\b(?:SUPERVISOR_TOKEN|HASSIO_TOKEN)\b"), "uses the Supervisor token"),
    (re.compile(r"/run/s6/container_environment"), "reads the app's saved environment, which holds the Supervisor token"),
    (re.compile(r"https?://supervisor\b|\bsupervisor/|\bha\s+(?:supervisor|super|su)\b|\bbashio::", re.IGNORECASE),
     "talks to the Home Assistant Supervisor"),
    (re.compile(r"\.storage/+auth"), "reads Home Assistant's sign-in store"),
    (re.compile(r"secrets\.ya?ml"), "reads secrets.yaml"),
)
PROTECTED_PATH_RES = (
    (re.compile(r"(?:^|/)\.storage/+auth"), "edits Home Assistant's sign-in store"),
    (re.compile(r"(?:^|/)secrets\.ya?ml$"), "edits secrets.yaml"),
)
PATCH_BEGIN = "*** Begin Patch"
PATCH_FILE_RE = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+?)\s*$")
YAML_SUFFIXES = (".yaml", ".yml")
PATCH_BLOCK_RE = re.compile(r"\*\*\* Begin Patch.*?(?:\*\*\* End Patch|\Z)", re.DOTALL)

# Older payloads can explicitly request sandbox permission. That request
# alone follows native approval; only a named forbidden capability is denied.
ESCALATION_KEYS = ("sandbox_permissions", "with_escalated_permissions", "escalated", "require_escalated")
ESCALATION_VALUES = {"require_escalated", "escalated", "true", "danger-full-access"}


@dataclass(frozen=True)
class Finding:
    """One refused reference in a text: the 1-based line, why it is refused, and the line as written.

    ``kind`` names a command domain, ``refused_script``, ``runs_on_load``
    or an action ``template``; targets and device categories are not refused.
    """

    line: int
    reason: str
    text: str
    kind: str = ""

    def key(self) -> tuple[str, str]:
        """Return what identifies the finding regardless of where it moved in the file."""
        return (self.reason, self.text.strip())


def rules_path() -> Path:
    """Return the image's refused-domain list; a source checkout (tests, development) uses policy/."""
    image_copy = IMAGE_DIR / "refused-domains.json"
    source = SOURCE_DIR / "refused-domains.json"
    return image_copy if image_copy.exists() or not source.exists() else source


def load_rules(path: Path | None = None) -> dict[str, Any]:
    """Return the refused-domain rules; the built-in strict list when the file is missing or unreadable.

    A file can add domains but never remove the built-in ones.
    """
    rules = json.loads(json.dumps(STRICT_RULES))
    try:
        data = json.loads((path or rules_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rules
    if not isinstance(data, dict):
        return rules
    for key in LIST_KEYS:
        extra = data.get(key)
        if isinstance(extra, list):
            rules[key] = sorted(set(rules[key]) | {str(item) for item in extra})
    return rules


def find_refused(text: str, rules: dict[str, Any], refused_scripts: frozenset[str] | set[str] = frozenset()) -> list[Finding]:
    """Find always-on commands, definitions/platforms, templated actions and indirect security calls.

    Device actions and targets follow the owner's normal approval flow. This
    tolerant line scan also works while an edit is incomplete. A python_script
    definition enables scripts that run when called.
    """
    anything = set(rules["reaches_anything"])
    platforms = set(rules["runs_on_load_platforms"])
    definitions = set(rules["runs_on_load_keys"])
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = ACTION_RE.match(line)
        if match:
            domain, service = match.groups()
            if domain in anything:
                findings.append(Finding(number, f"an action on {domain}, which can reach anything", line, domain))
            elif domain == "script" and service not in SCRIPT_SERVICES and service in refused_scripts:
                findings.append(Finding(number, f"a call to script.{service}, which itself acts on a refused domain", line,
                                        "refused_script"))
        else:
            platform = PLATFORM_RE.match(line)
            if platform and platform.group(1) in platforms:
                findings.append(Finding(number, f"a {platform.group(1)} platform, which runs on its own once loaded", line,
                                        "runs_on_load"))
            definition = DEFINITION_KEY_RE.match(line)
            if definition and definition.group(1) in definitions:
                findings.append(Finding(number, f"a {definition.group(1)} definition, which runs on its own once loaded", line,
                                        "runs_on_load"))
        if TEMPLATE_ACTION_RE.match(line):
            findings.append(Finding(number, "a templated action", line, "template"))
        if not match and SCRIPT_TARGET_RE.match(line):
            for ref in SCRIPT_REF_RE.findall(line):
                if ref not in SCRIPT_SERVICES and ref in refused_scripts:
                    findings.append(Finding(number, f"a call to script.{ref}, which itself acts on a refused domain", line,
                                            "refused_script"))
                    break
    return findings


BLOCK_CHANGED = "a change to a block that holds a refused action"


def refused_blocks(text: str, rules: dict[str, Any], scripts: set[str] | frozenset[str] = frozenset()) -> dict[str, tuple[int, int, str]]:
    """Return {hash: (first line number, last line number, first line)} for each top-level block of this YAML text that holds a refused line.

    Definitions, platforms and templated actions stay hashed;
    blocks containing only unchanged calls then follow normal approval.
    A block starts at a line that begins in the first column (``- id: ...`` in
    automations.yaml, ``goodnight:`` in scripts.yaml, ``automation:`` in a
    package). Changing a trigger or a condition of an automation that already
    holds a refused action leaves the action line as it was, so the line
    findings see nothing new; the block's hash changes.
    """
    out: dict[str, tuple[int, int, str]] = {}
    block: list[str] = []
    start = 1
    for number, line in enumerate(text.splitlines() + ["__end__"], start=1):
        if line[:1] and not line[:1].isspace() and not line.startswith("#") and block:
            body = "\n".join(item.rstrip() for item in block).strip()
            findings = find_refused(body, rules, scripts)
            if body and any(finding.kind in {"runs_on_load", "template"} for finding in findings):
                out[hashlib.sha256(body.encode("utf-8")).hexdigest()] = (start, number - 1, block[0])
            block, start = [], number
        block.append(line)
    return out


def new_refused(before: str, after: str, rules: dict[str, Any], scripts: set[str] | frozenset[str] = frozenset()) -> list[Finding]:
    """Find newly added refused lines and changed blocks, without treating moved lines as new."""
    counts = Counter(item.key() for item in find_refused(before, rules, scripts))
    found = []
    for item in find_refused(after, rules, scripts):
        if counts[item.key()]:
            counts[item.key()] -= 1
        else:
            found.append(item)
    # Definition/platform/template-action bodies stay checked. Call-only blocks
    # follow normal approval when the call itself is unchanged.
    previous = refused_blocks(before, rules, scripts)
    for digest, (first, last, text) in refused_blocks(after, rules, scripts).items():
        if digest not in previous and not any(first <= item.line <= last for item in found):
            found.append(Finding(first, BLOCK_CHANGED, text))
    return found



def refused_scripts(texts: dict[str, str], rules: dict[str, Any], deadline: float | None = None) -> set[str]:
    """Resolve scripts transitively using always-on security kinds within the time budget."""
    blocks: dict[str, str] = {}
    for rel, text in texts.items():
        if deadline is not None and time.monotonic() > deadline:
            raise ValueError("script scan time budget exceeded")
        if rel != "scripts.yaml" and not rel.startswith("scripts/"):
            continue
        current, block = "", []
        for line in text.splitlines() + ["__end__:"]:
            key = re.match(r"^([a-z0-9_]+)\s*:\s*(?:#.*)?$", line)
            if key:
                if current:
                    blocks[current] = "\n".join(block)
                current, block = key.group(1), []
            else:
                block.append(line)
    refused: set[str] = set()
    while True:
        found = set()
        for name, text in blocks.items():
            if deadline is not None and time.monotonic() > deadline:
                raise ValueError("script scan time budget exceeded")
            if find_refused(text, rules, refused):
                found.add(name)
        if found <= refused:
            return refused
        refused |= found


def config_refused_scripts(rules: dict[str, Any]) -> set[str]:
    """Read only bounded, ordinary script YAML for patch-hook indirect-call checks."""
    texts: dict[str, str] = {}
    deadline = time.monotonic() + 0.5
    total = 0
    candidates = [CONFIG_ROOT / "scripts.yaml"]
    folder = CONFIG_ROOT / "scripts"
    if folder.is_dir() and not folder.is_symlink():
        for parent, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(parent)/d).is_symlink()]
            if time.monotonic() > deadline:
                raise ValueError("script scan time budget exceeded")
            candidates.extend(Path(parent)/name for name in names if name.endswith(YAML_SUFFIXES))
            if len(candidates) > 3000:
                raise ValueError("script scan file budget exceeded")
    for path in candidates:
        if time.monotonic() > deadline:
            raise ValueError("script scan time budget exceeded")
        try:
            rel = path.relative_to(CONFIG_ROOT).as_posix()
            data = read_config_bytes(CONFIG_ROOT, rel, HOOK_MAX_BYTES)
            total += len(data)
            if total > 8 * 1024 * 1024:
                raise ValueError("script scan byte budget exceeded")
            texts[rel] = data.decode("utf-8", errors="replace")
        except FileNotFoundError:
            continue
    return refused_scripts(texts, rules, deadline=deadline)


PATCH_MAX_BYTES = 256 * 1024
CONFIG_MAX_BYTES = 2 * 1024 * 1024
HOOK_MAX_BYTES = 512 * 1024


def read_config_bytes(root: Path, rel: str, max_bytes: int = CONFIG_MAX_BYTES) -> bytes:
    """Read a bounded ordinary file through no-follow directory descriptors, never credentials."""
    parts = Path(rel).parts
    if (not parts or Path(rel).is_absolute() or any(p.startswith(".") for p in parts)
            or any(p.lower() in {"secrets.yaml", "secrets.yml"} for p in parts)):
        raise ValueError("not an ordinary configuration path")
    folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder)
            folder = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes or info.st_nlink != 1:
                raise ValueError("not a bounded ordinary configuration file")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("configuration file exceeds size limit")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(folder)


def patch_versions(text: str, cwd: str = "") -> list[tuple[str, str, str]]:
    """Reconstruct YAML before/after in memory; an uncertain patch is refused, never executed.

    This accepts apply_patch's context hunks, add/delete and move forms. Each
    context must identify exactly one span after the preceding hunk. A move
    checks both locations and reads only its original, ordinary /config file.
    """
    if len(text.encode("utf-8")) > PATCH_MAX_BYTES:
        raise ValueError("patch exceeds size limit")
    match = PATCH_BLOCK_RE.search(text)
    if not match or not match.group().rstrip().endswith("*** End Patch"):
        raise ValueError("incomplete patch")
    if len(PATCH_BLOCK_RE.findall(text)) != 1:
        raise ValueError("multiple patch bodies are not checked as one edit")
    deadline = time.monotonic() + 0.75
    lines = match.group().splitlines()[1:-1]
    records: list[tuple[str, str, list[str]]] = []
    for line in lines:
        header = PATCH_FILE_RE.match(line)
        if header and header[1] != "Move to":
            records.append((header[1], header[2], []))
        elif records:
            records[-1][2].append(line)
        elif line.strip():
            raise ValueError("patch has no file header")
    out = []
    root = CONFIG_ROOT.absolute()
    def relative(name: str) -> str:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = Path(cwd or root) / candidate
        # Do not normalize away traversal or follow any link, even inside /config.
        if ".." in candidate.parts:
            raise ValueError("patch path traverses directories")
        rel = candidate.relative_to(root).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            raise ValueError("patch path is not ordinary configuration")
        for pattern, _ in PROTECTED_PATH_RES:
            if pattern.search(rel):
                raise ValueError("patch names a protected path")
        return rel
    total = 0
    seen: set[str] = set()
    if len(records) > 64:
        raise ValueError("patch file budget exceeded")
    for kind, name, body in records:
        if time.monotonic() > deadline:
            raise ValueError("patch time budget exceeded")
        moved = next((line.split(": ", 1)[1] for line in body if line.startswith("*** Move to: ")), name)
        if not (name.lower().endswith(YAML_SUFFIXES) or moved.lower().endswith(YAML_SUFFIXES)):
            continue
        source, destination = relative(name), relative(moved)
        if source in seen or destination in seen:
            raise ValueError("repeated file operations cannot be checked independently")
        seen.update((source, destination))
        if kind == "Add File":
            # Add File must not overwrite a real file or a symlink.
            try:
                read_config_bytes(root, source, HOOK_MAX_BYTES)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("added file already exists")
            before = ""
        else:
            before = read_config_bytes(root, source, HOOK_MAX_BYTES).decode("utf-8")
        destination_before = before
        if source != destination:
            if kind != "Update File":
                raise ValueError("only an update may move a file")
            try:
                destination_before = read_config_bytes(root, destination, HOOK_MAX_BYTES).decode("utf-8")
            except FileNotFoundError:
                destination_before = ""
        total += len(before.encode("utf-8"))
        if total > 8 * CONFIG_MAX_BYTES:
            raise ValueError("patch source budget exceeded")
        if kind == "Delete File":
            out.append((destination, before, ""))
            continue
        if kind == "Add File":
            if any(not line.startswith("+") for line in body):
                raise ValueError("invalid added-file body")
            out.append((destination, "", "\n".join(line[1:] for line in body) + "\n"))
            continue
        original = before.splitlines()
        updated: list[str] = []
        cursor = 0
        hunks: list[list[str]] = []
        for line in body:
            if line.startswith("*** Move to:"):
                continue
            if line.startswith("@@"):
                hunks.append([])
            elif line == "*** End of File":
                if not hunks:
                    raise ValueError("invalid end marker")
                hunks[-1].append(line)
            elif hunks and line[:1] in {" ", "+", "-"}:
                hunks[-1].append(line)
            else:
                raise ValueError("invalid patch hunk")
        for hunk in hunks:
            at_end = "*** End of File" in hunk
            hunk = [line for line in hunk if line != "*** End of File"]
            old = [line[1:] for line in hunk if line[0] in " -"]
            new = [line[1:] for line in hunk if line[0] in " +"]
            # Linear KMP matching avoids quadratic context scans that could hit
            # Codex's fail-open hook timeout on a large, repetitive YAML file.
            positions = []
            if not old:
                positions = [len(original)] if at_end or not original else []
            else:
                prefix = [0] * len(old)
                length = 0
                for n in range(1, len(old)):
                    while length and old[n] != old[length]:
                        length = prefix[length - 1]
                    if old[n] == old[length]:
                        length += 1
                    prefix[n] = length
                length = 0
                for n in range(cursor, len(original)):
                    if n % 1024 == 0 and time.monotonic() > deadline:
                        raise ValueError("patch time budget exceeded")
                    while length and original[n] != old[length]:
                        length = prefix[length - 1]
                    if original[n] == old[length]:
                        length += 1
                    if length == len(old):
                        if not at_end or n + 1 == len(original):
                            positions.append(n + 1 - length)
                            if len(positions) > 1:
                                break
                        length = prefix[length - 1]
            if len(positions) != 1:
                raise ValueError("patch context is missing or ambiguous")
            index = positions[0]
            updated.extend(original[cursor:index]); updated.extend(new)
            cursor = index + len(old)
        if not hunks and source == destination:
            raise ValueError("empty update")
        updated.extend(original[cursor:])
        after = "\n".join(updated) + ("\n" if before.endswith("\n") else "")
        out.append((destination, before, after))
        if source != destination:
            # A harmless edit to a text file may plant its unchanged body in YAML.
            # Judge what is new at the destination too, including absent files.
            out.append((destination, destination_before, after))
    return out


def parse_patch(text: str) -> list[tuple[str, str]]:
    """Return (path, added lines) for each file in an apply_patch body; moved files count under their new path."""
    files: list[tuple[str, list[str]]] = []
    inside = False
    for raw in text.splitlines():
        if raw.startswith(PATCH_BEGIN):
            inside = True
            continue
        if not inside:
            continue
        if raw.startswith("*** End Patch"):
            inside = False
            continue
        header = PATCH_FILE_RE.match(raw)
        if header:
            kind, path = header.groups()
            if kind == "Move to" and files:
                files[-1] = (path, files[-1][1])
            else:
                files.append((path, []))
            continue
        if files and raw.startswith("+"):
            files[-1][1].append(raw[1:])
    return [(path, "\n".join(added)) for path, added in files]


def _strings(value: Any) -> list[str]:
    """Return every string inside a JSON value, in order; a list of words (an argv) is also joined into one."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        out = [text for item in value for text in _strings(item)]
        if value and all(isinstance(item, str) for item in value):
            out.append(" ".join(value))
        return out
    return []


def asks_for_escalation(tool_input: Any) -> bool:
    """Return whether a tool call asks to run outside the sandbox (network, paths outside /config)."""
    if not isinstance(tool_input, dict):
        return False
    for key in ESCALATION_KEYS:
        value = tool_input.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() in ESCALATION_VALUES):
            return True
    return False


# These are security restrictions, independent of any retired device category option.
REMOTE_DEFINITIONS = ("shell_command", "rest_command", "command_line", "python_script", "pyscript", "template")
FULL_ACCESS_RE = re.compile(r"danger[-_]?full[-_]?access|dangerFullAccess|--dangerously-bypass-approvals-and-sandbox|--yolo\b", re.IGNORECASE)

HA_REMOTE_FAMILIES = frozenset(("addon", "addons", "add-on", "add-ons", "host", "os", "core",
                                "hardware", "network", "backup", "backups", "apps"))
HA_ALWAYS_FAMILIES = frozenset(("supervisor", "super", "su"))
SHELL_ASSIGNMENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*=")
# The global flags of ha that take the next word as their value.
HA_VALUE_FLAGS = ("--log-level", "--api-token", "--endpoint", "--config")
# A redirection operator, written once for the parsers, the text floor and the spacing below.
# The heredoc operator has one spelling for each text: "<<-x" is "<<-" with the target "x", never
# "<<" with the target "-x". Two readings of the same text would make the floor's match time
# double with each repeated "<<-x".
_REDIRECTION_OPERATOR = r"(?:&>>?|<<<|<<-|<<(?!-)|<>|>>|>\||>&|<&|>|<)"
# An optional file descriptor before the operator: a number or {name}.
_REDIRECTION_FD = r"(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})"
# A redirection as the word splitter returns it: an optional file descriptor (2 or {fd}), the
# operator, then the target when it is joined to the operator (group 1, empty when the next
# word is the target). The shell removes a redirection before the command runs, wherever it
# stands, so "ha 2>/dev/null su info" runs "ha su info".
REDIRECTION_RE = re.compile(r"^" + _REDIRECTION_FD + r"?" + _REDIRECTION_OPERATOR + r"(.*)$", re.DOTALL)
# An operator as it can stand in command text. The shell ends a word at "<" and ">", so
# "ha>/dev/null" is "ha", ">", "/dev/null". A file descriptor (group "fd") belongs to the
# operator only when it starts a word ("2>x"); in "ha2>x" the word is "ha2". A number right
# after an operator is that operator's target instead (_space_redirections).
REDIRECTION_TEXT_RE = re.compile(
    r"(?:(?<![^\s;&|()])(?P<fd>" + _REDIRECTION_FD + r"))?" + _REDIRECTION_OPERATOR)
# A conservative text floor also catches shell constructs the parser cannot place.
# It reads the command with its quotes removed and a blank on each side of every redirection
# operator. The four value-taking global flags with their value, any other flag, and any
# redirection with its target may precede the family.
# The three alternatives cannot match the same text: an alternative that could would make
# the match time double with each repeated flag.
_HA_REDIRECTION_TEXT = _REDIRECTION_FD + r"?" + _REDIRECTION_OPERATOR + r"\s*[^\s<>&|]\S*"
_HA_VALUE_FLAG_TEXT = r"--(?:log-level|api-token|endpoint|config)"
# The same flags without the first dash, to exclude them from the other-flag alternative.
_HA_VALUE_FLAG_AFTER_DASH = r"-(?:log-level|api-token|endpoint|config)"
# What may stand between ha and its family word: flags, flag values and redirections, each
# followed by blanks.
_HA_BEFORE_FAMILY_TEXT = (
    r"(?:(?:"
    + _HA_VALUE_FLAG_TEXT + r"(?:=\S+|(?:\s+" + _HA_REDIRECTION_TEXT + r")*\s+(?!" + _REDIRECTION_FD + r"?[<>&])\S+)"
    + r"|-(?!" + _HA_VALUE_FLAG_AFTER_DASH + r"(?:[=\s]|$))\S+"
    + r"|" + _HA_REDIRECTION_TEXT
    + r")\s+)*")
HA_TEXT_PREFIX = r"\bha\s+" + _HA_BEFORE_FAMILY_TEXT
HA_REMOTE_TEXT_RE = re.compile(
    HA_TEXT_PREFIX + r"(?:add-?ons?|host|os|core|hardware|network|backups?|apps)\b", re.IGNORECASE)
# The always-refused families get the same floor whether or not remote control is on.
HA_ALWAYS_TEXT_RE = re.compile(HA_TEXT_PREFIX + r"(?:supervisor|super|su)\b", re.IGNORECASE)
# Flag values and shell syntax can obscure the exact argument boundaries. The
# always-refused families remain protected even when they follow such values.
HA_SHELL_WORD_RE = re.compile(r"\bh[\"'\\]*a\b", re.IGNORECASE)
HA_UNKNOWN_FLAG_RE = re.compile(
    r"--(?:log-level|api-token|endpoint|config)(?:=|\s+)"
    r"[^\s;]*(?:[\"'`\\]|\$\()", re.IGNORECASE)
# A word after ha whose end or value the floor cannot read: a redirection target with a quote
# or a backslash, a process substitution as the target, any word with a $ or a backtick (a
# variable that is empty or holds a blank, ${x:- a}, $(...), `...`), a glob (with nullglob, a
# glob that matches nothing is removed) or a bash brace expansion ({,} expands to nothing).
# The shell can then put the family word where the floor does not look.
HA_UNREADABLE_WORD_RE = re.compile(
    _REDIRECTION_OPERATOR + r"\s*(?:\(|[^\s;<>]*[\"'\\])|[$`*?\[]|\{[^\s{}]*(?:,|\.\.)")
# A family word that the shell builds while it runs: a $ (s${x}u, $'\163u', s$""u), a
# substitution or a glob (s[u] names /bin/su when the command runs in /bin). It is refused in the
# family position whatever it could become, in both remote-control modes. A brace word is read
# by the brace rule instead, which lists what bash makes of it.
HA_COMPUTED_WORD_RE = re.compile(r"[$`*?\[]")
# The same family position as the text floor finds it, for the built family word rule below.
# A script in bash's $'...' form with a backslash escape in it: bash -c $'ha \x73u info' runs
# ha su info, and no reading here decodes the escapes. Refused in a text with the word ha.
ANSI_C_ESCAPE_RE = re.compile(r"\$'[^']*\\")
# Not right after a dash: in ``ls -ha *`` the letters are a flag group, never the program.
_HA_WORD_TEXT_RE = re.compile(r"(?<![\w-])ha\s+", re.IGNORECASE)
# A built family word for the rule below: a $, a backtick, a glob, or a "(" (with bash's extglob
# option, @(su), +(su) and !(x) are globs too).
_HA_BUILT_WORD_RE = re.compile(r"[$`*?\[(]")
_HA_BEFORE_FAMILY_RE = re.compile(r"\s*" + _HA_BEFORE_FAMILY_TEXT, re.IGNORECASE)
_HA_NEXT_WORD_RE = re.compile(r"\S+")
# Where the shell ends a word outside quotes: a blank (a space, a tab or a newline; not a carriage
# return or a form feed) or an operator character.
_SHELL_WORD_BREAKS = " \t\n;&|<>()"
_SHELL_WORD_PIECE_RE = re.compile(r"[^ \t\n;&|<>()]+")
# Text that the plain quote reading below does not follow: ${...}, $(...), $'...' and $"..." can
# hold quotes of their own (bash and dash read some of them differently), a backtick, a comment
# and a heredoc. A text with any of them is cut at every blank and operator, quoted or not. A
# plain variable ($x, $1) holds no quote and does not count.
_BRACE_UNSURE_RE = re.compile(r"\$[{('\"]|[`#]|<<")
# A brace word with one of these is refused: bash reads a quoted "," or "}" as a plain letter,
# so {s,"x y"}u is "su" and "x yu", and the word ends where the quotes say, not at the blank.
_BRACE_WORD_QUOTING_RE = re.compile(r"[\"'`\\]")
# How deep quoted text is read again as a command (bash -c 'eval "..."'). Deeper text is
# returned whole, quotes and all, so the family check refuses it when it holds a comma.
_BRACE_QUOTE_DEPTH = 8
# A brace word with more braces than this is refused instead of expanded: each brace costs one
# more pass over the word, and no real command needs so many.
_BRACE_MAX_BRACES = 64

_ALWAYS_FAMILY_NAMES = ("supervisor", "super", "su")
_ANY_FAMILY_NAMES = _ALWAYS_FAMILY_NAMES + (
    "add-?ons?", "host", "os", "core", "hardware", "network", "backups?", "apps")
HA_ALWAYS_WORD_RE = re.compile(r"\b(?:" + "|".join(_ALWAYS_FAMILY_NAMES) + r")\b", re.IGNORECASE)
HA_ANY_FAMILY_WORD_RE = re.compile(r"\b(?:" + "|".join(_ANY_FAMILY_NAMES) + r")\b", re.IGNORECASE)


def _quote_tolerant(name: str) -> str:
    """Return a pattern for a family name with quotes or backslashes between its letters (s\\u, "s"u)."""
    letters = re.findall(r"[^?]\??", name)
    return r"[\"'`\\]*".join(letters)


# The family names as the raw text can spell them, with quotes or backslashes between letters.
_ALWAYS_SPELLED_RE = re.compile(
    r"\b(?:" + "|".join(map(_quote_tolerant, _ALWAYS_FAMILY_NAMES)) + r")\b", re.IGNORECASE)
_ANY_SPELLED_RE = re.compile(
    r"\b(?:" + "|".join(map(_quote_tolerant, _ANY_FAMILY_NAMES)) + r")\b", re.IGNORECASE)


def _last_family_word(text: str, remote_on: bool) -> int:
    """Return where the last family word that this mode refuses starts in text, or -1.

    A family word follows a position when this start is at or after it. One pass over the
    text answers that for every position, so a long text with many ha lines stays fast.
    """
    spelled = _ANY_SPELLED_RE if remote_on else _ALWAYS_SPELLED_RE
    last = -1
    for match in spelled.finditer(text):
        last = match.start()
    return last


def _brace_words(text: str, after: int, depth: int = 0) -> list[str]:
    """Return each word of the text that holds a brace bash may expand and ends after ``after``.

    The text is read from its start, so a quote opened before ``after`` is known. Plain quotes
    are followed as the shell follows them: a blank or an operator character ends a word only
    outside quotes and when it is not escaped, so ``{s,"x y"}u`` is one word. The first shell
    does not expand a brace inside quotes, but a second shell can: ``bash -c 'ha s{u,} info'``,
    ``eval ha '{s,x}u' info``, ``trap '...' EXIT`` or a variable that ``eval`` runs. So the text
    inside each pair of quotes is read again as a command of its own, and its brace words count
    too. A text with ``${``, ``$(``, ``$'``, ``$"``, a backtick, a comment or a heredoc is cut at
    every blank and operator instead, quotes or not. Each reading passes over its text once, and
    quotes nest at most ``_BRACE_QUOTE_DEPTH`` deep, so the time grows with the length only.
    """
    if _BRACE_UNSURE_RE.search(text):
        return [piece.group() for piece in _SHELL_WORD_PIECE_RE.finditer(text)
                if piece.end() > after and "{" in piece.group()]
    words = []
    word_start, quote, quote_start, braced = 0, "", 0, False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote == "'" and ch != "'":
            pass
        elif ch == "\\" and quote != "'":
            i += 1
        elif quote and ch != quote:
            pass
        elif quote:
            # The quote closes here.
            if i > after:
                words += _quoted_brace_words(text[quote_start:i + 1], depth)
            quote = ""
        elif ch in "'\"":
            quote, quote_start = ch, i
        elif ch == "{":
            braced = True
        elif ch in _SHELL_WORD_BREAKS:
            if braced and i > after:
                words.append(text[word_start:i])
            word_start, braced = i + 1, False
        i += 1
    if braced or (quote and "{" in text[word_start:]):
        # A quote still open at the end: the reading lost its place, so the rest is one word.
        words.append(text[word_start:])
    return words


def _quoted_brace_words(quoted: str, depth: int) -> list[str]:
    """Return the brace words of quoted text (its quotes included) read as a command of its own.

    In double quotes a backslash still escapes the next character, as the shell reads it, so the
    text keeps its backslashes and the family check refuses a brace word with one.
    """
    inner = quoted[1:-1]
    if "{" not in inner:
        return []
    if depth >= _BRACE_QUOTE_DEPTH:
        return [quoted]
    return _brace_words(inner, -1, depth + 1)


def _brace_word_names_family(word: str, remote_on: bool) -> bool:
    """Say whether bash can expand a word with braces into a family word that this mode refuses.

    Bash expands braces only around a comma or a sequence (``{a,b}``, ``{a..z}``) written in the
    command: a comma that a quote, a variable or a substitution makes is a plain letter. So a word
    with neither is no expansion (``{name:.su}`` in a jq filter). A word with a quote, a backslash
    or a backtick counts as a family word first, comma or not: the cut reading may have split it at
    a quoted blank, so the comma can be in the next piece. Any other word fails closed too: a
    sequence, more than ``_BRACE_MAX_BRACES`` braces, and too many choices to list all count as a
    family word. So does a word with a ``$`` whose braces do not close: the cut reading broke it
    inside ``${...}`` or ``$(...)`` (``{s,$(:)}u`` is cut to ``{s,$``), so its end is not known.
    """
    if _BRACE_WORD_QUOTING_RE.search(word):
        # Tested before the comma: the cut reading splits {-x" y",s}u at the quoted blank, so the
        # piece {-x" holds no comma, and bash still makes "su" of the whole word.
        return True
    if "," not in word and ".." not in word:
        return False
    if (".." in word
            or ("$" in word and word.count("{") != word.count("}"))
            or word.count("{") > _BRACE_MAX_BRACES):
        return True
    choices = _brace_choices(word)
    family = HA_ANY_FAMILY_WORD_RE if remote_on else HA_ALWAYS_WORD_RE
    return choices is None or any(family.search(choice) for choice in choices)


def _ha_unknown_flag(text: str, remote_on: bool) -> bool:
    """Conservatively refuse a word after ha whose end the floor cannot read.

    The text floor reads the command without quotes, so a value with a blank in it ("a b"
    or a\\ b) looks like two words there, and the family after it is not found.

    - A global-flag value with a quote, a backslash or a substitution: with remote control
      on, every such value is refused. With it off, only the remote families are allowed, so
      the value is refused when a Supervisor family word follows it.
    - A redirection target with a quote or a backslash, a process substitution, or any word
      with a ``$``, a backtick, a glob character or a brace expansion: refused when a family
      word that this mode refuses follows it (with remote control off, a Supervisor family;
      with it on, any family).
    - A word with a brace after the first ha that bash can expand into such a family word
      (``s{u,}``), or that holds a quote, a backslash or a backtick (``{s,"x y"}u``).

    ``ha`` is looked for on each line, and the family word counts anywhere after the
    unreadable word, to the end of the command text: a quoted value, ``${...}``, ``$(...)`` and
    a backtick substitution can hold a newline, and the shell then reads the family word on the
    next line as part of the same command. A family word in a later command counts too. Lines
    end at a newline only, as for the shell; a carriage return, a form feed or U+2028 does not
    end one. The text is also read with every backslash-newline pair removed, as the floor
    reads it, so a family word split across lines is found.
    """
    for reading in (text, text.replace("\\\n", "")):
        last_always = _last_family_word(reading, False)
        last_refused = _last_family_word(reading, remote_on)
        first_ha = -1
        start = 0
        while start <= len(reading):
            end = reading.find("\n", start)
            if end < 0:
                end = len(reading)
            word = HA_SHELL_WORD_RE.search(reading, start, end)
            start = end + 1
            if not word:
                continue
            if first_ha < 0:
                first_ha = word.end()
            flag = HA_UNKNOWN_FLAG_RE.search(reading, word.end(), end)
            if flag and (remote_on or last_always >= flag.end()):
                return True
            unreadable = HA_UNREADABLE_WORD_RE.search(reading, word.end(), end)
            if unreadable and last_refused >= unreadable.start():
                return True
        if first_ha >= 0:
            for brace in _brace_words(reading, first_ha):
                if _brace_word_names_family(brace, remote_on):
                    return True
    return _ha_floor_brace_word(text, remote_on)


def _ha_floor_brace_word(text: str, remote_on: bool) -> bool:
    """Read the brace words once more in the text as the floor reads it: no quotes, no backslashes.

    A second shell (``eval``, ``bash -c``) reads the text after the first shell has removed its
    quotes and backslashes. So ``eval ha s\\{u,\\} info`` and ``bash -c 'ha s{u,'"} info"`` both
    reach it as ``ha s{u,} info``, while the reading above sees an escaped brace or two quoted
    pieces that each hold a brace that does not close.
    """
    plain = re.sub(r"[\"'\\]", "", text.replace("\\\n", ""))
    first = HA_SHELL_WORD_RE.search(plain)
    if not first:
        return False
    return any(_brace_word_names_family(brace, remote_on) for brace in _brace_words(plain, first.end()))


def _ha_built_family_word(joined: str, depth: int = 0) -> bool:
    """Refuse a family word that the shell builds, after any ha word in the text.

    The parser refuses ``ha s${x}u info`` where it finds ``ha`` as the program, but it finds the
    program only behind the words it knows (``if``, ``env``, ...). Behind any other word
    (``while``, ``setsid``, ``coproc``) it does not, so this rule reads the text as the floor
    does and needs no list of words: after a word ``ha`` and its flags, flag values and
    redirections, the next word is refused when it holds a ``$``, a backtick, a glob or a ``(``
    (an extended glob). A brace word there can expand to nothing (``ha {,} s${x}u``), so the word
    after it is read too.

    Each ``$(...)`` and backtick substitution is first replaced by a word with a ``$`` (as in the
    parser), so a blank inside it does not move the family position, and its own text is read as
    a command. The text is then read four times: with continuations joined and with every
    backslash-newline pair removed, and each of those once with its quotes and backslashes
    removed (as the floor reads it) and once with each quoted or escaped blank kept inside its
    word (``--config "a b"`` is one flag value, as the shell reads it). The cost: a ``$``, a
    backtick, a glob or a ``(`` right after the word ``ha`` anywhere in the command is refused,
    even as an argument of another program (``grep ha *.yaml``).
    """
    if depth > 6:
        # Substitutions nested this deep are not read further: refuse when the word ha is there.
        return bool(_HA_WORD_TEXT_RE.search(joined))
    masked, nested = _shell_substitutions(joined)
    if any(_ha_built_family_word(item, depth + 1) for item in nested):
        return True
    for reading in (masked, masked.replace("\\\n", "")):
        unquoted = _space_redirections(re.sub(r"[\"'\\]", "", reading))
        quoted_kept = _keep_quoted_blanks(_space_redirections(reading))
        if _built_word_after_ha(unquoted) or _built_word_after_ha(quoted_kept):
            return True
    return False


def _built_word_after_ha(plain: str) -> bool:
    """Say whether a word ``ha`` in plain text is followed, past its flags, by a built family word."""
    position = 0
    while word := _HA_WORD_TEXT_RE.search(plain, position):
        position = _HA_BEFORE_FAMILY_RE.match(plain, word.end()).end()
        while family := _HA_NEXT_WORD_RE.match(plain, position):
            position = family.end()
            if family.group().startswith("-") or REDIRECTION_RE.match(family.group()):
                break
            if _HA_BUILT_WORD_RE.search(family.group()):
                return True
            if "{" not in family.group():
                break
            position = _HA_BEFORE_FAMILY_RE.match(plain, position).end()
    return False


def _keep_quoted_blanks(text: str) -> str:
    """Remove quotes and backslashes, and write each quoted or escaped blank or operator as ``_``.

    The shell keeps ``"a b"`` and ``a\\ b`` as one word. Without its quotes the text would read
    as two words, and the family position would move by one.
    """
    out, quote, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            following = text[i + 1]
            if following != "\n":
                # A backslash-newline pair is a continuation: the shell removes both.
                out.append("_" if following in _SHELL_WORD_BREAKS else following)
            i += 2
            continue
        if ch in "'\"" and quote in ("", ch):
            quote = "" if quote else ch
        elif quote and ch in _SHELL_WORD_BREAKS:
            out.append("_")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _space_redirections(text: str, whole: bool = False) -> str:
    """Put a blank on each side of every unquoted redirection operator, as the shell reads it.

    The shell ends a word at ``<`` and ``>``, so ``ha>/dev/null su`` runs ``ha su``. The word
    splitter does not, so the operators are set apart first. A quoted or escaped operator
    stays in its word, and so does the text of ``${...}``, where ``>`` is not an operator.
    A number right after an operator, even after a blank, is its target and not the file
    descriptor of the next operator: bash runs ``ha 2>& 1>x su`` as ``2>& 1``, ``>x``.

    With ``whole``, each operator is also put in single quotes, so the word splitter keeps
    ``2>&`` and ``&>`` as one word instead of ending a command at their ``&``. Only an operator
    that the shell reads as one is quoted: a quoted ``">"`` stays a plain word and cannot join
    the ``&`` after it.
    """
    out, quote, i, after_operator = [], "", 0, False
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            out.append(text[i:i + 2]); i += 2; after_operator = False; continue
        if ch == "'" and quote != '"':
            quote = "" if quote == "'" else "'"
        elif ch == '"' and quote != "'":
            quote = "" if quote == '"' else '"'
        elif not quote and text.startswith("${", i):
            end = text.find("}", i)
            end = len(text) if end < 0 else end + 1
            out.append(text[i:end]); i = end; after_operator = False; continue
        elif not quote:
            operator = REDIRECTION_TEXT_RE.match(text, i)
            if operator and not (after_operator and operator.group("fd")):
                written = f"'{operator.group(0)}'" if whole else operator.group(0)
                out.append(f" {written} ")
                i = operator.end()
                after_operator = True
                continue
        if not ch.isspace():
            after_operator = False
        out.append(ch); i += 1
    return "".join(out)


def _floor_texts(text: str, joined: str) -> tuple[str, str]:
    """Return the two readings of a command that the text floor checks, without quotes or backslashes.

    The first reading joins continuations as the shell does. The second removes every
    backslash-newline pair, in any quote or comment state. The second reading does not depend on
    where this module thinks a comment, a quote or a nested shell script starts, so an error in
    that model cannot hide a Supervisor command. The cost is a refusal of a single-quoted literal
    pair that spells one; the floor already refuses such a spelling in a comment.
    Both readings have a blank on each side of every redirection operator, as the shell reads
    ``ha>/dev/null su`` (``ha``, ``>``, ``/dev/null``, ``su``).
    """
    quote_chars = r"[\"'`\\]"
    unjoined = text.replace("\\\n", "")
    plain_joined = re.sub(quote_chars, "", joined)
    plain_unjoined = re.sub(quote_chars, "", unjoined)
    return _space_redirections(plain_joined), _space_redirections(plain_unjoined)


def _shell_words(text: str, punctuation: str) -> list[str]:
    """Split command text into words and shell punctuation, without running it.

    Text the splitter cannot read, such as an unfinished quote from an apostrophe in a
    heredoc body, is read again line by line. A line that still cannot be read is split
    with its quotes and backslashes removed; the other lines keep their quoting, so a
    quoted script given to ``sh -c`` stays one word. The shell still runs such a
    command, so it must never be skipped as unreadable. approvals.py reads words with
    the same code, in shell_words.py, but fails closed on such text instead.
    """
    try:
        return split_words(strip_comments(text), punctuation)
    except ValueError:
        pass
    words = []
    for line in text.split("\n"):
        try:
            words.extend(split_words(strip_comments(line), punctuation))
        except ValueError:
            words.extend(split_words(re.sub(r"[\"'\\]", "", line), punctuation))
        words.append("\n")
    return words


# What a substitution becomes in the masked text. It keeps a $, so the family check below sees a
# word that the shell builds while it runs (ha $(printf s)u info).
SUBSTITUTION_MARK = "$SUBSTITUTION"


def _shell_substitutions(text: str) -> tuple[str, list[str]]:
    """Mask literal shell substitutions while retaining their commands for inspection."""
    out, nested = [], []
    i, quote = 0, ''
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text) and quote != "'":
            out.append(text[i:i + 2]); i += 2; continue
        if ch == "'" and quote != '"':
            quote = '' if quote == "'" else "'"
        elif ch == '"' and quote != "'":
            quote = '' if quote == '"' else '"'
        if quote != "'" and text.startswith('$(', i):
            depth, end, inner_quote = 1, i + 2, ''
            while end < len(text) and depth:
                if text[end] == '\\' and end + 1 < len(text) and inner_quote != "'":
                    end += 2; continue
                if text[end] == "'" and inner_quote != '"':
                    inner_quote = '' if inner_quote == "'" else "'"
                elif text[end] == '"' and inner_quote != "'":
                    inner_quote = '' if inner_quote == '"' else '"'
                elif not inner_quote and text[end] == '(':
                    depth += 1
                elif not inner_quote and text[end] == ')':
                    depth -= 1
                end += 1
            if not depth:
                nested.append(text[i + 2:end - 1]); out.append(SUBSTITUTION_MARK); i = end; continue
        if quote != "'" and ch == '`':
            end = text.find('`', i + 1)
            if end >= 0:
                nested.append(text[i + 1:end]); out.append(SUBSTITUTION_MARK); i = end + 1; continue
        out.append(ch); i += 1
    return ''.join(out), nested


def _segments_of(tokens: list[str], separators: str) -> list[list[str]]:
    """Group words into simple commands at the shell's separators."""
    segments, segment = [], []
    for token in tokens:
        if token and all(ch in separators for ch in token):
            if segment:
                segments.append(segment)
                segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    return segments


def _command_segments(text: str, separators: str) -> list[list[str]]:
    """Return the simple commands of command text in two readings, and check both.

    In the first reading every ``&`` and ``|`` ends a command. In the second, an ``&`` or ``|``
    inside a redirection operator belongs to it (``ha 2>&1 su info`` is one command). The text
    alone cannot always tell which is right: bash reads ``&>file`` as one redirection, dash as
    ``&`` and then ``>file``. A check of both readings can only add refusals. A quoted or escaped
    operator (``">"``) is a plain word in both readings.
    """
    segments = []
    for whole in (False, True):
        words = _shell_words(_space_redirections(text, whole), separators)
        segments += _segments_of(words, separators)
    return segments


def _skip_redirections(segment: list[str], index: int) -> int:
    """Return the index of the first word at or after index that is not part of a redirection."""
    while index < len(segment):
        redirection = REDIRECTION_RE.match(segment[index])
        if not redirection:
            break
        # An operator alone ("2>", "<") takes the next word as its target.
        index += 1 if redirection.group(1) else 2
    return index


def _ha_family_index(segment: list[str], index: int) -> int:
    """Return the index of the family word: after ha's global flags, their values and any redirections."""
    index = _skip_redirections(segment, index)
    while index < len(segment) and segment[index].startswith("-"):
        flag = segment[index].lower()
        index = _skip_redirections(segment, index + 1)
        if flag == "--":
            break
        if flag in HA_VALUE_FLAGS and index < len(segment):
            index = _skip_redirections(segment, index + 1)
    return index


def _ha_supervisor_command(text: str, depth: int = 0) -> bool:
    """Find literal Supervisor CLI aliases without executing shell text.

    A family word that the shell builds (``ha s${x}u info``) is refused too: it could be any family.
    """
    if depth > 6:
        return False
    joined = _join_shell_continuations(text)
    if joined is None:
        return True
    masked, nested = _shell_substitutions(joined)
    if any(_ha_supervisor_command(item, depth + 1) for item in nested):
        return True
    for segment in _command_segments(masked, ';&|()\n'):
        index = 0
        while index < len(segment):
            word = segment[index].lower()
            if word == 'eval' and index + 1 < len(segment):
                if _ha_supervisor_command(' '.join(segment[index + 1:]), depth + 1):
                    return True
                break
            if REDIRECTION_RE.match(segment[index]):
                index = _skip_redirections(segment, index); continue
            if SHELL_ASSIGNMENT_RE.match(segment[index]) or word in ('if', 'then', 'do', '!', '{', 'exec', 'time', 'nohup'):
                index += 1; continue
            if word.rsplit('/', 1)[-1] in ('sudo', 'env', 'command', 'xargs', 'timeout', 'watch', 'stdbuf', 'nice'):
                word = word.rsplit('/', 1)[-1]
                index += 1
                while index < len(segment) and segment[index].startswith('-'):
                    flag = segment[index]
                    index += 1
                    takes_value = ((word == 'sudo' and flag in ('-u', '--user', '-g', '--group'))
                                   or (word == 'env' and flag in ('-u', '--unset', '-C', '--chdir'))
                                   or (word == 'xargs' and flag in ('-n', '-I', '-P', '-s'))
                                   or (word == 'watch' and flag in ('-n', '--interval'))
                                   or (word == 'timeout' and flag in ('-s', '--signal', '-k', '--kill-after'))
                                   or (word == 'nice' and flag in ('-n', '--adjustment')))
                    if takes_value and index < len(segment):
                        index += 1
                if word == 'timeout' and index < len(segment):
                    index += 1
                continue
            break
        if index >= len(segment):
            continue
        program = segment[index].lower().rsplit('/', 1)[-1]
        if program == 'find' and '-exec' in segment[index + 1:]:
            start = segment.index('-exec', index + 1) + 1
            if _ha_supervisor_command(shlex.join(segment[start:]), depth + 1):
                return True
        if program == 'busybox' and index + 1 < len(segment):
            index += 1; program = segment[index].lower()
        if program in ('sh', 'bash', 'zsh'):
            for j in range(index + 1, len(segment)):
                if segment[j].startswith('-') and 'c' in segment[j][1:]:
                    j += 1
                    if j < len(segment) and segment[j] == '--':
                        j += 1
                    if j < len(segment) and _ha_supervisor_command(segment[j], depth + 1):
                        return True
                    break
        if program in ('python', 'python3') and index + 2 < len(segment) and segment[index + 1] == '-c':
            try:
                tree = ast.parse(segment[index + 2])
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'os'
                            and node.func.attr == 'system' and node.args and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                            and _ha_supervisor_command(node.args[0].value, depth + 1)):
                        return True
            except (SyntaxError, ValueError):
                pass
        if program != 'ha' or (segment[index].lower() != 'ha' and not segment[index].lower().endswith('/ha')):
            continue
        index = _ha_family_index(segment, index + 1)
        while index < len(segment):
            if segment[index].lower() in HA_ALWAYS_FAMILIES or HA_COMPUTED_WORD_RE.search(segment[index]):
                return True
            # A brace word can expand to nothing (ha {,} su info), so the next word may be the family.
            if "{" not in segment[index]:
                break
            index = _ha_family_index(segment, index + 1)
    return False


def _ha_command(text: str, remote_on: bool, depth: int = 0) -> bool:
    """Recognize literal HA invocations in command position, including simple shell wrappers.

    This is a text check, not a shell interpreter: computed commands still depend on the sandbox.
    """
    if depth > 3:
        return False
    joined = _join_shell_continuations(text)
    if joined is None:
        return True
    for segment in _command_segments(joined, ";&|()\n"):
        index = 0
        while index < len(segment):
            word = segment[index]
            if REDIRECTION_RE.match(word):
                index = _skip_redirections(segment, index)
            elif SHELL_ASSIGNMENT_RE.match(word):
                index += 1
            elif word.lower().rsplit("/", 1)[-1] in ("env", "sudo", "command", "xargs"):
                index += 1
                while index < len(segment) and segment[index].startswith("-"):
                    index += 1
            else:
                break
        if index >= len(segment):
            continue
        program = segment[index].lower()
        if program.rsplit("/", 1)[-1] in ("sh", "bash") and index + 2 < len(segment) and segment[index + 1] == "-c":
            if _ha_command(segment[index + 2], remote_on, depth + 1):
                return True
        if program != "ha" and not (program.startswith("/") and program.endswith("/ha")):
            continue
        index = _ha_family_index(segment, index + 1)
        if index < len(segment):
            family = segment[index].lower()
            if remote_on and family in HA_REMOTE_FAMILIES:
                return True
    return False


# The image folder /usr/share/ha_codex as a command may spell it. Every "share" folder followed by an "ha_" name
# counts, globbed, braced or behind a variable, except the plain absolute /share/ha_<name> (the Home Assistant share
# folder, where the owner's repositories live). A relative share/ha_<name> stays refused: the text cannot tell
# whether the command runs in /usr. Best effort on the text: it cannot see a path a command builds at run time.
# Where a new path can start. A closing ``)`` or a backtick is not one: ``$(echo /usr)/share`` stays one word.
_IMAGE_WORD_BREAKS = re.compile(r"[\s;&|(<>=:]+")
_IMAGE_WILD = re.compile(r"[*?\[]")


def _brace_choices(word: str, limit: int = 64) -> list[str] | None:
    """Expand shell braces in one word; None when there are too many choices to list."""
    out, todo = [], [word]
    while todo:
        item = todo.pop()
        match = re.search(r"\{([^{}]*)\}", item)
        if not match:
            out.append(item)
        else:
            todo.extend(item[:match.start()] + choice + item[match.end():] for choice in match[1].split(","))
        if len(out) + len(todo) > limit:
            return None
    return out


def _part_could_be(part: str, name: str, prefix: str = "") -> bool:
    """Return whether one path part, read as a shell glob, can name ``name``."""
    part = part.lower()
    return (part == name or bool(prefix and part.startswith(prefix))
            or bool(_IMAGE_WILD.search(part) and fnmatch.fnmatchcase(name, part)))


def names_image_folder(text: str) -> bool:
    """Return whether command text can name /usr/share/ha_codex, after quotes and backslashes are removed."""
    plain = re.sub(r"/{2,}", "/", re.sub(r"[\\'\"]", "", str(text or "")))
    for word in _IMAGE_WORD_BREAKS.split(plain):
        choices = _brace_choices(word)
        if choices is None:
            # Too many choices to read: refuse any that could hold an ha_ name.
            if re.search(r"h|[*?\[]", word, re.IGNORECASE):
                return True
            continue
        for choice in choices:
            parts = re.sub(r"/{2,}", "/", choice).split("/")
            for index in range(len(parts) - 1):
                share, name = parts[index], parts[index + 1]
                if not _part_could_be(name, "ha_codex", "ha_"):
                    continue
                if "$" in share or "`" in share or ")" in share or _part_could_be(share, "share"):
                    # Allowed: only the literal absolute /share/ha_<name>.
                    if index == 1 and parts[0] == "" and share.lower() == "share":
                        continue
                    return True
    return False

def _policy_restriction(text: str) -> str:
    """Name protected app settings/rules/policy in either a patch path or command text.

    Like the approval adapter, this is a conservative literal-path check, not
    a shell interpreter. Computed/obfuscated paths still require the sandbox.
    """
    plain = re.sub(r"[\\'\"]", "", text)
    plain = re.sub(r"/{2,}", "/", plain)
    if re.search(r"/data/options\.json", plain, re.IGNORECASE):
        return "changes the add-on's settings"
    if re.search(r"schedules\.sqlite3(?:-(?:journal|wal|shm))?|/data/sched[^/\s]*[*?\[]", plain, re.IGNORECASE):
        return "accesses the protected schedule database"
    if re.search(r"worker_api_token", plain, re.IGNORECASE):
        return "accesses the add-on's private credential"
    if re.search(r"/data/+github", plain, re.IGNORECASE):
        return "accesses the add-on's GitHub sign-in"
    if re.search(r"codex-home/.*(?:AGENTS\.md|house-rules)|ha_codex/.*(?:AGENTS\.md|house-rules)", plain, re.IGNORECASE):
        return "changes the add-on's rules"
    if names_image_folder(text) or re.search(r"ha_codex/(?:policy|agents|house)|codex-home/.*(?:config\.toml|[?*\[])|/etc/codex/(?:requirements|managed_config)", plain, re.IGNORECASE):
        return "changes the add-on's permission policy"
    return ""


def _python_definition(path: str) -> str:
    match = re.search(r"(?:^|[/\s'\"])(python_scripts|pyscript)/", path)
    return ("python_script" if match[1] == "python_scripts" else "pyscript") if match else ""


def check_tool_call(event: str, payload: Any, remote_on: bool, rules: dict[str, Any]) -> str:
    """Return why the hook refuses this tool call or permission request, or an empty string to let it through.

    ``event`` is the Codex hook event (``PreToolUse`` or ``PermissionRequest``);
    ``payload`` is the hook's input. Nothing here ever asks: the answer is a
    refusal or no opinion, and with no opinion the usual approval flow runs.
    """
    data = payload if isinstance(payload, dict) else {}
    event = str(data.get("hook_event_name") or event or "")
    tool_input = data.get("tool_input", data.get("input", data))
    if remote_on and (event == "PermissionRequest" or asks_for_escalation(tool_input)):
        if not isinstance(tool_input, dict) or not any(
                tool_input.get(key) for key in ("command", "patch", "path", "file_path", "changes")):
            return "This permission request does not name an action the command check can review; it is refused."
    # An ordinary PermissionRequest is not an escalation to full access. The
    # unchanged hook returns exit 0 with no output, never an allow decision.
    if remote_on:
        rules = {**rules, "runs_on_load_keys": sorted(set(rules["runs_on_load_keys"]) | {"template"})}
    deadline = time.monotonic() + 2.0
    texts = _strings(tool_input)
    if len(texts) > 128 or sum(len(text) for text in texts) > 1024 * 1024:
        return "This tool input exceeds the command check's size budget; it is refused."
    for text in texts:
        if time.monotonic() > deadline:
            return "This tool input exceeds the command check's time budget; it is refused."
        if remote_on:
            if FULL_ACCESS_RE.search(text):
                return "This request enables full access, which is never allowed."
            restriction = _policy_restriction(text)
            if restriction:
                return f"This request {restriction}, which is never allowed."
            definition = _python_definition(text)
            if definition:
                return f"This request names a {definition} definition file, which Codex may not write or edit."
        if PATCH_BEGIN in text:
            if len(text.encode("utf-8")) > PATCH_MAX_BYTES:
                return "This patch exceeds the command check's size budget; it is refused."
            try:
                scripts = config_refused_scripts(rules)
                for path, added in parse_patch(text):
                    if time.monotonic() > deadline:
                        raise ValueError("command check time budget exceeded")
                    for pattern, why in PROTECTED_PATH_RES:
                        if pattern.search(path):
                            return f"This edit {why}, which is never allowed."
                    if path.lower().endswith(YAML_SUFFIXES):
                        found = find_refused(added, rules, scripts)
                        if found:
                            return f"This edit adds {found[0].reason}. A person must add this change outside Codex."
                if remote_on:
                    for path, before, after in patch_versions(text, str(data.get("cwd") or "")):
                        if time.monotonic() > deadline:
                            raise ValueError("command check time budget exceeded")
                        found = new_refused(before, after, rules, scripts)
                        if time.monotonic() > deadline:
                            raise ValueError("command check time budget exceeded")
                        if found:
                            reason = found[0].reason
                            if reason == BLOCK_CHANGED:
                                body = find_refused(after, rules, scripts)
                                reason = next((item.reason for item in body if item.kind in {"runs_on_load", "template"}), reason)
                            return f"This edit to {path} makes {reason}. A person must add this change outside Codex."
            except (OSError, ValueError, UnicodeError):
                return "This edit cannot be checked safely against the complete configuration file; it is refused."
            # The rest of a command that carries a patch (a heredoc) is still a command.
            text = PATCH_BLOCK_RE.sub(" ", text)
        if remote_on:
            definition = re.search(r"\b(" + "|".join(REMOTE_DEFINITIONS) + r")\s*:", text)
            if definition:
                return f"This command names a {definition[1]} definition, which Codex may not write or edit."
        for pattern, why in PROTECTED_COMMAND_RES:
            if pattern.search(text):
                return f"This command {why}, which is never allowed."
        # Join literal words before the conservative quote/backslash floor.
        joined = _join_shell_continuations(text)
        if joined is None:
            return ("This command may hide a Home Assistant Supervisor command in a shell line continuation. "
                    "Write the command on one line so it can be checked.")
        floor = _floor_texts(text, joined)
        if (_ha_supervisor_command(text)
                or any(HA_ALWAYS_TEXT_RE.search(plain) for plain in floor)
                or _ha_unknown_flag(text, remote_on)
                or _ha_built_family_word(joined)
                or (HA_SHELL_WORD_RE.search(text) and ANSI_C_ESCAPE_RE.search(text))
                or (remote_on and any(HA_REMOTE_TEXT_RE.search(plain) for plain in floor))
                or _ha_command(text, remote_on)):
            return "This command talks to the Home Assistant Supervisor, which is never allowed."
    return ""
