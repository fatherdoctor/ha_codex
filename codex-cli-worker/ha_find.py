"""The worker side of the home_assistant_call find tool: a read-only search of the house's entities.

Codex calls find before an action, to get the exact entity_id, as a person would search the entity list. The
search is loose on purpose: it only reads, and the action that follows still goes through the person's answer to
the call tool (ha_call.py). name_match.py keeps the strict rule for a call that gives a name in place of a target.

What find reads, through Core with the worker's own access:

- the state list (/api/states), the same read the call tool's name match and the verify tool's find use;
- the area of each entity, from one fixed template (AREA_TEMPLATE) that holds no text from the request.

What find answers: at most MAX_RESULTS rows of entity id, friendly name, area and state, the closest first.
Credential-like and password-mode entities never show, as for every read. find sends nothing to Home Assistant
but these two reads; it has no path to a service call.

How a row fits the request:

1. query: the words a person used ("the overhead lamp in the media room"). Small words such as "the" and "in" are
   dropped. A word fits an entity when it is a word of its friendly name, entity id, area or domain, the same word
   with or without a trailing "s", the start (3 letters or more) of one of those words, or a device word for the
   entity's domain ("lamp" fits every light). Rows that hold every word come first. When no row holds every
   word, the rows that hold the most words are the answer, and the message says so.
2. area: the area's id, or every word of the area's name.
3. domain: the exact domain, for example light.

At least one of the three is needed. area or domain alone lists the area's or the domain's entities.
"""
from __future__ import annotations

import json
import re

import requests

from name_match import name_words
from relay_reads import PRIVATE, ReadError, fetch_states, password_state

MAX_RESULTS = 25
MAX_QUERY_LENGTH = 120
MAX_AREA_ANSWER_BYTES = 4 * 1024 * 1024
TEMPLATE_URL = "http://supervisor/core/api/template"
ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
DOMAIN_RE = re.compile(r"[a-z_][a-z0-9_]*")
FIELDS = frozenset({"query", "area", "domain", "limit"})
# One JSON row per line: [entity_id, area id, area name], for each entity that has an area (its own or its
# device's). The template is fixed: no text from the request is ever put into it.
AREA_TEMPLATE = (
    "{% for s in states %}{% set a = area_id(s.entity_id) %}{% if a %}"
    "{{ [s.entity_id, a, area_name(s.entity_id)] | tojson }}\n"
    "{% endif %}{% endfor %}"
)
# Words a person adds around a device name that name no device.
SMALL_WORDS = frozenset({"a", "an", "the", "in", "on", "of", "at", "to", "my", "our", "please", "and"})
# Words a person uses for the devices of a domain, where the entity's own name may not hold them.
DEVICE_WORDS = {
    "light": {"light", "lights", "lamp", "lamps", "bulb", "bulbs", "lighting"},
    "switch": {"switch", "switches", "plug", "plugs", "outlet", "outlets"},
    "fan": {"fan", "fans"},
    "cover": {"blind", "blinds", "shade", "shades", "curtain", "curtains", "cover", "covers"},
    "lock": {"lock", "locks"},
    "climate": {"thermostat", "thermostats", "heat", "heating", "cooling", "ac"},
    "media_player": {"tv", "television", "speaker", "speakers", "player"},
    "vacuum": {"vacuum", "robot"},
}


class FindRefused(ValueError):
    """A find request that cannot be run; the message says why, for Codex."""


def parse_request(arguments):
    """Check the find tool's arguments and return (query words, area, domain, limit); raise FindRefused if bad."""
    if not isinstance(arguments, dict):
        raise FindRefused("expected an object with query, area or domain")
    unknown = set(arguments) - FIELDS
    if unknown:
        raise FindRefused("unsupported field: " + ", ".join(sorted(unknown)))
    texts = {}
    for key in ("query", "area"):
        value = arguments.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_QUERY_LENGTH):
            raise FindRefused(f"{key} must be text of at most {MAX_QUERY_LENGTH} characters")
        texts[key] = value or ""
    domain = arguments.get("domain")
    if domain is not None and (not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain)):
        raise FindRefused("domain must be a Home Assistant domain, for example light")
    limit = arguments.get("limit", MAX_RESULTS)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise FindRefused(f"limit must be a number from 1 to {MAX_RESULTS}")
    words = name_words(texts["query"])
    # "the lamp" keeps "lamp"; a query of small words only keeps them all, so it is never empty by accident.
    words = [word for word in words if word not in SMALL_WORDS] or words
    if texts["query"] and not words:
        raise FindRefused("query must hold a letter or a digit")
    area = texts["area"].strip()
    if not words and not area and not domain:
        raise FindRefused("give a query, an area or a domain")
    return words, area, domain, limit


