"""The home_assistant_call find tool: a loose, read-only search that gives Codex the exact entity_id.

The house below is invented. A fake Core answers the two reads find makes (the state list and the area template)
and records every request, so the tests can assert that find sends nothing else.
"""
from __future__ import annotations

import json
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_server import server  # noqa: F401  First: it puts the worker's folder on sys.path.

# isort: split
import ha_call
import ha_call_mcp
import ha_find
import tool_threads

TOKEN = "invented-supervisor-token"
THREAD = "11111111-2222-4333-8444-666666666666"

STATES = [
    {"entity_id": "light.media_room_media_room_overhead", "state": "off",
     "attributes": {"friendly_name": "Media Room Overhead"}},
    {"entity_id": "light.media_room_lamp", "state": "on", "attributes": {"friendly_name": "Reading Lamp"}},
    {"entity_id": "button.media_room_overhead_identify", "state": "unknown",
     "attributes": {"friendly_name": "Media Room Overhead Identify"}},
    {"entity_id": "media_player.media_room_tv", "state": "off", "attributes": {"friendly_name": "Media Room TV"}},
    {"entity_id": "light.kitchen_ceiling", "state": "on", "attributes": {"friendly_name": "Kitchen Ceiling"}},
    {"entity_id": "switch.kitchen_coffee", "state": "off", "attributes": {"friendly_name": "Coffee Maker"}},
    {"entity_id": "light.hall_globe", "state": "off", "attributes": {"friendly_name": "Globe"}},
    {"entity_id": "sensor.media_room_api_key", "state": "invented-value",
     "attributes": {"friendly_name": "Media Room Overhead Key"}},
    {"entity_id": "input_text.media_room_overhead_code", "state": "invented-value",
     "attributes": {"friendly_name": "Media Room Overhead Code", "mode": "password"}},
    "not a state row",
]
AREAS = {
    "light.media_room_media_room_overhead": ("media_room", "Media Room"),
    "light.media_room_lamp": ("media_room", "Media Room"),
    "button.media_room_overhead_identify": ("media_room", "Media Room"),
    "media_player.media_room_tv": ("media_room", "Media Room"),
    "light.kitchen_ceiling": ("kitchen", "Kitchen"),
    "switch.kitchen_coffee": ("kitchen", "Kitchen"),
    "light.hall_globe": ("upstairs_hall", "Upstairs Hall"),
}


class FakeCore:
    """Stands in for requests.Session in ha_find and requests.post in ha_call; records each request."""

    def __init__(self, areas=AREAS, area_status=200):
        self.requests = []
        lines = "".join(json.dumps([entity, *pair]) + "\n" for entity, pair in areas.items())
        self.area_body = lines.encode()
        self.area_status = area_status

    def session(self):
        """A stand-in for requests.Session(): its post answers the area template from area_body."""
        return _Session(self)

    def fetch_states(self, path, token):
        self.requests.append(("GET", path, None))
        assert token == TOKEN
        if path == "/states":
            return json.loads(json.dumps(STATES))
        entity_id = path[len("/states/"):]
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    def service_post(self, url, **options):
        self.requests.append(("POST", url, options.get("json")))
        return SimpleNamespace(status_code=200, json=lambda: [], text="[]")


class _Session:
    """Enough of requests.Session for ha_find.read_areas: a context manager whose post streams one body."""

    trust_env = True

    def __init__(self, core):
        self.core = core

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **options):
        self.core.requests.append(("POST", url, options.get("json")))
        return _Response(self.core.area_status, self.core.area_body)


class _Response:
    """A streamed response, used as a context manager as read_areas uses it."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        return [self.body]


class FindTestCase(unittest.TestCase):
    """find with a fake Core in place of the state read and the template read."""

    area_status = 200

    def setUp(self):
        self.core = FakeCore(area_status=self.area_status)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ha_find, "fetch_states", self.core.fetch_states))
        self.stack.enter_context(patch.object(ha_find.requests, "Session", self.core.session))

    def find(self, **arguments):
        return ha_find.find(arguments, TOKEN)

    def ids(self, **arguments):
        return [row["entity_id"] for row in self.find(**arguments)["matches"]]


class SearchTests(FindTestCase):
    def test_the_media_room_overhead_is_found_first(self):
        """The 11-19 miss: the person's words find the light, the device before its button."""
        for query in ("media room overhead", "media room overhead light", "the overhead light in the media room",
                      "Media Room Overhead"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query=query)[0], "light.media_room_media_room_overhead")

    def test_a_device_word_fits_every_entity_of_its_domain(self):
        self.assertEqual(self.ids(query="media room lamp")[:2],
                         ["light.media_room_lamp", "light.media_room_media_room_overhead"])
        self.assertIn("light.media_room_media_room_overhead", self.ids(query="overhead lamp"))
        self.assertEqual(self.ids(query="media room tv"), ["media_player.media_room_tv"])

    def test_a_row_carries_the_id_the_name_the_area_and_the_state(self):
        row = self.find(query="media room overhead")["matches"][0]
        self.assertEqual(row, {"entity_id": "light.media_room_media_room_overhead", "name": "Media Room Overhead",
                               "area": "Media Room", "state": "off"})

    def test_the_area_name_counts_as_words_of_the_entity(self):
        """The reading lamp's own name says nothing of the room; its area does."""
        self.assertIn("light.media_room_lamp", self.ids(query="reading lamp media room"))
        self.assertEqual(self.ids(query="upstairs hall light"), ["light.hall_globe"])

    def test_when_no_row_holds_every_word_the_closest_rows_come_with_a_warning(self):
        answer = self.find(query="kitchen pendant")
        self.assertEqual(answer["matches"][0]["entity_id"], "light.kitchen_ceiling")
        self.assertIn("No entity holds every word; these hold 1 of 2", answer["message"])

    def test_an_answer_with_no_row_says_to_list_the_area_or_domain(self):
        answer = self.find(query="zzz qqq")
        self.assertEqual(answer["matches"], [])
        self.assertIn("list an area or a domain", answer["message"])


