"""Share: a chat downloaded as Markdown, whole or its last lines, with the steps Codex printed."""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_server import server
from test_activity import command, item

# The mask corpus of the Share file. The real reader is a person: the owner sends the Markdown file to someone
# else, who reads it in a text editor, a chat or an issue. Each case is (id, family, text, secret, expected):
# "masked" means `secret` must not be in the file, "kept" means it must still be there. S1 to S15 are the cases
# of the review of this change; R and A cases were added with the fix (A cases look like secrets and are not). Add a new shape here first, then the rule.
# Families not covered: invisible characters (a zero-width space inside a token splits it, and the mask keeps
# both halves; no rule can tell such a split from two words), and secrets with no label or known shape (a
# password written in a sentence, a name, an address), which the dialog and DOCS.md tell the owner to look for.
# A known limit of the fixed lengths: a key word longer than 32 characters, or more than eight words around the
# secret word, is not read as a key. After the G and T cases this corpus is the contract: a new shape is a new
# case here, added in its own change.
SYNTHETIC_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzeW50aGV0aWMifQ.c3ludGhldGljLXNpZ25hdHVyZS12YWx1ZQ"
SYNTHETIC_SUPERVISOR = "synthetic-supervisor-token-value-0123456789"
SHARE_MASK_CORPUS = [
    ("S1", "label", f"Authorization: Bearer {SYNTHETIC_JWT}", SYNTHETIC_JWT, "masked"),
    ("S2", "label", f"HA_TOKEN={SYNTHETIC_JWT}", SYNTHETIC_JWT, "masked"),
    (
        "S3",
        "shape",
        f"Use this one: {SYNTHETIC_JWT} for the API",
        SYNTHETIC_JWT,
        "masked",
    ),
    (
        "S4",
        "shape",
        "the key sk-proj-SyntheticKeyValue0123456789abcdef works",
        "SyntheticKeyValue0123456789abcdef",
        "masked",
    ),
    ("S5", "shape", "ghp_" + "A" * 36, "A" * 36, "masked"),
    (
        "S6",
        "shape",
        f"supervisor {SYNTHETIC_SUPERVISOR}",
        SYNTHETIC_SUPERVISOR,
        "masked",
    ),
    ("S7", "label", "mqtt_password: synthPass99", "synthPass99", "masked"),
    ("S8", "label", "client_secret=synthSecret77", "synthSecret77", "masked"),
    (
        "S9",
        "quotes",
        '{"api_key": "syntheticapikeyvalue0123"}',
        "syntheticapikeyvalue0123",
        "masked",
    ),
    (
        "S10",
        "line ends",
        "token:\n  syntheticnextlinetoken0123",
        "syntheticnextlinetoken0123",
        "masked",
    ),
    (
        "S11",
        "quotes",
        '"access_token":"syntheticaccesstoken0123"',
        "syntheticaccesstoken0123",
        "masked",
    ),
    (
        "S12",
        "url",
        "https://example.invalid/api?token=syntheticquerytoken0123",
        "syntheticquerytoken0123",
        "masked",
    ),
    # The token rule needs 16 characters, so short words after "token" stay readable ("token: expired").
    ("S13", "size", "token: synthShort12", "synthShort12", "kept"),
    (
        "S14",
        "url",
        "mqtt://broker:synthUrlPass5@mqtt.example.invalid:1883",
        "synthUrlPass5",
        "masked",
    ),
    ("S15", "shape", "harc_" + "B" * 43, "B" * 43, "masked"),
    (
        "R1",
        "quotes",
        '{"password": "synth pass with spaces"}',
        "synth pass with spaces",
        "masked",
    ),
    ("R2", "quotes", "wifi_password: 'synthQuoted1'", "synthQuoted1", "masked"),
    ("R3", "case", "PASSWORD=SynthUpper8", "SynthUpper8", "masked"),
    ("R4", "case", "Passwd: synthMixed3", "synthMixed3", "masked"),
    ("R5", "label", "door_pin: 4821", "4821", "masked"),
    ("R6", "label", "secret_key = synthSuffix4", "synthSuffix4", "masked"),
    ("R7", "line ends", "mqtt_password:\r\nsynthCrlf6\r\n", "synthCrlf6", "masked"),
    ("R8", "line ends", "password:\n  synthNextLine2", "synthNextLine2", "masked"),
    ("R9", "separators", "user=a;password=synthSemi9;port=1", "synthSemi9", "masked"),
    (
        "R10",
        "nesting",
        'payload: \'{"password":"synthNested4"}\'',
        "synthNested4",
        "masked",
    ),
    (
        "R11",
        "url",
        "postgresql://ha:synthDbPass%40x@db.example.invalid/ha",
        "synthDbPass%40x",
        "masked",
    ),
    (
        "R12",
        "shape",
        "sk-ant-api03-SyntheticAnthropicKey0123456789",
        "SyntheticAnthropicKey0123456789",
        "masked",
    ),
    ("R13", "size", "password:", "password:", "kept"),
    ("R14", "separators", "secret=synthAmp1&next=2", "synthAmp1", "masked"),
    (
        "R15",
        "nesting",
        "- name: broker\n  options:\n    password: synthDeep5",
        "synthDeep5",
        "masked",
    ),
    (
        "A1",
        "allow",
        "I forgot the password for the broker, so I reset it.",
        "I forgot the password for the broker",
        "kept",
    ),
    (
        "A2",
        "allow",
        "commit 3f9a1c2b4d5e6f708192a3b4c5d6e7f8091a2b3c fixed it",
        "3f9a1c2b4d5e6f708192a3b4c5d6e7f8091a2b3c",
        "kept",
    ),
    (
        "A3",
        "allow",
        "mqtt_password: !secret mqtt_password",
        "!secret mqtt_password",
        "kept",
    ),
    (
        "A4",
        "allow",
        "http://homeassistant.local:8123/config/automation",
        "homeassistant.local:8123/config",
        "kept",
    ),
    ("A5", "allow", "pinned: true and spin: 3", "pinned: true and spin: 3", "kept"),
    ("A6", "allow", "Read secrets.yaml before the change", "secrets.yaml", "kept"),
    (
        "A7",
        "allow",
        "The task-runner-0123456789abcdefghij started",
        "task-runner-0123456789abcdefghij",
        "kept",
    ),
    # G cases: the closure of the fix. A value with no quotes runs to the end of its line, a quoted value may hold
    # an escaped quote, and a URL may have an empty user name.
    ("G1", "separators", "mqtt_password: Xy7&kd;92Zq", "kd;92Zq", "masked"),
    (
        "G2",
        "separators",
        "wifi_password: blue horse battery",
        "horse battery",
        "masked",
    ),
    ("G3", "quotes", 'password: "ab\\"cd9"', "cd9", "masked"),
    (
        "G4",
        "url",
        "redis://:synthRedis9@cache.example.invalid:6379",
        "synthRedis9",
        "masked",
    ),
    ("G5", "nesting", '{\\"password\\": \\"synthEsc12\\"}', "synthEsc12", "masked"),
    # Time cases: long runs with no space, which made the key and scheme rules scan the rest of the run from
    # each word start. Each case must take less than SHARE_MASK_TIME_LIMIT seconds.
    ("T1", "time", "a-" * 50_000 + "done", "done", "kept"),
    ("T2", "time", "-".join(f"id{n:04d}" for n in range(16_000)), "id15999", "kept"),
    ("T3", "time", "x." * 50_000 + "y", "x.x.x.y", "kept"),
]
# The longest time one Share file may spend in the mask for one case of the corpus.
SHARE_MASK_TIME_LIMIT = 1.0


