"""Actions this app refuses whatever Codex or the user says, and the plain-words text for every request.

Codex asks before it runs a command outside its sandbox or changes a file. Before
that request reaches the user, the worker reads the action itself (never the
model's description of it) and checks it against a fixed list of categories:

- locks, the alarm, garage doors and other covers, and cameras;
- anything that sends a message to a person (notifications, text to speech,
  mail, chat and SMS services);
- publishing: a push, release, pull request or package upload to a public place;
- this app's own settings, permission policy and house rules;
- shell, REST and Python commands and templated actions added to the configuration;
- a script, scene or automation whose own definition does one of the above.

The refused domains and the configuration scan are shared with the
remote-control check: ``gate_rules.find_refused`` over
``policy/refused-domains.json``.

A request in one of them is refused at once: Codex gets "decline", nothing runs,
and the chat shows one plain sentence saying why. Lights, switches and scenes are
the device actions this app is for. The check reads text, so it errs toward
refusing: a lock named in a command that only reads it is refused too, and Codex
is told to read state through its verification tool instead.

Every request also gets a confirmation in three short lines: what changes, where,
and the effect, so the person deciding does not have to read a shell command or a
diff to know what they are allowing.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import gate_rules

CONFIG_ROOT = Path("/config")
# Refused lines an exchange left in the configuration that the worker could not undo (see record_refused_edits).
REFUSED_EDITS_PATH = Path("/data/refused-edits.json")

_RULES = gate_rules.load_rules()

# Home Assistant domains whose actions are never allowed, by category. Every domain of the shared list has one.
DENIED_DOMAINS: dict[str, tuple[str, ...]] = {
    "locks": ("lock",),
    "alarm": ("alarm_control_panel", "alarmo"),
    "garage": ("cover",),
    "cameras": ("camera",),
    "messages": ("notify", "tts"),
}

# The sentence the chat shows for each category, and that Codex receives as the reason.
REFUSALS: dict[str, str] = {
    "locks": "This app never controls locks. Use the lock's own app or the Home Assistant lock card.",
    "alarm": "This app never arms, disarms or changes the alarm. Use the alarm panel.",
    "garage": "This app never opens or closes a garage door, gate, blind or other cover. Use its own control.",
    "cameras": "This app never views, records or changes cameras.",
    "messages": "This app never sends a message, notification or announcement to anyone.",
    "publishing": "This app never pushes, releases or publishes anything to a public place.",
    "policy": "This app's settings, permission policy and house rules cannot be changed from a conversation.",
    "commands": ("This app never adds or runs shell, REST or Python commands or templated actions in Home Assistant; "
                 "a person adds those."),
    "indirect": ("This script, scene or automation itself controls a lock, the alarm, a cover or a camera, or sends a "
                 "message, so this app never runs it. Run it from Home Assistant."),
    "planted": ("A configuration change from Codex that adds a refused action is still in place, so this app does not "
                "reload or restart Home Assistant. Remove it, or reload from Home Assistant yourself."),
}

ALLOWED_DEVICE_DOMAINS = ("light", "switch", "scene")

_DOMAIN_TO_CATEGORY = {domain: category for category, domains in DENIED_DOMAINS.items() for domain in domains}
for _domain in _RULES["refused_domains"]:
    _DOMAIN_TO_CATEGORY.setdefault(_domain, "commands")
for _domain in _RULES["reaches_anything"]:
    _DOMAIN_TO_CATEGORY.setdefault(_domain, "commands")
# What a configuration finding (gate_rules.Finding.kind) is refused as, beyond the domains above.
_KIND_TO_CATEGORY = {"mobile_app": "messages", "message_script": "messages", "refused_script": "indirect",
                     "runs_on_load": "commands", "template": "commands"}
_DENIED = "|".join(sorted((re.escape(d) for d in _DOMAIN_TO_CATEGORY), key=len, reverse=True))

# A service call over REST (/api/services/<domain>/<service>) or the websocket ("domain": "<domain>").
_SERVICE_PATH = re.compile(r"/api/services/([a-z0-9_]+)/([a-z0-9_]+)")
_WS_DOMAIN = re.compile(r"""["']domain["']\s*:\s*["']([a-z0-9_]+)["']""")
# An entity id or a service name of a refused domain: lock.front_door, notify.mobile_app_phone. File names such
# as lock.json or camera.py are not entity ids.
_FILE_SUFFIX = r"(?:py|json|ya?ml|txt|log|md|sh|js|css|html|db|lock)\b"
_ENTITY = re.compile(rf"(?<![\w.-])({_DENIED})\.(?!{_FILE_SUFFIX})([a-z0-9_]+)\b")
# The camera snapshot and stream endpoints.
_CAMERA_API = re.compile(r"/api/camera_proxy(?:_stream)?/")
# A script whose name says it messages someone (script.notify_family, script.announce_dinner).
_MESSAGE_SCRIPT_NAME = (r"script\.[a-z0-9_]*(?:" + "|".join(re.escape(w) for w in _RULES["message_script_words"])
                        + r")[a-z0-9_]*")
