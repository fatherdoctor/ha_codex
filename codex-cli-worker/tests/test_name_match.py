"""The match rule for a home_assistant_call request by name: hostile names first (name_match.py).

The real reader of the result is Home Assistant's service call: the entity id this rule returns is the one
entity the action changes. So every case asks one question: does this name pick exactly that entity, or nothing?

Families from the hostile-input list, and the ones that cannot reach this code:
- case and form, invisible characters, separators, size and empty: cases below.
- quotes, comments, line ends, multiline, nesting, wrappers: n/a, the name is never parsed by a shell or a
  template; quote marks and line ends are word separators like any other punctuation (cases below show it).
- git shapes, settings with many values, template readers: n/a, no git, config or template input.
- time of check: the entity list is read after the person's yes and the call is sent at once by entity id; a
  rename in between sends the call to the entity that was matched (stated in AUDITING.md).
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from name_match import name_words, resolve


def light(object_id, friendly):
    return {
        "entity_id": f"light.{object_id}",
        "state": "off",
        "attributes": {"friendly_name": friendly},
    }


HOUSE = [
    light("office_overhead", "Office Overhead"),
    light("office_overhead_fan_light", "Office Overhead Fan"),
    light("lamppost", "Lamppost"),
    light("monitor_backlight", "Monitor"),
    light("media_room_overhead", "Media Room Overhead"),
    light("cafe_lamp", "Café Lamp"),
    light("porch", "Porch"),
    light("porch_2", "Porch"),
    {
        "entity_id": "switch.office_overhead",
        "state": "on",
        "attributes": {"friendly_name": "Office Overhead"},
    },
    {
        "entity_id": "button.media_room_overhead_identify",
        "state": "unknown",
        "attributes": {"friendly_name": "Media Room Overhead Identify"},
    },
    {
        "entity_id": "light.door_code_light",
        "state": "off",
        "attributes": {"friendly_name": "Hall"},
    },
    {
        "entity_id": "text.hall",
        "state": "x",
        "attributes": {"friendly_name": "Hall", "mode": "password"},
    },
    light("hall_pin_light", "Hall Pin"),
    light("thai_table", "กัน"),
    light("hindi_room", "कमरा"),
]

# (id, name, service, expected: an entity id, "none", "several", or "error"; family)
CORPUS = [
    (
        "exact",
        "media room overhead",
        "light.turn_on",
        "light.media_room_overhead",
        "allow",
    ),
    (
        "exact-wins-over-longer",
        "office overhead",
        "light.turn_on",
        "light.office_overhead",
        "allow",
    ),
    (
        "longer-exact",
        "office overhead fan",
        "light.turn_on",
        "light.office_overhead_fan_light",
        "allow",
    ),
    (
        "one-fits-not-exact",
        "media overhead",
        "light.turn_on",
        "light.media_room_overhead",
        "allow",
    ),
    ("substring-trap", "lamp", "light.turn_on", "light.cafe_lamp", "substring"),
    ("substring-only", "post", "light.turn_on", "none", "substring"),
    ("on-in-monitor", "on", "light.turn_on", "none", "substring"),
    (
        "upper-case",
        "MEDIA ROOM OVERHEAD",
        "light.turn_on",
        "light.media_room_overhead",
        "case and form",
    ),
    (
        "fullwidth",
        "ｍｅｄｉａ room overhead",
        "light.turn_on",
        "light.media_room_overhead",
        "case and form",
    ),
    ("accent-kept", "café lamp", "light.turn_on", "light.cafe_lamp", "case and form"),
    (
        "accent-decomposed",
        "café lamp",
        "light.turn_on",
        "light.cafe_lamp",
        "case and form",
    ),
    (
        "accent-dropped-object-id",
        "cafe lamp",
        "light.turn_on",
        "light.cafe_lamp",
        "case and form",
    ),
    (
        "zero-width-inside",
        "media r​oom overhead",
        "light.turn_on",
        "light.media_room_overhead",
        "invisible",
    ),
    (
        "bidi-override",
        "‮media room overhead",
        "light.turn_on",
        "light.media_room_overhead",
        "invisible",
    ),
    (
        "nbsp-and-tab",
        "media room\toverhead",
        "light.turn_on",
        "light.media_room_overhead",
        "invisible",
    ),
    (
        "nul-separates",
        "media\x00room overhead",
        "light.turn_on",
        "light.media_room_overhead",
        "invisible",
    ),
    ("cyrillic-o-lookalike", "оffice overhead", "light.turn_on", "none", "invisible"),
    (
        "quotes-are-separators",
        "\"office\" 'overhead'",
        "light.turn_on",
        "light.office_overhead",
        "separators",
    ),
    (
        "semicolon",
        "office; overhead",
        "light.turn_on",
        "light.office_overhead",
        "separators",
    ),
    (
        "newline",
        "office\noverhead",
        "light.turn_on",
        "light.office_overhead",
        "separators",
    ),
    (
        "underscore-id-form",
        "media_room_overhead",
        "light.turn_on",
        "light.media_room_overhead",
        "separators",
    ),
    ("two-exact", "porch", "light.turn_on", "several", "count"),
    ("many-fit", "overhead", "light.turn_on", "several", "count"),
    (
        "domain-from-service",
        "office overhead",
        "switch.turn_off",
        "switch.office_overhead",
        "domain",
    ),
    (
        "button-not-light",
        "media room overhead identify",
        "light.turn_on",
        "none",
        "domain",
    ),
    ("no-own-domain", "office overhead", "homeassistant.turn_on", "error", "domain"),
    ("private-object-id", "hall", "light.turn_on", "none", "private"),
    ("private-by-word", "hall pin", "light.turn_on", "none", "private"),
    ("password-text", "hall", "text.set_value", "none", "private"),
    ("empty", "", "light.turn_on", "error", "size and empty"),
    ("only-space", "   ", "light.turn_on", "error", "size and empty"),
    ("only-punctuation", "--_!!", "light.turn_on", "error", "size and empty"),
    ("too-long", "a" * 121, "light.turn_on", "error", "size and empty"),
    ("not-text", ["office"], "light.turn_on", "error", "size and empty"),
    # A vowel or tone mark is part of its word (review CX-SPEED.br F1, case R15). Cut at the mark, the
    # Thai names "กัน" and "กิน" are both the words "ก" "น", and the Hindi "कमर" is the name "कमरा".
    ("thai-mark-differs", "กิน", "light.turn_on", "none", "combining marks"),
    ("thai-same-marks", "กัน", "light.turn_on", "light.thai_table", "combining marks"),
    (
        "devanagari-vowel-sign-missing",
        "कमर",
        "light.turn_on",
        "none",
        "combining marks",
    ),
    (
        "devanagari-same-word",
        "कमरा",
        "light.turn_on",
        "light.hindi_room",
        "combining marks",
    ),
    ("only-a-mark", "\u0e31", "light.turn_on", "error", "combining marks"),
]


def outcome(name, service, states=HOUSE):
    try:
        found = resolve(states, name, service)
    except ValueError:
        return "error"
    if "entity_id" in found:
        return found["entity_id"]
    return "none" if found["total"] == 0 else "several"


class CorpusTests(unittest.TestCase):
    def test_every_corpus_case(self):
        failing = [
            (case_id, outcome(name, service), expected)
            for case_id, name, service, expected, _ in CORPUS
            if outcome(name, service) != expected
        ]
        self.assertEqual(failing, [], "failing corpus ids")

    def test_each_family_has_two_cases(self):
        families = [family for *_, family in CORPUS]
        self.assertTrue(all(families.count(family) >= 2 for family in families))


class RuleTests(unittest.TestCase):
    """The design's own cases (CX-SPEED section 8), one rule each."""

    def test_lamp_with_lamp_and_lamppost_acts_on_lamp(self):
        states = [light("lamppost", "Lamppost"), light("lamp", "Lamp")]
        self.assertEqual(outcome("lamp", "light.turn_on", states), "light.lamp")

    def test_lamp_with_only_lamppost_changes_nothing(self):
        self.assertEqual(
            outcome("lamp", "light.turn_on", [light("lamppost", "Lamppost")]), "none"
        )

    def test_the_count_is_all_matches_not_the_rows_shown(self):
        states = [light(f"overhead_{n}", f"Overhead {n}") for n in range(25)]
        found = resolve(states, "overhead", "light.turn_on")
        self.assertEqual((found["total"], len(found["candidates"])), (25, 10))

    def test_two_exact_names_are_candidates_with_the_exact_ones_only(self):
        found = resolve(HOUSE, "porch", "light.turn_on")
        self.assertEqual(
            [row["entity_id"] for row in found["candidates"]],
            ["light.porch", "light.porch_2"],
        )

    def test_candidates_show_the_closest_name_first_and_no_hidden_entity(self):
        found = resolve(HOUSE, "overhead", "light.turn_on")
        self.assertEqual(found["candidates"][0]["entity_id"], "light.office_overhead")
        self.assertEqual(set(found["candidates"][0]), {"entity_id", "name", "state"})
        hidden = resolve(HOUSE, "hall", "light.turn_on")
        self.assertEqual(hidden, {"candidates": [], "total": 0})

    def test_words_are_whole_and_folded(self):
        self.assertEqual(name_words("Café-Lamp_2 ​X"), ["café", "lamp", "2", "x"])


if __name__ == "__main__":
    unittest.main()
