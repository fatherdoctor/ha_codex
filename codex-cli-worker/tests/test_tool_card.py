"""Codex's own request before a tool call becomes a page card that shows the request in full (CX-SIMPLE.B)."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import threading
from typing import ClassVar
import unittest

from test_approvals import (
    Transport,  # first: it puts the worker folder on the import path
)
import approvals
from approvals import AppServerSession

QUESTION = 'Allow the home_assistant_call MCP server to run tool "call"?'


def tool_params(**overrides):
    """A Home Assistant action as the plain tool receives it, with invented entity ids."""
    params = {
        "service": "light.turn_off",
        "target": {"entity_id": "light.kitchen_ceiling"},
    }
    params.update(overrides)
    return params


class ToolCardTests(unittest.TestCase):
    """One card for each tool request of the Home Assistant tool; every other question is declined as before."""

    def setUp(self):
        self.writer = Transport()
        self.events = []
        self.saved = []
        self.session = AppServerSession(
            self.writer,
            lock=threading.RLock(),
            thread_params={
                "sandbox": "workspace-write",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
            turn_params={
                "input": [{"type": "text", "text": "Turn off the kitchen light"}]
            },
            on_event=self.events.append,
            on_approvals=self.saved.append,
            on_final=lambda text: None,
            is_active=lambda: True,
            redact=lambda text: text.replace("hidden-secret", "[redacted]"),
        )
        self.session.start()
        self.session.receive({"id": "worker-1", "result": {}})
        self.session.receive(
            {
                "id": "worker-2",
                "result": {
                    "thread": {"id": "thread"},
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                },
            }
        )
        self.session.receive({"id": "worker-3", "result": {"turn": {"id": "turn"}}})

    def ask(self, request_id=7, arguments=None, **overrides):
        """Send the request Codex 0.166.0 sends before a tool call; return the records saved for the page."""
        params = {
            "threadId": "thread",
            "turnId": "turn",
            "serverName": "home_assistant_call",
            "mode": "form",
            "message": QUESTION,
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "persist": ["session", "always"],
                "tool_params": tool_params() if arguments is None else arguments,
            },
        }
        params.update(overrides)
        before = len(self.writer.messages())
        self.session.receive(
            {
                "id": request_id,
                "method": "mcpServer/elicitation/request",
                "params": params,
            }
        )
        return self.writer.messages()[before:]

    def record(self):
        return self.saved[-1][-1]

    def test_request_waits_for_the_owner_and_sends_nothing_yet(self):
        self.assertEqual(self.ask(), [])
        record = self.record()
        self.assertEqual(
            (record["kind"], record["status"], record["tool"]),
            ("tool", "pending", "home_assistant_call.call"),
        )
        self.assertTrue(record["can_allow"])
        self.assertTrue(self.session.waiting_for_user())

    def test_card_shows_every_argument_exactly(self):
        arguments = tool_params(
            service="notify.notify",
            target=None,
            data={
                "title": "Kitchen",
                "message": "Line one.\nLine two, with light.porch in it.",
            },
        )
        self.ask(arguments=arguments)
        self.assertEqual(json.loads(self.record()["arguments"]), arguments)

    def test_plain_words_name_the_action_and_only_its_target(self):
        self.ask(arguments=tool_params(data={"note": "not light.porch"}))
        plain = self.record()["confirmation"]
        self.assertEqual(plain["what"], "Run the Home Assistant action light.turn_off.")
        self.assertEqual(plain["where"], "light.kitchen_ceiling.")
        self.assertEqual(plain["effect"], "This turns off light.kitchen_ceiling.")

    def test_plain_words_name_every_id_the_target_holds(self):
        """The three plain lines never name fewer places than the call reaches (review F1)."""
        cases = {  # target: (where, what the effect line says it turns off)
            "an entity and an area": (
                {"entity_id": "light.kitchen_ceiling", "area_id": "upstairs"},
                "light.kitchen_ceiling, every light in area upstairs.",
            ),
            "an entity, a floor and a label": (
                {
                    "entity_id": "light.kitchen_ceiling",
                    "floor_id": "first",
                    "label_id": "all_lights",
                },
                "light.kitchen_ceiling, every light on floor first, every light with label all_lights.",
            ),
            "entities of two domains": (
                {"entity_id": ["light.desk", "switch.desk_fan"]},
                "light.desk, switch.desk_fan.",
            ),
            "only an area": ({"area_id": "upstairs"}, "Every light in area upstairs."),
            "two devices": (
                {"device_id": ["device_one", "device_two"]},
                "Every light of device device_one, every light of device device_two.",
            ),
            "every entity of the domain": ({"entity_id": "all"}, "Every light."),
        }
        for number, (name, (target, where)) in enumerate(cases.items(), start=20):
            with self.subTest(name):
                self.ask(request_id=number, arguments=tool_params(target=target))
                plain = self.record()["confirmation"]
                self.assertEqual(plain["where"], where)
                self.assertEqual(
                    plain["effect"], "This turns off " + where[0].lower() + where[1:]
                )

    def test_plain_words_claim_no_limit_for_a_target_they_cannot_read(self):
        """No target, an unknown field or a value that is not an id: the words say every light the action reaches."""
        targets = [
            None,
            {},
            {"code": "invented"},
            {"entity_id": [1]},
            "light.desk",
            {"entity_id": "light.desk", "code": "invented"},
            {"entity_id": "light.desk", "area_id": [1]},
        ]
        for number, target in enumerate(targets, start=30):
            with self.subTest(target=target):
                self.ask(request_id=number, arguments=tool_params(target=target))
                plain = self.record()["confirmation"]
                self.assertEqual(plain["where"], "Every light the action reaches.")
                self.assertEqual(
                    plain["effect"], "This turns off every light the action reaches."
                )

    def test_allow_once_accepts_this_call_only(self):
        self.ask()
        self.session.decide(self.record()["approval_id"], "allow")
        # No persist value: Codex reads a plain accept as one approval and asks again before the next call.
        self.assertEqual(
            self.writer.messages()[-1],
            {"id": 7, "result": {"action": "accept", "content": None, "_meta": None}},
        )
        self.assertEqual(self.record()["status"], "allowed")

    def test_deny_declines(self):
        self.ask()
        self.session.decide(self.record()["approval_id"], "deny")
        self.assertEqual(
            self.writer.messages()[-1],
            {"id": 7, "result": {"action": "decline", "content": None, "_meta": None}},
        )

    def assert_deny_only(self):
        record = self.record()
        self.assertFalse(record["can_allow"])
        self.assertIn("cannot be allowed here", record["confirmation"]["effect"])
        before = len(self.writer.messages())
        with self.assertRaises(ValueError):
            self.session.decide(record["approval_id"], "allow")
        self.assertEqual(len(self.writer.messages()), before)

    def test_clipped_request_has_no_allow(self):
        self.ask(
            arguments=tool_params(data={"message": "x" * (approvals.DISPLAY_LIMIT + 1)})
        )
        self.assert_deny_only()

    def test_request_with_a_hidden_value_has_no_allow(self):
        self.ask(arguments=tool_params(data={"message": "hidden-secret"}))
        self.assertNotIn("hidden-secret", self.record()["arguments"])
        self.assert_deny_only()

    def test_request_without_arguments_has_no_allow(self):
        self.ask(arguments="light.turn_off")
        self.assert_deny_only()

    def test_other_questions_are_still_declined_in_the_chat(self):
        cases = {
            "another server": {
                "serverName": "codex_scheduling",
                "message": 'Allow the codex_scheduling MCP server to run tool "list_schedules"?',
            },
            "message names another server": {
                "message": 'Allow the github MCP server to run tool "call"?'
            },
            "another tool of the server": {
                "message": 'Allow the home_assistant_call MCP server to run tool "read"?'
            },
            "the server's own question": {"_meta": None, "message": "Pick a light"},
            "not a form": {"mode": "url"},
            "a different sentence": {"message": QUESTION + " Also allow everything"},
        }
        for number, (name, overrides) in enumerate(cases.items(), start=10):
            with self.subTest(name):
                reply = self.ask(request_id=number, **overrides)
                self.assertEqual(
                    reply, [{"id": number, "result": {"action": "decline"}}]
                )
                self.assertEqual(self.events[-1]["type"], "error")
        self.assertEqual(self.saved, [])

    def test_question_without_the_approval_kind_is_declined(self):
        """The exact sentence, form mode and the arguments, but no codex_approval_kind: no card (review F4)."""
        reply = self.ask(_meta={"tool_params": tool_params()})
        self.assertEqual(reply, [{"id": 7, "result": {"action": "decline"}}])
        self.assertEqual(self.events[-1]["type"], "error")
        self.assertEqual(self.saved, [])

    def test_request_for_another_turn_is_refused(self):
        with self.assertRaises(ValueError):
            self.ask(turnId="old-turn")
        self.assertEqual(self.saved, [])

    def test_resolved_elsewhere_expires_the_card(self):
        self.ask()
        self.session.receive(
            {
                "method": "serverRequest/resolved",
                "params": {"threadId": "thread", "requestId": 7},
            }
        )
        self.assertEqual(self.record()["status"], "expired")
        with self.assertRaises(LookupError):
            self.session.decide(self.record()["approval_id"], "allow")

    def test_closed_exchange_expires_the_card(self):
        self.ask()
        self.session.close()
        self.assertEqual(self.record()["status"], "expired")
        self.assertFalse(
            any(
                message.get("result", {}).get("action") == "accept"
                for message in self.writer.messages()
            )
        )

    def test_over_the_limit_is_declined_in_the_tool_form(self):
        self.session._records.extend(
            {"approval_id": str(n), "status": "denied"}
            for n in range(approvals.REQUEST_LIMIT)
        )
        reply = self.ask()
        self.assertEqual(
            reply,
            [
                {
                    "id": 7,
                    "result": {"action": "decline", "content": None, "_meta": None},
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()


class HassMcpWriteCardTests(unittest.TestCase):
    """The write tools of the bundled Home Assistant MCP server get the same card as home_assistant_call (CX-05-MCP)."""

    setUp = ToolCardTests.setUp
    record = ToolCardTests.record

    def ask(self, tool, arguments, request_id=7):
        """Send Codex's question before a call of one hass-mcp tool; return what the session wrote back."""
        params = {
            "threadId": "thread",
            "turnId": "turn",
            "serverName": "home_assistant_mcp",
            "mode": "form",
            "message": f'Allow the home_assistant_mcp MCP server to run tool "{tool}"?',
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": arguments},
        }
        before = len(self.writer.messages())
        self.session.receive(
            {
                "id": request_id,
                "method": "mcpServer/elicitation/request",
                "params": params,
            }
        )
        return self.writer.messages()[before:]

    def test_each_write_tool_waits_for_the_owner_with_its_arguments_in_full(self):
        arguments = {
            "url_path": "dashboard-test",
            "view": 0,
            "card": {"type": "markdown", "content": "Hello"},
        }
        for number, tool in enumerate(approvals.hass_mcp.WRITE_TOOLS, start=20):
            with self.subTest(tool=tool):
                self.assertEqual(self.ask(tool, arguments, request_id=number), [])
                record = self.record()
                self.assertEqual(
                    (record["status"], record["tool"]),
                    ("pending", f"home_assistant_mcp.{tool}"),
                )
                self.assertTrue(record["can_allow"])
                self.assertEqual(json.loads(record["arguments"]), arguments)

    def test_allow_once_accepts_this_call_only_and_deny_declines(self):
        self.ask("restart_ha", {})
        self.session.decide(self.record()["approval_id"], "allow")
        self.assertEqual(
            self.writer.messages()[-1],
            {"id": 7, "result": {"action": "accept", "content": None, "_meta": None}},
        )
        self.ask(
            "entity_action",
            {"entity_id": "light.desk_lamp", "action": "off"},
            request_id=8,
        )
        self.session.decide(self.record()["approval_id"], "deny")
        self.assertEqual(
            self.writer.messages()[-1],
            {"id": 8, "result": {"action": "decline", "content": None, "_meta": None}},
        )

    def test_a_read_tool_question_is_declined(self):
        # Reads are approved ahead, so Codex never asks; a question about one is not a card.
        reply = self.ask("get_entity", {"entity_id": "light.desk_lamp"})
        self.assertEqual(reply, [{"id": 7, "result": {"action": "decline"}}])
        self.assertEqual(self.saved, [])

    def words(self, tool, arguments):
        """Ask about one call, each with its own request id, and return the card's plain words."""
        self.asked = getattr(self, "asked", 100) + 1
        self.ask(tool, arguments, request_id=self.asked)
        return self.record()["confirmation"]

    def test_entity_action_names_the_action_and_every_target_params_add(self):
        plain = self.words(
            "entity_action",
            {
                "entity_id": "light.desk_lamp",
                "action": "on",
                "params": {"brightness": 90},
            },
        )
        self.assertEqual(plain["what"], "Run the Home Assistant action light.turn_on.")
        self.assertEqual(plain["where"], "light.desk_lamp.")
        # hass-mcp sends {"entity_id": entity_id, **params}: an entity_id in params replaces the first one.
        plain = self.words(
            "entity_action",
            {
                "entity_id": "light.desk_lamp",
                "action": "off",
                "params": {"entity_id": "all", "area_id": "hall"},
            },
        )
        self.assertEqual(plain["where"], "Every light, every light in area hall.")

    def test_call_service_tool_names_the_targets_in_its_data_only(self):
        plain = self.words(
            "call_service_tool",
            {
                "domain": "lock",
                "service": "unlock",
                "data": {"entity_id": "lock.side_gate", "code_note": "not lock.porch"},
            },
        )
        self.assertEqual(plain["what"], "Run the Home Assistant action lock.unlock.")
        self.assertEqual(plain["where"], "lock.side_gate.")
        plain = self.words(
            "call_service_tool", {"domain": "light", "service": "turn_off"}
        )
        self.assertEqual(plain["where"], "Every light the action reaches.")

    def test_restart_and_reload_read_as_the_call_tool_reads_them(self):
        self.assertEqual(
            self.words("restart_ha", {})["what"], "Restart Home Assistant."
        )
        self.assertEqual(
            self.words(
                "call_service_tool", {"domain": "automation", "service": "reload"}
            )["what"],
            "Reload the automations.",
        )

    def test_dashboard_tools_say_which_dashboard_and_whether_it_is_a_dry_run(self):
        plain = self.words("remove_view", {"url_path": "dashboard-test", "view": 1})
        self.assertEqual(
            (plain["what"], plain["where"]),
            ("Remove a view from a dashboard.", "The dashboard dashboard-test."),
        )
        self.assertIn("changes at once", plain["effect"])
        plain = self.words(
            "set_dashboard_config", {"config": {"views": []}, "dry_run": True}
        )
        self.assertEqual(plain["where"], "The default dashboard.")
        self.assertIn("Nothing changes", plain["effect"])

    def test_arguments_out_of_form_fall_back_to_the_tool_and_its_arguments(self):
        plain = self.words(
            "call_service_tool", {"domain": "Light Domain", "service": "turn_on"}
        )
        self.assertEqual(
            plain["what"],
            "Run the tool home_assistant_mcp.call_service_tool, with the arguments shown below.",
        )