class FilterTests(FindTestCase):
    def test_area_alone_lists_the_area(self):
        self.assertEqual(sorted(self.ids(area="Kitchen")), ["light.kitchen_ceiling", "switch.kitchen_coffee"])
        self.assertEqual(sorted(self.ids(area="kitchen")), ["light.kitchen_ceiling", "switch.kitchen_coffee"])
        self.assertEqual(self.ids(area="hall"), ["light.hall_globe"])

    def test_domain_alone_lists_the_domain(self):
        self.assertEqual(sorted(self.ids(domain="light")), ["light.hall_globe", "light.kitchen_ceiling",
                                                            "light.media_room_lamp",
                                                            "light.media_room_media_room_overhead"])

    def test_area_and_domain_together(self):
        self.assertEqual(sorted(self.ids(area="media room", domain="light")),
                         ["light.media_room_lamp", "light.media_room_media_room_overhead"])
        self.assertEqual(self.ids(query="overhead", area="kitchen"), [])

    def test_both_searches_miss_and_the_room_list_still_finds_the_device(self):
        """David 2026-11-19: when find and the name match miss, Codex keeps looking; the room's list is one way.

        The person says "the big bowl" for the globe light upstairs. Neither the call's name match nor find's
        words fit, but the area's light list holds it, and the rules tell Codex to look there before it asks.
        """
        import name_match
        self.assertEqual(name_match.resolve(STATES, "big bowl", "light.turn_on")["total"], 0)
        self.assertEqual(self.ids(query="the big bowl"), [])
        self.assertEqual(self.ids(area="upstairs hall", domain="light"), ["light.hall_globe"])
        self.assertIn("list the room's or the domain's entities with find", server.PAGE_INSTRUCTIONS)


class CapAndHiddenTests(FindTestCase):
    def test_the_answer_holds_at_most_25_rows_and_says_it_was_cut(self):
        many = [{"entity_id": f"light.spot_{index:02d}", "state": "off",
                 "attributes": {"friendly_name": f"Spot {index}"}} for index in range(40)]
        with patch.object(ha_find, "fetch_states", lambda path, token: many):
            answer = self.find(domain="light")
        self.assertEqual(len(answer["matches"]), ha_find.MAX_RESULTS)
        self.assertEqual(ha_find.MAX_RESULTS, 25)
        self.assertEqual(answer["total_matches"], 40)
        self.assertTrue(answer["truncated"])
        with patch.object(ha_find, "fetch_states", lambda path, token: many):
            self.assertEqual(len(self.find(domain="light", limit=3)["matches"]), 3)

    def test_credential_like_and_password_entities_never_show(self):
        shown = self.ids(query="media room") + self.ids(area="media room") + self.ids(domain="sensor")
        self.assertNotIn("sensor.media_room_api_key", shown)
        self.assertNotIn("input_text.media_room_overhead_code", shown)

    def test_malformed_requests_are_refused_before_any_read(self):
        for arguments in ({}, {"query": ""}, {"query": 5}, {"query": "x" * 121}, {"domain": "Light"},
                          {"domain": "light.x"}, {"limit": 0}, {"limit": 26}, {"limit": True},
                          {"query": "lamp", "service": "light.turn_on"}, {"query": "!!!"}, "not an object"):
            with self.subTest(arguments=arguments), self.assertRaises(ha_find.FindRefused):
                ha_find.find(arguments, TOKEN)
        self.assertEqual(self.core.requests, [])