_MESSAGE_SCRIPT = re.compile(r"\b" + _MESSAGE_SCRIPT_NAME)
# Mail, chat and SMS services reached directly.
_MESSAGE_HOSTS = re.compile(
    r"\b(?:sendmail|mailx?|mutt|msmtp|ssmtp|swaks)\b|smtps?://|api\.twilio\.com|api\.pushover\.net|ntfy\.sh"
    r"|hooks\.slack\.com|discord(?:app)?\.com/api/webhooks|api\.telegram\.org|graph\.facebook\.com|api\.pushbullet\.com")
# Publishing: git push, the GitHub CLI's writing commands, package and image uploads, and writes to GitHub's API.
_PUBLISH = re.compile(
    r"\bgit\b[^\n;&|]*\bpush\b"
    r"|\bgh\s+(?:release|pr|issue|gist|repo)\s+(?:create|upload|edit|comment|merge|fork|delete|close|reopen)\b"
    r"|\bgh\s+api\b[^\n;&|]*(?:-X\s*|--method\s+)(?:POST|PUT|PATCH|DELETE)\b"
    r"|\bnpm\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b|\bpoetry\s+publish\b|\bcargo\s+publish\b"
    r"|uploads\.github\.com"
    r"|api\.github\.com[^\n;&|]*(?:-X\s*|--request\s+)(?:POST|PUT|PATCH|DELETE)\b"
    r"|(?:-X\s*|--request\s+)(?:POST|PUT|PATCH|DELETE)\b[^\n;&|]*api\.github\.com",
    re.IGNORECASE)
# This app's settings and policy: the Supervisor's options endpoint for this app, its CLI, and the protected files.
_POLICY = re.compile(
    r"/addons/self/options|/addons/[a-z0-9_]+/options|\bha\s+(?:apps?|addons?)\s+options\b|bashio::addon\.option"
    r"|/usr/share/ha_codex|/data/options\.json|/data/codex-home/(?:AGENTS\.md|config\.toml)|/data/worker_api_token")


def _categories_in_command(command: str) -> list[str]:
    """Return the refused categories a command's text reaches, in a fixed order."""
    found: set[str] = set()
    text = command or ""
    lower = text.lower()
    for domain, _service in _SERVICE_PATH.findall(lower):
        if domain in _DOMAIN_TO_CATEGORY:
            found.add(_DOMAIN_TO_CATEGORY[domain])
    for domain in _WS_DOMAIN.findall(lower):
        if domain in _DOMAIN_TO_CATEGORY:
            found.add(_DOMAIN_TO_CATEGORY[domain])
    for domain, _rest in _ENTITY.findall(lower):
        found.add(_DOMAIN_TO_CATEGORY[domain])
    if _CAMERA_API.search(lower):
        found.add("cameras")
    if _MESSAGE_SCRIPT.search(lower) or _MESSAGE_HOSTS.search(lower):
        found.add("messages")
    if _PUBLISH.search(text):
        found.add("publishing")
    if _POLICY.search(text):
        found.add("policy")
    if _refused_references(lower):
        found.add("indirect")
    if _RELOAD_OR_RESTART.search(lower) and standing_refused_edits():
        found.add("planted")
    return [category for category in REFUSALS if category in found]