def read_areas(token):
    """Return {entity_id: (area id, area name)} from Core, or None when the areas cannot be read."""
    try:
        with requests.Session() as client:
            client.trust_env = False
            with client.post(TEMPLATE_URL, json={"template": AREA_TEMPLATE},
                             headers={"Authorization": f"Bearer {token}"}, timeout=(3, 10),
                             allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    return None
                data = bytearray()
                for chunk in response.iter_content(4096):
                    data.extend(chunk)
                    if len(data) > MAX_AREA_ANSWER_BYTES:
                        return None
    except requests.RequestException:
        return None
    areas = {}
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            entity_id, area_id, area_name = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entity_id, str) and isinstance(area_id, str):
            areas[entity_id] = (area_id, area_name if isinstance(area_name, str) else "")
    return areas


def word_fits(word, entity_words, domain):
    """Return True when one query word fits the words of an entity, or names a device of its domain."""
    if word in DEVICE_WORDS.get(domain, ()):
        return True
    for candidate in entity_words:
        if word == candidate or word.rstrip("s") == candidate.rstrip("s"):
            return True
        if len(word) >= 3 and candidate.startswith(word):
            return True
    return False


def area_fits(area, entity_area):
    """Return True when the area filter is the entity's area id, or every word of it is in the area's name."""
    if entity_area is None:
        return False
    area_id, area_name = entity_area
    if area == area_id:
        return True
    wanted = name_words(area)
    have = set(name_words(area_name)) | set(name_words(area_id))
    return bool(wanted) and all(word in have for word in wanted)


def visible_row(row):
    """Return (entity_id, domain, friendly name, state) for a state row find may show, or None."""
    if not isinstance(row, dict):
        return None
    entity_id = row.get("entity_id")
    state = row.get("state")
    if not isinstance(entity_id, str) or not ENTITY_RE.fullmatch(entity_id) or not isinstance(state, str):
        return None
    domain, object_id = entity_id.split(".", 1)
    if PRIVATE.search(object_id) or password_state(row):
        return None
    attributes = row.get("attributes")
    friendly = attributes.get("friendly_name") if isinstance(attributes, dict) else None
    return entity_id, domain, friendly if isinstance(friendly, str) else "", state


def search(states, areas, words, area, domain, limit):
    """Return the find answer for a state list and an area map (None when the areas could not be read)."""
    ranked = []
    for row in states:
        found = visible_row(row)
        if found is None:
            continue
        entity_id, entity_domain, friendly, state = found
        if domain and entity_domain != domain:
            continue
        entity_area = (areas or {}).get(entity_id)
        if area and not area_fits(area, entity_area):
            continue
        entity_words = set(name_words(friendly)) | set(name_words(entity_id.split(".", 1)[1]))
        entity_words |= {entity_domain}
        if entity_area:
            entity_words |= set(name_words(entity_area[1]))
        fitting = sum(word_fits(word, entity_words, entity_domain) for word in words)
        if words and not fitting:
            continue
        ranked.append({"fitting": fitting, "other_words": len(entity_words - set(words)),
                       "row": {"entity_id": entity_id, "name": friendly[:120],
                               "area": entity_area[1][:120] if entity_area else None, "state": state[:120]}})
    best = max((item["fitting"] for item in ranked), default=0)
    # The rows with the most words fitting; every word first, when any row holds them all.
    chosen = [item for item in ranked if item["fitting"] == best]
    chosen.sort(key=lambda item: (item["other_words"], item["row"]["entity_id"]))
    shown = [item["row"] for item in chosen[:limit]]
    if not chosen:
        message = ("No entity fits. Try other words, or list an area or a domain with find "
                   "before you look elsewhere.")
    elif words and best < len(words):
        message = (f"No entity holds every word; these hold {best} of {len(words)}. Check that one is the "
                   "device the person means before you act, or try other words.")
    else:
        message = "Use the exact entity_id in target.entity_id of the call tool."
    answer = {"status": "ok", "matches": shown, "total_matches": len(chosen),
              "truncated": len(chosen) > len(shown), "message": message}
    if areas is None:
        answer["areas"] = "unavailable: Home Assistant did not return the areas, so no row names its area"
    return answer


def find(arguments, token):
    """Run one find request with the worker's token; return the answer for Codex, or raise FindRefused."""
    words, area, domain, limit = parse_request(arguments)
    if not token:
        raise FindRefused("Home Assistant authentication is unavailable")
    try:
        states = fetch_states("/states", token)
    except (ReadError, requests.RequestException, ValueError) as exc:
        raise FindRefused(f"Could not read the entity list ({type(exc).__name__})") from None
    if not isinstance(states, list):
        raise FindRefused("Home Assistant returned an invalid entity list")
    areas = read_areas(token)
    if area and areas is None:
        raise FindRefused("Home Assistant did not return the areas: search with query or domain instead")
    return search(states, areas, words, area, domain, limit)
