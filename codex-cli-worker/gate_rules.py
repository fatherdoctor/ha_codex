"""The rules of the remote-control command check, shared by the Codex hook and the worker's config watch.

Commands from the ChatGPT app reach Codex through OpenAI's relay and skip this
app's Allow and Deny buttons. The check below holds outside the model:

- ``check_tool_call`` is what the managed Codex hook (``gate/gate_hook.py``)
  runs for every tool call and every permission request. It refuses, and never
  asks, any command that needs more than the sandbox while remote control is
  on, and always refuses reads of process environments, the Supervisor token,
  Home Assistant's sign-in store and ``secrets.yaml``, and file edits that add
  an action on a refused domain or a templated action or entity.
- ``find_refused`` is the scan both sides use on YAML text. The worker runs it
  over ``/config`` to catch edits the hook cannot see (a shell command that
  writes a file) and raises a Home Assistant repair for them.
- ``refused_blocks`` hashes each top-level YAML block that holds a refused
  line, so a changed trigger or condition of such a block is seen even though
  its refused line stays as it was.

The refused domains live in one file, ``policy/refused-domains.json``
(``/usr/share/ha_codex/refused-domains.json`` in the image). When that file is
missing the built-in list below is used; it is never looser than the file.

The scan reads lines, not parsed YAML, so it also works on half an edit. It
can miss an obfuscated reference; that is why commands that leave the sandbox
are refused outright while remote control is on.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IMAGE_DIR = Path("/usr/share/ha_codex")
SOURCE_DIR = Path(__file__).resolve().parent / "policy"
OPTIONS_PATH = Path("/data/options.json")


def category_refusals_enabled(path: Path | None = None) -> bool:
    """Only an explicit JSON true opts into category refusals; security checks are separate."""
    try:
        data = json.loads((path or OPTIONS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("refuse_categories") is True

STRICT_RULES: dict[str, Any] = {
    "refused_domains": ["alarm_control_panel", "alarmo", "camera", "cover", "lock", "notify", "tts"],
    "reaches_anything": ["hassio", "pyscript", "python_script", "rest_command", "shell_command"],
    "message_script_words": ["alert", "announce", "call", "message", "notification", "notify", "sms", "text"],
    "runs_on_load_platforms": ["command_line", "rest", "scrape"],
    "runs_on_load_keys": ["command_line", "rest", "rest_command", "scrape", "shell_command"],
    "device_actions": {
        "alarm_control_panel": ["arm_away", "arm_custom_bypass", "arm_home", "arm_night", "arm_vacation",
                                "disarm", "trigger"],
        "camera": ["turn_off", "turn_on"],
        "cover": ["close", "close_tilt", "open", "open_tilt", "set_position", "set_tilt_position", "stop",
                  "stop_tilt"],
        "lock": ["lock", "open", "unlock"],
        "mobile_app": ["notify"],
    },
}
LIST_KEYS = ("refused_domains", "reaches_anything", "message_script_words",
             "runs_on_load_platforms", "runs_on_load_keys")

# Lines around a device action's `domain:` that are searched for its `type:`.
DEVICE_WINDOW = 4

ACTION_RE = re.compile(r"""^\s*-?\s*(?:service|action)\s*:\s*["']?([a-z0-9_]+)\.([a-z0-9_]+)""")
TEMPLATE_ACTION_RE = re.compile(r"""^\s*-?\s*(?:(?:service|action)\s*:.*(?:\{\{|\{%)|service_template\s*:)""")
TEMPLATE_TARGET_RE = re.compile(r"""^\s*-?\s*(?:entity_id|device_id|area_id)\s*:.*(?:\{\{|\{%)""")
DEVICE_DOMAIN_RE = re.compile(r"""^\s*-?\s*domain\s*:\s*["']?([a-z0-9_]+)["']?\s*$""")
DEVICE_TYPE_RE = re.compile(r"""^\s*-?\s*type\s*:\s*["']?([a-z0-9_]+)["']?\s*$""")
# A scene's (or scene.apply's) entity state: `lock.front_door:` as a key under `entities:`.
STATE_KEY_RE = re.compile(r"""^\s*-?\s*["']?([a-z0-9_]+)\.[a-z0-9_]+["']?\s*:""")
PLATFORM_RE = re.compile(r"""^\s*-?\s*platform\s*:\s*["']?([a-z0-9_]+)["']?\s*(?:#.*)?$""")
# A definition key whose value is a block or an include: `shell_command:`, `  rest: !include rest.yaml`.
DEFINITION_KEY_RE = re.compile(r"""^\s*-?\s*([a-z0-9_]+)\s*:\s*(?:!include\S*\s+\S+\s*)?(?:#.*)?$""")
SCRIPT_REF_RE = re.compile(r"""\bscript\.([a-z0-9_]+)\b""")
# The script domain's own services are not scripts.
SCRIPT_SERVICES = {"turn_on", "turn_off", "toggle", "reload"}