def _added_lines(diff: str) -> str:
    """Return the lines a unified diff adds (or the whole text when it is not a diff, as for a new file)."""
    lines = (diff or "").splitlines()
    if not any(line.startswith(("+", "-", "@@")) for line in lines):
        return "\n".join(lines)
    return "\n".join(line[1:] for line in lines if line.startswith("+") and not line.startswith("+++"))


def finding_category(finding: gate_rules.Finding) -> str:
    """Return the refused category of one configuration finding."""
    return _DOMAIN_TO_CATEGORY.get(finding.kind) or _KIND_TO_CATEGORY.get(finding.kind) or "commands"


def _categories_in_changes(changes: list[dict[str, Any]]) -> list[str]:
    """Return the refused categories a file change would put into the configuration."""
    found: set[str] = set()
    definitions = refused_definitions()
    scripts = {entity.split(".", 1)[1] for entity in definitions if entity.startswith("script.")}
    for change in changes:
        added = _added_lines(str(change.get("diff") or "")).lower()
        for finding in gate_rules.find_refused(added, _RULES, scripts):
            found.add(finding_category(finding))
        if _refused_references(added, definitions):
            found.add("indirect")
        for path in (change.get("path"), change.get("move_path")):
            if path and _POLICY.search(str(path)):
                found.add("policy")
    return [category for category in REFUSALS if category in found]


# --- Scripts, scenes and automations that act under their own name (F11), and planted edits (F9) ----------------