class AppToolCardTests(unittest.TestCase):
    """A tool of a ChatGPT app (GitHub through the ChatGPT sign-in, say) gets the same card in Default (CX-TOOL-CARD-ANY).

    Before, every such question was declined, so a draft pull request that Codex opened through the GitHub app
    failed in Default while it worked in Full access and Auto-review, where Codex does not ask the owner.
    """

    setUp = ToolCardTests.setUp
    record = ToolCardTests.record
    ARGUMENTS: ClassVar[dict] = {
        "repository_full_name": "example-owner/example-repo",
        "head": "feature",
        "base": "main",
        "title": "Add a test",
        "draft": True,
    }

    def ask(
        self,
        request_id=7,
        message='Allow GitHub to run tool "create_pull_request"?',
        meta=None,
        **overrides,
    ):
        """Send the request Codex 0.166.0 sends before an app tool call; return what the session wrote back."""
        params = self.ask_params(message, meta, **overrides)
        before = len(self.writer.messages())
        with redirect_stdout(io.StringIO()) as log:
            self.session.receive(
                {
                    "id": request_id,
                    "method": "mcpServer/elicitation/request",
                    "params": params,
                }
            )
        self.log = log.getvalue()
        return self.writer.messages()[before:]

    def ask_params(
        self,
        message='Allow GitHub to run tool "create_pull_request"?',
        meta=None,
        **overrides,
    ):
        """Return the params of the request Codex 0.166.0 sends before an app tool call."""
        params = {
            "threadId": "thread",
            "turnId": "turn",
            "serverName": "codex_apps",
            "mode": "form",
            "message": message,
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "source": "connector",
                "connector_id": "connector_github",
                "connector_name": "GitHub",
                "tool_title": "Create pull request",
                "tool_params": dict(self.ARGUMENTS),
                **(meta or {}),
            },
        }
        params.update(overrides)
        return params

    def decide(self, decision):
        with redirect_stdout(io.StringIO()) as log:
            self.session.decide(self.record()["approval_id"], decision)
        return log.getvalue()

    def test_default_shows_a_card_with_the_app_the_tool_and_every_argument(self):
        self.assertEqual(self.ask(), [])
        record = self.record()
        self.assertEqual(
            (record["kind"], record["status"], record["tool"], record["app"]),
            ("tool", "pending", "codex_apps.create_pull_request", "GitHub"),
        )
        self.assertTrue(record["can_allow"])
        self.assertEqual(json.loads(record["arguments"]), self.ARGUMENTS)
        plain = record["confirmation"]
        self.assertEqual(
            plain["what"],
            "Let GitHub run its tool create_pull_request, with the arguments shown below.",
        )
        self.assertEqual(
            plain["where"],
            "Your GitHub account, connected through your ChatGPT sign-in.",
        )
        self.assertIn(
            'Codex asks: Allow GitHub to run tool "create_pull_request"?',
            plain["effect"],
        )
        self.assertTrue(self.session.waiting_for_user())

    def test_approve_once_accepts_this_call_only_and_logs_names_only(self):
        self.ask()
        log = self.decide("allow")
        self.assertEqual(
            self.writer.messages()[-1],
            {"id": 7, "result": {"action": "accept", "content": None, "_meta": None}},
        )
        self.assertEqual(
            log, "Tool call codex_apps.create_pull_request allowed by the owner.\n"
        )
        self.assertNotIn("example-repo", log)

    def test_decline_declines_and_logs_one_line(self):
        self.ask()
        log = self.decide("deny")
        self.assertEqual(
            self.writer.messages()[-1],
            {"id": 7, "result": {"action": "decline", "content": None, "_meta": None}},
        )
        self.assertEqual(
            log, "Tool call codex_apps.create_pull_request denied by the owner.\n"
        )

    def test_a_decision_that_does_not_reach_codex_logs_expired_not_allowed(self):
        self.ask()

        def refuse(records):
            raise OSError("disk full")

        self.session.on_approvals = refuse
        with redirect_stdout(io.StringIO()) as log, self.assertRaises(OSError):
            self.session.decide(self.record()["approval_id"], "allow")
        self.assertEqual(
            log.getvalue(),
            "Tool call codex_apps.create_pull_request expired: the owner's decision "
            "did not reach Codex.\n",
        )
        self.assertNotIn(
            {"id": 7, "result": {"action": "accept", "content": None, "_meta": None}},
            self.writer.messages(),
        )

    def test_a_sentence_written_for_the_tool_names_it_by_its_title(self):
        self.ask(
            message="Allow GitHub to add a comment to a pull request?",
            meta={"tool_title": "Add comment"},
        )
        record = self.record()
        self.assertEqual(record["tool"], "codex_apps.Add comment")
        self.assertEqual(
            record["question"], "Allow GitHub to add a comment to a pull request?"
        )

    def test_a_title_or_app_name_that_is_not_plain_reads_as_generic_words(self):
        self.ask(
            message="Allow this?",
            meta={"tool_title": "<b>x</b>", "connector_name": "Git\nHub"},
        )
        record = self.record()
        self.assertEqual((record["tool"], record["app"]), ("codex_apps.tool", ""))
        self.assertEqual(
            record["confirmation"]["what"],
            "Let a ChatGPT app run its tool tool, with the arguments shown below.",
        )

    def test_a_title_or_app_name_with_a_hidden_value_reads_as_generic_words_on_the_card_and_in_the_log(
        self,
    ):
        self.ask(
            message="Allow the app to add a comment?",
            meta={"tool_title": "hidden-secret", "connector_name": "hidden-secret"},
        )
        record = self.record()
        self.assertEqual((record["tool"], record["app"]), ("codex_apps.tool", ""))
        self.assertNotIn("hidden-secret", json.dumps(record))
        log = self.decide("allow")
        self.assertEqual(log, "Tool call codex_apps.tool allowed by the owner.\n")

    def test_a_tool_name_with_a_hidden_value_reads_as_the_generic_word(self):
        self.ask(message='Allow GitHub to run tool "hidden-secret"?')
        record = self.record()
        self.assertEqual(record["tool"], "codex_apps.tool")
        self.assertNotIn("hidden-secret", json.dumps(record))
        self.assertEqual(
            self.decide("deny"), "Tool call codex_apps.tool denied by the owner.\n"
        )

    def test_a_title_with_a_period_and_spaces_makes_one_sentence_in_the_log(self):
        self.ask(
            message="Allow the app to do this?",
            meta={"tool_title": "x denied by the owner. Tool call y"},
        )
        self.assertEqual(
            self.record()["tool"], "codex_apps.x denied by the owner. Tool call y"
        )
        log = self.decide("allow")
        self.assertEqual(
            log,
            "Tool call codex_apps.x_denied_by_the_owner._Tool_call_y allowed by the owner.\n",
        )

    def test_a_question_with_hidden_text_can_only_be_denied(self):
        self.ask(
            message='Allow GitHub hidden-secret to run tool "create_pull_request"?'
        )
        self.assertFalse(self.record()["can_allow"])
        with self.assertRaises(ValueError):
            self.session.decide(self.record()["approval_id"], "allow")

    def test_read_only_still_declines_and_says_why(self):
        self.session.thread_params["sandbox"] = "read-only"
        reply = self.ask()
        self.assertEqual(reply, [{"id": 7, "result": {"action": "decline"}}])
        self.assertEqual(self.saved, [])
        self.assertIn("Read only chat changes nothing", self.events[-1]["message"])
        self.assertEqual(
            self.log,
            "Declined a tool question from codex_apps: the page has no card for it.\n",
        )

    def test_the_read_only_sentence_needs_a_read_only_chat(self):
        """Only Read only declines an app tool question today; a decline anywhere else must not blame Read only."""
        with redirect_stdout(io.StringIO()):
            self.session._decline_tool_question(8, self.ask_params())
        self.assertNotIn("Read only", self.events[-1]["message"])
        self.session.thread_params["sandbox"] = "read-only"
        with redirect_stdout(io.StringIO()):
            self.session._decline_tool_question(9, self.ask_params())
        self.assertIn("Read only chat changes nothing", self.events[-1]["message"])

    def test_an_app_question_that_is_not_a_tool_call_is_still_declined_and_logged(self):
        for number, overrides in enumerate(
            ({"mode": "url"}, {"_meta": {"codex_approval_kind": "browser_auth"}}),
            start=10,
        ):
            with self.subTest(overrides):
                reply = self.ask(request_id=number, **overrides)
                self.assertEqual(
                    reply, [{"id": number, "result": {"action": "decline"}}]
                )
                self.assertEqual(
                    self.events[-1]["message"],
                    "The codex_apps tool asked a question this page cannot show, so it was declined "
                    "and nothing was changed.",
                )
                self.assertIn("Declined a tool question from codex_apps", self.log)
        self.assertEqual(self.saved, [])

    def test_auto_review_makes_no_card(self):
        """In Auto-review Codex's reviewer answers in place of the owner and sends only its review events."""
        review = {
            "threadId": "thread",
            "turnId": "turn",
            "reviewId": "review-1",
            "targetItemId": "call-1",
            "action": {
                "type": "mcpToolCall",
                "server": "codex_apps",
                "toolName": "create_pull_request",
            },
        }
        self.session.receive(
            {
                "method": "item/autoApprovalReview/completed",
                "params": {
                    **review,
                    "review": {"status": "approved", "riskLevel": "low"},
                },
            }
        )
        self.assertEqual(self.saved, [])
        self.assertEqual(self.events[-1]["status"], "approved")
