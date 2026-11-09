"""Actions this app refuses whatever Codex or the user says, and the plain-words text for every request.

Codex asks before it runs a command outside its sandbox or changes a file. Before
that request reaches the user, the worker reads the action itself (never the
model's description of it) and checks it against a fixed list of categories:

- locks, the alarm, garage doors and other covers, and cameras;
- anything that sends a message to a person (notifications, text to speech,
  mail, chat and SMS services);
- publishing: a push, release, pull request or package upload to a public place;
- this app's own settings, permission policy and house rules.

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

import re
from typing import Any

# Home Assistant domains whose actions are never allowed, by category.
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
}

ALLOWED_DEVICE_DOMAINS = ("light", "switch", "scene")

_DOMAIN_TO_CATEGORY = {domain: category for category, domains in DENIED_DOMAINS.items() for domain in domains}
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
_MESSAGE_SCRIPT_NAME = r"script\.[a-z0-9_]*(?:notify|notification|announce|message|text|sms|call|alert)[a-z0-9_]*"
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
# In a file change, an action line: `action: lock.unlock`, `service: notify.mobile_app_phone`, `- action: cover.open_cover`.
_ACTION_LINE = re.compile(rf"\b(?:action|service)\s*:\s*[\"']?({_DENIED})\.[a-z0-9_]+")
_ACTION_SCRIPT = re.compile(r"\b(?:action|service)\s*:\s*[\"']?" + _MESSAGE_SCRIPT_NAME)


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
    return [category for category in REFUSALS if category in found]


def _added_lines(diff: str) -> str:
    """Return the lines a unified diff adds (or the whole text when it is not a diff, as for a new file)."""
    lines = (diff or "").splitlines()
    if not any(line.startswith(("+", "-", "@@")) for line in lines):
        return "\n".join(lines)
    return "\n".join(line[1:] for line in lines if line.startswith("+") and not line.startswith("+++"))


def _categories_in_changes(changes: list[dict[str, Any]]) -> list[str]:
    """Return the refused categories a file change would put into the configuration."""
    found: set[str] = set()
    for change in changes:
        added = _added_lines(str(change.get("diff") or "")).lower()
        for domain in _ACTION_LINE.findall(added):
            found.add(_DOMAIN_TO_CATEGORY[domain])
        if _ACTION_SCRIPT.search(added):
            found.add("messages")
        for path in (change.get("path"), change.get("move_path")):
            if path and _POLICY.search(str(path)):
                found.add("policy")
    return [category for category in REFUSALS if category in found]


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
