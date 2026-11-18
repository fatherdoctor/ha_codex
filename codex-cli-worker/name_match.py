"""Find the one entity a person named, for a home_assistant_call request that gives a name instead of a target.

A wrong match changes the wrong device, so this rule is stricter than the find tool's (verification.find_row):

1. The domain comes from the service. light.turn_on searches lights only. A service whose domain holds no
   entities of its own (homeassistant.turn_on) is not accepted by name; the request must give the entity id.
2. Whole words. A name is cut into words at every character that is not a letter or a digit, after Unicode
   NFKC and case folding, with invisible format characters removed. Each word of the request must equal a word
   of the entity's friendly name or object id: "lamp" does not fit "lamppost".
3. Exact name. A friendly name with the same words in the same order is exact. One exact name in the domain is
   the match, also when longer names fit too. Two exact names are two candidates, not a match.
4. With no exact name, every entity that fits is counted, not only the rows shown. One in all is the match;
   two or more are candidates, and nothing is sent.
5. Credential-like and password-mode entities never match and never show as candidates.

The hostile cases for this rule are in tests/test_name_match.py (CORPUS); add a case there.
"""
from __future__ import annotations

import re
import unicodedata

from relay_reads import PRIVATE, password_state

ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
WORD_RE = re.compile(r"[^\W_]+")
MAX_NAME_LENGTH = 120
MAX_CANDIDATES = 10
# Services of these domains act on entities of other domains, so a name cannot pick the domain for them.
NO_OWN_ENTITIES = frozenset({"homeassistant"})


def name_words(text):
    """Return the words of a name: NFKC, case folded, format characters dropped, cut at non-letters and non-digits."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    visible = "".join(char for char in folded if unicodedata.category(char) != "Cf")
    return WORD_RE.findall(visible)


def entity_row(row, domain):
    """Return (entity_id, friendly name, state) for a state row of the domain that may match, or None."""
    entity_id = row.get("entity_id")
    state = row.get("state")
    if not isinstance(entity_id, str) or not ENTITY_RE.fullmatch(entity_id) or not isinstance(state, str):
        return None
    entity_domain, object_id = entity_id.split(".", 1)
    if entity_domain != domain or PRIVATE.search(object_id) or password_state(row):
        return None
    attributes = row.get("attributes")
    friendly = attributes.get("friendly_name") if isinstance(attributes, dict) else None
    return entity_id, friendly if isinstance(friendly, str) else "", state


def resolve(states, name, service):
    """Match a name to the entities of the service's domain.

    Return {"entity_id": ...} for the one match, or {"candidates": [...], "total": n} when none or several fit.
    Raise ValueError for a name or service that cannot be matched at all.
    """
    if not isinstance(name, str) or len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"name must be text of at most {MAX_NAME_LENGTH} characters")
    words = name_words(name)
    if not words:
        raise ValueError("name must hold a letter or a digit")
    domain = service.split(".", 1)[0]
    if domain in NO_OWN_ENTITIES:
        raise ValueError(f"{service} acts on other domains: give target.entity_id, not a name")
    exact, fitting = [], []
    for row in states:
        found = entity_row(row, domain) if isinstance(row, dict) else None
        if found is None:
            continue
        entity_id, friendly, state = found
        friendly_words = name_words(friendly)
        searched = set(friendly_words) | set(name_words(entity_id.split(".", 1)[1]))
        if not all(word in searched for word in words):
            continue
        candidate = {"entity_id": entity_id, "name": friendly[:120], "state": state[:120],
                     "other_words": len(searched - set(words))}
        fitting.append(candidate)
        if friendly_words == words:
            exact.append(candidate)
    chosen = exact or fitting
    if len(chosen) == 1:
        return {"entity_id": chosen[0]["entity_id"]}
    # The closest name first, as the find tool orders them: the device before its extra sensors and buttons.
    chosen.sort(key=lambda item: (item["other_words"], item["entity_id"]))
    shown = [{key: item[key] for key in ("entity_id", "name", "state")} for item in chosen[:MAX_CANDIDATES]]
    return {"candidates": shown, "total": len(chosen)}