# Reads refused from every client: process environments (the worker's holds the Supervisor token),
# the Supervisor itself, Home Assistant's sign-in store (its signing key) and secrets.yaml.
PROTECTED_COMMAND_RES = (
    (re.compile(r"/proc/\S*environ"), "reads a process environment, which can hold the Supervisor token"),
    (re.compile(r"\b(?:SUPERVISOR_TOKEN|HASSIO_TOKEN)\b"), "uses the Supervisor token"),
    (re.compile(r"/run/s6/container_environment"), "reads the app's saved environment, which holds the Supervisor token"),
    (re.compile(r"https?://supervisor\b|\bsupervisor/|\bha\s+(?:supervisor|su)\b|\bbashio::", re.IGNORECASE),
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

# Keys a tool call may use to ask for more than the sandbox (provisional until the lab captures 0.166's format).
ESCALATION_KEYS = ("sandbox_permissions", "with_escalated_permissions", "escalated", "require_escalated")
ESCALATION_VALUES = {"require_escalated", "escalated", "true", "danger-full-access"}


@dataclass(frozen=True)
class Finding:
    """One refused reference in a text: the 1-based line, why it is refused, and the line as written.

    ``kind`` says what was found, for callers that group findings: the refused
    domain (``lock``, ``cover``, ``shell_command``, ``mobile_app`` ...),
    ``message_script``, ``refused_script``, ``runs_on_load`` or ``template``.
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
    devices = data.get("device_actions")
    if isinstance(devices, dict):
        for domain, types in devices.items():
            if isinstance(types, list):
                rules["device_actions"][str(domain)] = sorted(
                    set(rules["device_actions"].get(str(domain), [])) | {str(item) for item in types})
    return rules


def _message_script_re(rules: dict[str, Any]) -> re.Pattern[str]:
    words = "|".join(re.escape(word) for word in rules["message_script_words"]) or "notify"
    return re.compile(r"^[a-z0-9_]*(?:" + words + r")[a-z0-9_]*$")


def find_refused(text: str, rules: dict[str, Any], refused_scripts: frozenset[str] | set[str] = frozenset()) -> list[Finding]:
    """Return every line of this YAML text that the check refuses.

    The scan reads lines, not parsed YAML. Refused:

    - an action or service call on a refused domain (locks, alarms, every
      cover, cameras, notifications, spoken announcements) or on a domain
      that can reach anything (shell, REST, Python, add-on input);
    - a call to a script whose name says it sends a message, or to a script in
      ``refused_scripts`` (one whose own steps are refused);
    - a state for an entity on a refused domain written as a key, which is how
      a scene (or ``scene.apply``) sets a lock, an alarm or a cover;
    - a ``command_line``, ``rest`` or ``scrape`` platform, and a
      ``shell_command``, ``rest_command``, ``command_line``, ``rest`` or
      ``scrape`` definition, which run on their own once loaded;
    - a device action on a refused domain;
    - any templated action, entity, device or area.
    """
    lines = text.splitlines()
    refused = set(rules["refused_domains"])
    anything = set(rules["reaches_anything"])
    platforms = set(rules["runs_on_load_platforms"])
    definitions = set(rules["runs_on_load_keys"])
    messages = _message_script_re(rules)
    devices = rules["device_actions"]
    findings: list[Finding] = []
    for index, line in enumerate(lines):
        number = index + 1
        match = ACTION_RE.match(line)
        if match:
            domain, service = match.groups()
            if domain in refused:
                findings.append(Finding(number, f"an action on {domain}", line, domain))
            elif domain in anything:
                findings.append(Finding(number, f"an action on {domain}, which can reach anything", line, domain))
            elif domain == "script" and service not in SCRIPT_SERVICES and messages.match(service):
                findings.append(Finding(number, f"a call to script.{service}, whose name says it sends a message", line,
                                        "message_script"))
            elif domain == "script" and service not in SCRIPT_SERVICES and service in refused_scripts:
                findings.append(Finding(number, f"a call to script.{service}, which itself acts on a refused domain", line,
                                        "refused_script"))
        else:
            state = STATE_KEY_RE.match(line)
            if state and state.group(1) in refused:
                findings.append(Finding(number, f"a state for {state.group(1)}, which a scene can set", line, state.group(1)))
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
        elif TEMPLATE_TARGET_RE.match(line):
            findings.append(Finding(number, "a templated entity, device or area", line, "template"))
        device = DEVICE_DOMAIN_RE.match(line)
        if device and device.group(1) in devices:
            allowed = set(devices[device.group(1)])
            for near in lines[max(0, index - DEVICE_WINDOW):index + DEVICE_WINDOW + 1]:
                kind = DEVICE_TYPE_RE.match(near)
                if kind and kind.group(1) in allowed:
                    findings.append(Finding(number, f"a device action on {device.group(1)} ({kind.group(1)})", line,
                                            device.group(1)))
                    break
        if not match:
            for ref in SCRIPT_REF_RE.findall(line):
                if ref in SCRIPT_SERVICES or not re.match(r"^\s*-?\s*(?:entity_id\s*:\s*)?[\"']?script\.", line):
                    continue
                if messages.match(ref):
                    findings.append(Finding(number, f"a call to script.{ref}, whose name says it sends a message", line,
                                            "message_script"))
                    break
                if ref in refused_scripts:
                    findings.append(Finding(number, f"a call to script.{ref}, which itself acts on a refused domain", line,
                                            "refused_script"))
                    break
    return findings


BLOCK_CHANGED = "a change to a block that holds a refused action"


def active_findings(text: str, rules: dict[str, Any], scripts: set[str] | frozenset[str] = frozenset(),
                    categories: bool | None = None) -> list[Finding]:
    """Always keep Core escape paths; device, message and indirect categories are opt-in."""
    enabled = category_refusals_enabled() if categories is None else categories
    found = find_refused(text, rules, scripts)
    security = set(rules["reaches_anything"]) | {"runs_on_load", "template"}
    return found if enabled else [finding for finding in found if finding.kind in security]


def refused_blocks(text: str, rules: dict[str, Any], scripts: set[str] | frozenset[str] = frozenset(),
                   categories: bool = True) -> dict[str, tuple[int, int, str]]:
    """Return {hash: (first line number, last line number, first line)} for each top-level block of this YAML text that holds a refused line.

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
            if body and active_findings(body, rules, scripts, categories):
                out[hashlib.sha256(body.encode("utf-8")).hexdigest()] = (start, number - 1, block[0])
            block, start = [], number
        block.append(line)
    return out


def new_refused(before: str, after: str, rules: dict[str, Any], scripts: set[str] | frozenset[str] = frozenset(),
                categories: bool | None = None) -> list[Finding]:
    """Find newly added refused lines and changed blocks, without treating moved lines as new."""
    enabled = category_refusals_enabled() if categories is None else categories
    counts = Counter(item.key() for item in active_findings(before, rules, scripts, enabled))
    found = []
    for item in active_findings(after, rules, scripts, enabled):
        if counts[item.key()]:
            counts[item.key()] -= 1
        else:
            found.append(item)
    previous = refused_blocks(before, rules, scripts, enabled)
    for digest, (first, last, text) in refused_blocks(after, rules, scripts, enabled).items():
        if digest not in previous and not any(first <= item.line <= last for item in found):
            found.append(Finding(first, BLOCK_CHANGED, text))
    return found


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


def check_tool_call(event: str, payload: Any, remote_on: bool, rules: dict[str, Any]) -> str:
    """Return why the hook refuses this tool call or permission request, or an empty string to let it through.

    ``event`` is the Codex hook event (``PreToolUse`` or ``PermissionRequest``);
    ``payload`` is the hook's input. Nothing here ever asks: the answer is a
    refusal or no opinion, and with no opinion the usual approval flow runs.
    """
    data = payload if isinstance(payload, dict) else {}
    event = str(data.get("hook_event_name") or event or "")
    tool_input = data.get("tool_input", data.get("input", data))
    if remote_on and event == "PermissionRequest":
        return ("Remote control is on, so commands that need more than the sandbox are refused. "
                "Turn remote control off in the app's Settings to approve this from the chat page.")
    if remote_on and asks_for_escalation(tool_input):
        return ("Remote control is on, so commands that need more than the sandbox are refused, "
                "from the ChatGPT app and from the chat page alike.")
    texts = _strings(tool_input)
    for text in texts:
        if PATCH_BEGIN in text:
            for path, added in parse_patch(text):
                for pattern, why in PROTECTED_PATH_RES:
                    if pattern.search(path):
                        return f"This edit {why}, which is never allowed."
                if path.lower().endswith(YAML_SUFFIXES):
                    found = active_findings(added, rules)
                    if found:
                        first = found[0]
                        return (f"This edit to {path} adds {first.reason} ({first.text.strip()}). "
                                "A person must add this change outside Codex.")
            # The rest of a command that carries a patch (a heredoc) is still a command.
            text = PATCH_BLOCK_RE.sub(" ", text)
        for pattern, why in PROTECTED_COMMAND_RES:
            if pattern.search(text):
                return f"This command {why}, which is never allowed."
    return ""