_REFERENCE = re.compile(r"(?<![\w.-])(script|scene|automation)\.([a-z0-9_]+)\b")
_SERVICE_REFERENCE = re.compile(r"/api/services/script/([a-z0-9_]+)")
_OWN_SERVICES = {"turn_on", "turn_off", "toggle", "reload", "trigger", "apply", "create", "delete"}
_DEFINITION_FILES = (("script", "scripts.yaml"), ("scene", "scenes.yaml"), ("automation", "automations.yaml"))
_NAME_LINE = re.compile(r"""^(?:-\s+|\s+)(id|alias|name)\s*:\s*["']?(.*?)["']?\s*$""")
_TOP_KEY = re.compile(r"^([a-z0-9_]+)\s*:")
# A reload or restart: what makes a planted edit take effect.
_RELOAD_OR_RESTART = re.compile(
    r"/api/services/(?:[a-z0-9_]+/reload\w*|homeassistant/(?:restart|reload_all|reload_core_config|reload_config_entry))"
    r"|\bha\s+core\s+(?:restart|rebuild)\b|/core/restart\b")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _definition_blocks(text: str) -> list[list[str]]:
    """Split a scripts, scenes or automations file into its top-level entries."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line[:1] and not line[:1].isspace() and not line.startswith("#"):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return blocks


def _entity_ids_of(domain: str, block: list[str]) -> set[str]:
    """Return the entity ids an entry may have: a script by its key; a scene or automation by id, name or alias."""
    if domain == "script":
        key = _TOP_KEY.match(block[0])
        return {f"script.{key.group(1)}"} if key else set()
    ids: set[str] = set()
    for line in block:
        match = _NAME_LINE.match(line)
        if match and match.group(2) and len(line) - len(line.lstrip(" -")) <= 2:
            ids.add(f"{domain}.{_slug(match.group(2))}")
    return ids


def refused_definitions(config_root: Path | None = None) -> dict[str, str]:
    """Return {entity id: why} for each script, scene and automation whose own definition holds a refused line.

    Read from scripts.yaml, scenes.yaml (scenes made in the UI are there too) and
    automations.yaml. A script that calls a refused script is refused too.
    """
    root = config_root or CONFIG_ROOT
    entries: list[tuple[str, set[str], str]] = []
    for domain, name in _DEFINITION_FILES:
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in _definition_blocks(text):
            ids = _entity_ids_of(domain, block)
            if ids:
                entries.append((domain, ids, "\n".join(block)))
    refused: dict[str, str] = {}
    for _ in range(5):
        scripts = {entity.split(".", 1)[1] for entity in refused if entity.startswith("script.")}
        before = len(refused)
        for _domain, ids, body in entries:
            if ids & refused.keys():
                continue
            found = gate_rules.find_refused(body, _RULES, scripts)
            if found:
                for entity in ids:
                    refused[entity] = found[0].reason
        if len(refused) == before:
            break
    return refused


def _refused_references(text: str, definitions: dict[str, str] | None = None) -> list[str]:
    """Return the scripts, scenes and automations this text names whose definitions are refused."""
    names = [f"{domain}.{rest}" for domain, rest in _REFERENCE.findall(text) if rest not in _OWN_SERVICES]
    names += [f"script.{rest}" for rest in _SERVICE_REFERENCE.findall(text) if rest not in _OWN_SERVICES]
    if not names:
        return []
    definitions = refused_definitions() if definitions is None else definitions
    return [name for name in dict.fromkeys(names) if name in definitions]


def new_refused_lines(earlier: str, after: str, scripts: set[str] | frozenset[str] = frozenset()) -> list[gate_rules.Finding]:
    """Return the refused lines in ``after`` that ``earlier`` did not have (a line that only moved is not new)."""
    left = Counter(finding.key() for finding in gate_rules.find_refused(earlier, _RULES, scripts))
    out: list[gate_rules.Finding] = []
    for finding in gate_rules.find_refused(after, _RULES, scripts):
        if left[finding.key()] > 0:
            left[finding.key()] -= 1
        else:
            out.append(finding)
    return out


def _read_refused_edits() -> list[dict[str, Any]]:
    try:
        data = json.loads(REFUSED_EDITS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_refused_edits(items: list[dict[str, Any]]) -> None:
    if not items:
        try:
            REFUSED_EDITS_PATH.unlink()
        except FileNotFoundError:
            pass
        return
    REFUSED_EDITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REFUSED_EDITS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    os.replace(tmp, REFUSED_EDITS_PATH)


def record_refused_edits(items: list[dict[str, Any]]) -> None:
    """Remember refused lines an exchange left in place: {"file", "line", "reason", "text"} under /config."""
    known = {(item.get("file"), item.get("text")) for item in _read_refused_edits()}
    merged = _read_refused_edits() + [item for item in items if (item.get("file"), item.get("text")) not in known]
    _write_refused_edits(merged)


def standing_refused_edits(config_root: Path | None = None) -> list[dict[str, Any]]:
    """Return the remembered refused lines that are still in their files, and forget the ones that are gone."""
    root = config_root or CONFIG_ROOT
    items = _read_refused_edits()
    if not items:
        return []
    standing = []
    for item in items:
        rel = str(item.get("file") or "")
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            continue
        try:
            lines = {line.strip() for line in (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()}
        except OSError:
            continue
        if str(item.get("text") or "").strip() in lines:
            standing.append(item)
    if len(standing) != len(items):
        _write_refused_edits(standing)
    return standing


def refused_categories(kind: str, *, command: str = "", changes: list[dict[str, Any]] | None = None) -> list[str]:
    """Return every refused category this request reaches; empty when the request may go to the user."""
    if kind == "command":
        return _categories_in_command(command)
    return _categories_in_changes(changes or [])


def refusal_text(categories: list[str]) -> str:
    """Return the plain sentence (one per category) the chat shows for a refused request."""
    return " ".join(REFUSALS[category] for category in categories if category in REFUSALS)


# --- The confirmation: what changes, where, the effect ------------------------------------------------------------

_SERVICE_WORDS = {"turn_on": "turns on", "turn_off": "turns off", "toggle": "toggles"}
_RELOADS = {
    ("automation", "reload"): ("Reload the automations.", "Automations read their configuration again; none is run by this step."),
    ("script", "reload"): ("Reload the scripts.", "Scripts read their configuration again; none is run by this step."),
    ("scene", "reload"): ("Reload the scenes.", "Scenes read their configuration again; none is applied by this step."),
    ("homeassistant", "restart"): ("Restart Home Assistant.", "Home Assistant is unavailable for a minute or two."),
}

_FILE_EFFECTS = (
    (re.compile(r"(^|/)automations\.yaml$"), "Your automations change once automations are reloaded."),
    (re.compile(r"(^|/)scripts\.yaml$"), "Your scripts change once scripts are reloaded."),
    (re.compile(r"(^|/)scenes\.yaml$"), "Your scenes change once scenes are reloaded."),
    (re.compile(r"(^|/)\.storage/lovelace"), "A dashboard changes; open it to see the change."),
    (re.compile(r"(^|/)(configuration\.yaml|packages/.+\.ya?ml)$"),
     "Home Assistant's configuration changes; most parts take effect after a reload or a restart."),
    (re.compile(r"\.ya?ml$"), "A configuration file changes; it takes effect when Home Assistant next reads it."),
)

_KIND_WORDS = {"add": "Create", "update": "Change", "delete": "Delete"}


_NAMED_DOMAINS = ALLOWED_DEVICE_DOMAINS + tuple(_DOMAIN_TO_CATEGORY) + (
    "automation", "script", "input_boolean", "input_number", "input_select", "input_text", "input_button",
    "sensor", "binary_sensor", "climate", "fan", "media_player", "timer", "vacuum", "humidifier", "number", "select")
_ENTITY_ID = re.compile(r"(?<![\w.-])(?:" + "|".join(_NAMED_DOMAINS) + r")\.[a-z0-9_]+\b")


def _entity_ids(text: str) -> list[str]:
    """Return the entity ids a command names, in order, without repeats."""
    return list(dict.fromkeys(_ENTITY_ID.findall(text)))


def _command_confirmation(command: str, cwd: str, network: str) -> dict[str, str]:
    """Plain words for a command: a Home Assistant action when one is named, else the command itself."""
    lower = (command or "").lower()
    call = _SERVICE_PATH.search(lower)
    if call:
        domain, service = call.groups()
        if (domain, service) in _RELOADS:
            what, effect = _RELOADS[(domain, service)]
            return {"what": what, "where": "This Home Assistant.", "effect": effect}
        targets = [entity for entity in _entity_ids(lower) if entity.split(".")[0] == domain] or _entity_ids(lower)
        where = ", ".join(targets) if targets else f"every {domain} the action reaches"
        verb = _SERVICE_WORDS.get(service, f"runs {domain}.{service} on")
        if domain == "scene" and service == "turn_on":
            verb = "applies"
        return {"what": f"Run the Home Assistant action {domain}.{service}.", "where": (where if targets else where.capitalize()) + ".",
                "effect": f"This {verb} {where}."}
    where = f"In {cwd}" + (f", reaching {network}" if network else "") + "." if cwd else "On this Home Assistant."
    return {"what": "Run one command outside the sandbox, shown below.", "where": where,
            "effect": "Only what that command does; it may read files and use the network."}


def _file_effect(paths: list[str]) -> str:
    """The plain effect of changing these files: the most specific one that applies to each, without repeats."""
    effects: list[str] = []
    for path in paths:
        effect = next((text for pattern, text in _FILE_EFFECTS if pattern.search(path)), "The file changes.")
        if effect not in effects:
            effects.append(effect)
    return " ".join(effects)


def _changes_confirmation(changes: list[dict[str, Any]]) -> dict[str, str]:
    """Plain words for a file change: create, change or delete, which files, and what that does."""
    present = {str(change.get("kind") or "update") for change in changes}
    verbs = " and ".join(word.lower() for kind, word in _KIND_WORDS.items() if kind in present) or "change"
    paths = [str(change.get("path")) for change in changes if change.get("path")]
    count = len(paths)
    what = f"{verbs[0].upper() + verbs[1:]} {count} file{'s' if count != 1 else ''}."
    return {"what": what, "where": (", ".join(paths) + ".") if paths else "Not given.", "effect": _file_effect(paths)}


def confirmation(kind: str, *, command: str = "", cwd: str = "", network: str = "",
                 changes: list[dict[str, Any]] | None = None, refused: list[str] | None = None) -> dict[str, str]:
    """Return the three plain lines the person deciding reads: what changes, where, and the effect."""
    text = (_command_confirmation(command, cwd, network) if kind == "command"
            else _changes_confirmation(changes or []))
    if refused:
        text["effect"] = "Not allowed, so nothing happens. " + refusal_text(refused)
    return text