class ShareMaskCorpusTests(unittest.TestCase):
    """The mask of the Share file against every case of SHARE_MASK_CORPUS."""

    def test_corpus(self):
        """share_redact masks each secret of the corpus and keeps each readable value."""
        failing = []
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": SYNTHETIC_SUPERVISOR}):
            for case_id, _family, text, secret, expected in SHARE_MASK_CORPUS:
                started = time.perf_counter()
                result = server.share_redact(text)
                seconds = time.perf_counter() - started
                kept = secret in result
                if kept != (expected == "kept"):
                    failing.append(f"{case_id}: {result[:200]!r}")
                if seconds >= SHARE_MASK_TIME_LIMIT:
                    failing.append(f"{case_id}: {seconds:.2f} s")
        self.assertEqual(failing, [], "failing cases:\n" + "\n".join(failing))

    def test_every_family_has_two_cases(self):
        """The corpus keeps at least two cases a family, so a family does not rest on one example."""
        families = {}
        for _case_id, family, *_rest in SHARE_MASK_CORPUS:
            families[family] = families.get(family, 0) + 1
        self.assertEqual(
            [family for family, count in families.items() if count < 2], []
        )


class ShareChatTests(unittest.TestCase):
    """GET /tasks/<task_id>/share."""

    def setUp(self):
        """Isolate task storage in a temp directory and mock the task runner."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (
            ("tasks", {}),
            ("task_activity", {}),
            ("active_task_runners", set()),
            ("running_processes", {}),
        ):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(
            patch.object(server, "task_root", return_value=self.root / "tasks")
        )
        self.stack.enter_context(
            patch.object(server, "TASK_STATE_FILE", self.root / "index.json")
        )
        self.stack.enter_context(
            patch.object(server, "CODEX_HOME", self.root / "codex")
        )
        self.stack.enter_context(
            patch.object(server, "api_token", return_value="test-token")
        )
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}

    def share(self, task_id, query=""):
        """Request the shared file of a chat."""
        return self.client.get(f"/tasks/{task_id}/share{query}", headers=self.headers)

    def finished_chat(self):
        """Create a chat with one finished exchange: a thinking line, a failed command, and an answer."""
        response = self.client.post(
            "/tasks",
            json={"prompt": "Why is the porch light on?"},
            headers=self.headers,
        )
        task_id = response.json["task_id"]
        turn_id = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        server.record_activity_event(
            task_id,
            item("reasoning", "r0", text="Reading the automations\nthen the logbook"),
        )
        server.record_activity_event(
            task_id,
            command("c1", "/bin/sh -lc 'grep porch'", exit_code=1, status="failed"),
        )
        server.update_task(
            task_id,
            status="completed",
            summary="A sunset automation turned it on.",
            completed_at=server.utc_now(),
        )
        server.finish_activity(task_id)
        server.active_task_runners.discard(task_id)
        return task_id

    def test_whole_chat_has_message_steps_and_answer(self):
        """The whole chat holds the message, each printed step with its mark, and the answer, as a download."""
        task_id = self.finished_chat()
        response = self.share(task_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/markdown")
        self.assertEqual(
            response.headers["Content-Disposition"],
            f'attachment; filename="codex-chat-{task_id}.md"',
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        text = response.get_data(as_text=True)
        self.assertIn("Why is the porch light on?", text)
        self.assertIn(
            "### Steps\n\n✦ Reading the automations\n  then the logbook\n› grep porch (exit 1)",
            text,
        )
        self.assertIn("## Codex\n\nA sunset automation turned it on.", text)
        self.assertLess(text.index("✦ Reading"), text.index("## Codex"))

    def test_steps_of_every_exchange_and_of_the_running_one(self):
        """An earlier exchange keeps its saved steps, and a running exchange gives the steps so far."""
        task_id = self.finished_chat()
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        (session_dir / f"rollout-2026-09-20T01-00-00-{session_id}.jsonl").write_text(
            "{}\n"
        )
        server.tasks[task_id]["session_id"] = session_id
        response = self.client.post(
            f"/tasks/{task_id}/continue",
            json={"message": "And the garage?"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.json)
        turn_id = server.tasks[task_id]["current_turn_id"]
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        server.record_activity_event(
            task_id, item("reasoning", "r9", text="Checking the garage")
        )
        text = self.share(task_id).get_data(as_text=True)
        self.assertIn("› grep porch (exit 1)", text)
        self.assertIn("And the garage?\n\n### Steps\n\n✦ Checking the garage", text)

    def test_values_that_look_like_tokens_are_masked(self):
        """A token pasted into a message is masked in the file, as the worker masks codex.log."""
        response = self.client.post(
            "/tasks",
            json={"prompt": "Use token: synthetictokenvalue0123456789"},
            headers=self.headers,
        )
        text = self.share(response.json["task_id"]).get_data(as_text=True)
        self.assertNotIn("synthetictokenvalue0123456789", text)
        self.assertIn("[redacted]", text)

    def test_corpus_in_every_place_of_the_file(self):
        """Each corpus case gets the same result in a message, a thinking line, a command and an answer."""
        self.stack.enter_context(
            patch.dict(os.environ, {"SUPERVISOR_TOKEN": SYNTHETIC_SUPERVISOR})
        )
        for case_id, family, text, secret, expected in SHARE_MASK_CORPUS:
            if family == "time":
                # A time case is a run of 100,000 characters for the mask alone; the chat shortens such a text.
                continue
            response = self.client.post(
                "/tasks", json={"prompt": f"Message {text}"}, headers=self.headers
            )
            task_id = response.json["task_id"]
            server.update_task(task_id, status="running")
            server.start_activity(task_id, server.tasks[task_id]["current_turn_id"])
            server.record_activity_event(
                task_id, item("reasoning", "r1", text=f"Thinking {text}")
            )
            server.record_activity_event(
                task_id, command("c1", f"/bin/sh -lc 'echo {text}'")
            )
            server.update_task(
                task_id,
                status="completed",
                summary=f"Answer {text}",
                completed_at=server.utc_now(),
            )
            server.finish_activity(task_id)
            server.active_task_runners.discard(task_id)
            shared = self.share(task_id).get_data(as_text=True)
            count = shared.count(secret)
            if expected == "masked":
                self.assertEqual(count, 0, case_id)
            else:
                # The message, the thinking line and the answer keep the text as written.
                self.assertGreaterEqual(count, 3, f"{case_id}: {shared!r}")

    def test_last_lines(self):
        """lines keeps only the end of the file; a bad number is refused."""
        task_id = self.finished_chat()
        whole = self.share(task_id).get_data(as_text=True).rstrip("\n").split("\n")
        last = self.share(task_id, "?lines=3").get_data(as_text=True)
        self.assertEqual(last, "\n".join(whole[-3:]) + "\n")
        self.assertEqual(
            self.share(task_id, f"?lines={len(whole) + 50}")
            .get_data(as_text=True)
            .rstrip("\n")
            .split("\n"),
            whole,
        )
        for bad in ("0", "-2", "x", "100001", "1.5"):
            self.assertEqual(self.share(task_id, f"?lines={bad}").status_code, 400, bad)

    def test_needs_the_token_and_a_known_chat(self):
        """The file is behind the worker's sign-in, like the chat itself, and an unknown chat is not found."""
        task_id = self.finished_chat()
        self.assertEqual(self.client.get(f"/tasks/{task_id}/share").status_code, 401)
        self.assertEqual(self.share("missing").status_code, 404)
        self.assertEqual(self.share("..%2Fx").status_code, 404)


if __name__ == "__main__":
    unittest.main()