class ReadOnlyTests(FindTestCase):
    def test_find_reads_the_states_and_the_area_template_and_nothing_else(self):
        self.find(query="media room overhead")
        self.assertEqual([(method, url) for method, url, _ in self.core.requests],
                         [("GET", "/states"), ("POST", ha_find.TEMPLATE_URL)])

    def test_the_template_is_fixed_and_holds_no_text_of_the_request(self):
        self.find(query="{{ states }} kitchen", area="kitchen")
        bodies = [body for method, _, body in self.core.requests if method == "POST"]
        self.assertEqual(bodies, [{"template": ha_find.AREA_TEMPLATE}])

    def test_the_module_has_no_service_path(self):
        source = Path(ha_find.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/api/services", source)
        self.assertNotIn("CORE_SERVICES_URL", source)
        self.assertNotIn("ha_call", source.split('"""', 2)[2])


class AreasUnavailableTests(FindTestCase):
    area_status = 500

    def test_a_query_still_works_and_says_the_areas_are_missing(self):
        answer = self.find(query="media room overhead")
        self.assertEqual(answer["matches"][0]["entity_id"], "light.media_room_media_room_overhead")
        self.assertIsNone(answer["matches"][0]["area"])
        self.assertIn("unavailable", answer["areas"])

    def test_an_area_filter_is_refused(self):
        with self.assertRaises(ha_find.FindRefused):
            self.find(area="kitchen")


def call_worker():
    return SimpleNamespace(lock=threading.RLock(), tasks={"t1": {"task_id": "t1", "status": "running"}},
                           task_cancellation_requested=lambda task_id: False, ha_token=lambda: TOKEN)


class SocketTests(FindTestCase):
    """find through the call tool's worker: the same chat check, and no service call."""

    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(ha_call.requests, "post", self.core.service_post))
        self.stack.enter_context(patch.object(ha_call, "fetch_states", self.core.fetch_states))
        self.calls = ha_call.HomeAssistantCall(call_worker())
        tool_threads.CHATS.bind("t1", THREAD)
        self.addCleanup(tool_threads.CHATS.release, "t1")

    def test_find_needs_the_same_attended_chat_as_call(self):
        answer = self.calls.dispatch({"thread_id": THREAD, "tool": "find", "arguments": {"query": "kitchen"}})
        self.assertEqual(answer["status"], "unavailable")
        self.assertEqual(self.core.requests, [])
        self.calls.begin("t1")
        answer = self.calls.dispatch({"thread_id": THREAD, "tool": "find", "arguments": {"query": "kitchen"}})
        self.assertEqual(answer["status"], "ok")
        self.assertFalse([url for method, url, _ in self.core.requests if "/api/services/" in url])

    def test_a_refused_find_says_why(self):
        self.calls.begin("t1")
        answer = self.calls.dispatch({"thread_id": THREAD, "tool": "find", "arguments": {}})
        self.assertEqual(answer["status"], "refused")

    def test_an_unknown_tool_sends_nothing(self):
        self.calls.begin("t1")
        answer = self.calls.dispatch({"thread_id": THREAD, "tool": "write",
                                      "arguments": {"service": "light.turn_off"}})
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(self.core.requests, [])

    def test_the_call_with_an_exact_entity_id_sends_that_entity(self):
        """find's id goes into target.entity_id; the call sends exactly it and reads it back."""
        self.calls.begin("t1")
        found = self.calls.dispatch({"thread_id": THREAD, "tool": "find",
                                     "arguments": {"query": "media room overhead"}})
        entity_id = found["matches"][0]["entity_id"]
        self.core.requests.clear()
        answer = self.calls.dispatch({"thread_id": THREAD, "arguments": {
            "service": "light.turn_on", "target": {"entity_id": entity_id}}})
        self.assertEqual(answer["status"], "ok")
        services = [(url, body) for method, url, body in self.core.requests if method == "POST"]
        self.assertEqual(services, [(ha_call.CORE_SERVICES_URL + "light/turn_on",
                                     {"entity_id": "light.media_room_media_room_overhead"})])
        self.assertEqual(answer["readback"][0]["entity_id"], "light.media_room_media_room_overhead")


class BridgeTests(unittest.TestCase):
    def test_the_bridge_lists_find_as_a_read_and_forwards_it_with_its_name(self):
        tools = {tool["name"]: tool for tool in ha_call_mcp.respond({"method": "tools/list"})["tools"]}
        self.assertTrue(tools["find"]["annotations"]["readOnlyHint"])
        self.assertIn("Read-only", tools["find"]["description"])
        self.assertIn("target.entity_id", tools["call"]["description"])
        sent = []
        with patch.object(ha_call_mcp, "call", lambda *args: sent.append(args) or {"status": "ok"}):
            answer = ha_call_mcp.respond({"method": "tools/call", "params": {
                "name": "find", "arguments": {"query": "lamp"}, "_meta": {"threadId": THREAD}}})
        self.assertFalse(answer["isError"])
        self.assertEqual(sent, [({"query": "lamp"}, THREAD, "find")])

    def test_the_launch_approves_find_ahead_and_still_asks_for_each_call(self):
        values = server.call_mcp_args()
        self.assertIn('mcp_servers.home_assistant_call.tools.find.approval_mode="approve"', values)
        self.assertIn('mcp_servers.home_assistant_call.tools.call.approval_mode="prompt"', values)


if __name__ == "__main__":
    unittest.main()
