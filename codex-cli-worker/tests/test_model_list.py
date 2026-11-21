"""The chat page's model list comes from Codex (`model/list`), with the built-in list as the fallback."""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
if importlib.util.find_spec("websocket") is None:
    sys.modules["websocket"] = types.ModuleType("websocket")
SPEC = importlib.util.spec_from_file_location("codex_worker_server_model_list", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)
server.dismiss_wait_notice_async = lambda task_id: None

LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")


def entry(model: str, label: str | None = None, levels=LEVELS, hidden=False, default=False,
          tiers=(), default_tier=None) -> dict:
    """Return one `model/list` entry in the shape of app-server-protocol rust-v0.166.0 (invented models)."""
    return {
        "id": model, "model": model, "displayName": label or model.upper(), "description": "",
        "hidden": hidden, "isDefault": default, "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": level, "description": ""} for level in levels],
        "serviceTiers": [{"id": tier, "name": tier.title(), "description": ""} for tier in tiers],
        "defaultServiceTier": default_tier,
    }


# An invented model the built-in list does not have, as a new release of Codex would add.
NEW_MODEL = "gpt-9.9-nova"
LIVE = (
    server.ChatModel(NEW_MODEL, "GPT-9.9 Nova", ("low", "medium", "high", "turbo")),
    server.ChatModel("gpt-6-astra", "GPT-6 Astra", LEVELS),
)

FAKE_APP_SERVER = textwrap.dedent('''\
    #!{python}
    """A stand-in for `codex app-server`: answers initialize and two pages of model/list."""
    import json, sys
    pages = {{None: ([{first}], "page-2"), "page-2": ([{second}], None)}}
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if message["method"] == "initialize":
            result = {{"userAgent": "stand-in"}}
        elif message["method"] == "model/list":
            data, cursor = pages[message["params"].get("cursor")]
            print(json.dumps({{"method": "notice", "params": {{}}}}), flush=True)
            result = {{"data": data, "nextCursor": cursor}}
        else:
            print(json.dumps({{"id": message["id"], "error": {{"message": "unknown"}}}}), flush=True)
            continue
        print(json.dumps({{"id": message["id"], "result": result}}), flush=True)
''')


class ModelListStateMixin:
    """Each test starts with no live list and puts the worker's state back afterward."""

    def setUp(self) -> None:
        saved = dict(server.model_list_state)
        self.addCleanup(lambda: (server.model_list_state.clear(), server.model_list_state.update(saved)))
        server.model_list_state.update(models=None, read_at=None, tried_at=None, reading=False)
        # No test starts a real Codex: a refresh finds no binary unless a test says otherwise.
        patcher = patch.object(server, "codex_binary_path", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_live(self, models=LIVE) -> None:
        """Make the worker hold a fresh live list."""
        now = server.time.monotonic()
        server.model_list_state.update(models=models, read_at=now, tried_at=now, reading=False)


class ParseCodexModelsTests(unittest.TestCase):
    def test_keeps_visible_models_with_their_levels_default_first(self):
        models = server.parse_codex_models([
            entry("gpt-6-astra", "GPT-6-Astra"),
            entry(NEW_MODEL, "GPT-9.9 Nova", ("low", "medium", "turbo"), default=True),
            entry("gpt-6-luna", "GPT-6-Luna", LEVELS[:5]),
        ])
        self.assertEqual(models, (
            server.ChatModel(NEW_MODEL, "GPT-9.9 Nova", ("low", "medium", "turbo")),
            server.ChatModel("gpt-6-astra", "GPT-6-Astra", LEVELS),
            server.ChatModel("gpt-6-luna", "GPT-6-Luna", LEVELS[:5]),
        ))

    def test_fast_comes_from_the_service_tiers(self):
        odd_tiers = entry("gpt-odd-tiers")
        odd_tiers["serviceTiers"] = "priority"
        models = server.parse_codex_models([
            entry("gpt-fast-offered", tiers=("priority",)),
            entry("gpt-fast-default", tiers=("priority",), default_tier="priority"),
            entry("gpt-other-tier", tiers=("flex",), default_tier="flex"),
            entry("gpt-tier-not-listed", default_tier="priority"),
            odd_tiers,
        ])
        self.assertEqual({model.id: model.fast for model in models}, {
            "gpt-fast-offered": False, "gpt-fast-default": True, "gpt-other-tier": None,
            "gpt-tier-not-listed": None, "gpt-odd-tiers": None,
        })

    def test_leaves_out_hidden_retired_unsafe_and_malformed_entries(self):
        bad_levels = entry("gpt-bad-levels")
        bad_levels["supportedReasoningEfforts"] = "high"
        no_hidden_field = entry("gpt-no-hidden")
        del no_hidden_field["hidden"]
        models = server.parse_codex_models([
            entry("gpt-hidden", hidden=True),
            no_hidden_field,
            entry("gpt-5.5"),  # retired: runs gpt-5.6-sol, so the picker never offers it
            entry("--config=x"),
            entry("../../etc"),
            entry("gpt with space"),
            {"id": 7, "hidden": False, "supportedReasoningEfforts": []},
            entry("gpt-no-levels", levels=()),
            entry("gpt-odd-levels", levels=("High", "--x", "")),
            bad_levels,
            entry("gpt-6-astra"),
            entry("gpt-6-astra", "Second copy"),
        ])
        self.assertEqual([model.id for model in models], ["gpt-6-astra"])

    def test_label_falls_back_to_the_id_and_levels_are_not_repeated(self):
        odd = entry("gpt-6-sol", levels=("low", "low", "high"))
        odd["displayName"] = "bad\nlabel"
        self.assertEqual(server.parse_codex_models([odd]), (server.ChatModel("gpt-6-sol", "gpt-6-sol", ("low", "high")),))

    def test_the_list_is_bounded(self):
        many = [entry(f"gpt-{index}") for index in range(server.MODEL_LIST_MAX_MODELS + 20)]
        self.assertEqual(len(server.parse_codex_models(many)), server.MODEL_LIST_MAX_MODELS)


@unittest.skipIf(os.name == "nt", "the stand-in is a script with a shebang line")
class ReadCodexModelsTests(unittest.TestCase):
    def test_reads_every_page_from_app_server(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "codex"
            script.write_text(FAKE_APP_SERVER.format(
                python=sys.executable, first=repr(entry("gpt-6-astra")), second=repr(entry(NEW_MODEL))))
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            real_popen = subprocess.Popen

            def popen(command, **kwargs):
                # The worker runs Codex in /config; the stand-in runs in the temporary folder.
                self.assertEqual(command[1:], ["app-server", "--config", "check_for_update_on_startup=false"])
                return real_popen(command, **{**kwargs, "cwd": folder, "env": dict(os.environ)})

            with patch.object(server.subprocess, "Popen", side_effect=popen):
                entries = server.read_codex_models(str(script))
        self.assertEqual([item["id"] for item in entries], ["gpt-6-astra", NEW_MODEL])


class LiveListTests(ModelListStateMixin, unittest.TestCase):
    def test_built_in_list_until_codex_answers(self):
        self.assertIs(server.chat_models(), server.CHAT_MODELS)
        self.assertIsNone(server.model_efforts(NEW_MODEL))

    def test_live_list_replaces_the_built_in_one(self):
        self.use_live()
        self.assertEqual(server.chat_models(), LIVE)
        self.assertEqual(server.model_efforts(NEW_MODEL), ("low", "medium", "high", "turbo"))
        # A model only the built-in list has keeps its levels, so a saved chat on it still runs.
        self.assertEqual(server.model_efforts("gpt-6-luna"), server.CHAT_MODEL_EFFORTS["gpt-6-luna"])
        self.assertIn("turbo", server.known_efforts())

    def test_refresh_keeps_the_last_good_list_after_a_failure(self):
        self.use_live()
        server.model_list_state["read_at"] = None
        with (
            patch.object(server, "codex_binary_path", return_value="/usr/local/bin/codex"),
            patch.object(server, "read_codex_models", side_effect=TimeoutError("no answer")),
        ):
            server.refresh_models()
        self.assertEqual(server.model_list_state["models"], LIVE)
        self.assertFalse(server.model_list_state["reading"])

    def test_refresh_stores_a_good_answer(self):
        with (
            patch.object(server, "codex_binary_path", return_value="/usr/local/bin/codex"),
            patch.object(server, "read_codex_models", return_value=[entry(NEW_MODEL, "GPT-9.9 Nova")]),
        ):
            server.refresh_models()
        self.assertEqual(server.chat_models(), (server.ChatModel(NEW_MODEL, "GPT-9.9 Nova", LEVELS),))

    def test_an_empty_answer_keeps_the_built_in_list(self):
        with (
            patch.object(server, "codex_binary_path", return_value="/usr/local/bin/codex"),
            patch.object(server, "read_codex_models", return_value=[entry("gpt-hidden", hidden=True)]),
        ):
            server.refresh_models()
        self.assertIs(server.chat_models(), server.CHAT_MODELS)

    def test_a_stale_list_starts_one_read_in_the_background(self):
        self.use_live()
        # Older than the TTL, whatever the machine's uptime.
        stale = server.time.monotonic() - server.MODEL_LIST_TTL_SECONDS - 1
        server.model_list_state.update(read_at=stale, tried_at=stale)
        with patch.object(server.threading, "Thread") as thread:
            self.assertEqual(server.live_models(), LIVE)
            self.assertEqual(server.live_models(), LIVE)
        thread.assert_called_once()
        self.assertTrue(server.model_list_state["reading"])

    def test_no_new_read_soon_after_a_failed_one(self):
        server.model_list_state["tried_at"] = server.time.monotonic()
        with patch.object(server.threading, "Thread") as thread:
            self.assertIsNone(server.live_models())
        thread.assert_not_called()


class ChatSettingsTests(ModelListStateMixin, unittest.TestCase):
    def test_a_live_model_and_its_own_level_are_accepted(self):
        self.use_live()
        with patch.object(server, "read_options", return_value={}):
            settings = server.parse_chat_settings({"chat_settings": {"model": NEW_MODEL, "reasoning_effort": "turbo"}})
            self.assertEqual(settings["model"], NEW_MODEL)
            run = server.resolve_chat_settings(settings, {})
        self.assertEqual(run, {"model": NEW_MODEL, "reasoning_effort": "turbo", "fast": None})

    def test_fast_follows_the_model_unless_the_chat_chose(self):
        self.use_live((
            server.ChatModel("gpt-offers-fast", "Offers Fast", LEVELS, False),
            server.ChatModel("gpt-fast-default", "Fast Default", LEVELS, True),
            server.ChatModel("gpt-no-fast", "No Fast", LEVELS),
        ))
        cases = (("gpt-offers-fast", None, False), ("gpt-offers-fast", True, True),
                 ("gpt-fast-default", None, True), ("gpt-fast-default", False, False),
                 ("gpt-no-fast", True, None), ("gpt-no-fast", None, None))
        with patch.object(server, "read_options", return_value={}):
            for model, choice, runs in cases:
                with self.subTest(model=model, choice=choice):
                    settings = server.parse_chat_settings({"chat_settings": {"model": model, "fast": choice}})
                    self.assertEqual(settings["fast"], choice)
                    self.assertIs(server.resolve_chat_settings(settings, {})["fast"], runs)

    def test_fast_must_be_true_false_or_null(self):
        with patch.object(server, "read_options", return_value={}):
            for value in (1, 0, "on", "true", [], {}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    server.parse_chat_settings({"chat_settings": {"model": None, "fast": value}})

    def test_settings_saved_before_fast_existed_still_parse(self):
        with patch.object(server, "read_options", return_value={}):
            settings = server.parse_chat_settings({"chat_settings": {"model": None, "reasoning_effort": None}})
        self.assertIsNone(settings["fast"])

    def test_a_level_the_live_model_lacks_is_refused(self):
        self.use_live()
        with patch.object(server, "read_options", return_value={}), self.assertRaises(ValueError):
            server.parse_chat_settings({"chat_settings": {"model": NEW_MODEL, "reasoning_effort": "ultra"}})

    def test_an_unknown_model_asks_codex_once_before_it_is_refused(self):
        with (
            patch.object(server, "read_options", return_value={}),
            patch.object(server, "codex_binary_path", return_value="/usr/local/bin/codex"),
            patch.object(server, "read_codex_models", return_value=[entry(NEW_MODEL)]) as read,
        ):
            # A saved chat on a new model, after a worker restart: the check reads the list first.
            settings = server.parse_chat_settings({"chat_settings": {"model": NEW_MODEL, "reasoning_effort": None}})
            self.assertEqual(settings["model"], NEW_MODEL)
            read.reset_mock()
            with self.assertRaises(ValueError):
                server.parse_chat_settings({"chat_settings": {"model": "gpt-not-real", "reasoning_effort": None}})
        read.assert_not_called()

    def test_without_codex_an_unknown_model_is_still_refused(self):
        with patch.object(server, "read_options", return_value={}), self.assertRaises(ValueError):
            server.parse_chat_settings({"chat_settings": {"model": NEW_MODEL, "reasoning_effort": None}})

    def test_chat_options_name_the_source_of_the_list(self):
        with (
            patch.object(server, "read_options", return_value={}),
            patch.object(server, "api_token", return_value="test-token"),
            patch.object(server.threading, "Thread"),
        ):
            client = server.app.test_client()
            headers = {"Authorization": "Bearer test-token"}
            built_in = client.get("/chat-options", headers=headers).json
            self.use_live()
            live = client.get("/chat-options", headers=headers).json
        self.assertEqual(built_in["models_source"], "built-in")
        self.assertEqual([model["id"] for model in built_in["models"]], [model.id for model in server.CHAT_MODELS])
        self.assertEqual(live["models_source"], "codex")
        self.assertEqual(live["models"][0], {"id": NEW_MODEL, "label": "GPT-9.9 Nova",
                                             "efforts": ["low", "medium", "high", "turbo"], "fast": None})
        # The built-in list follows the pinned CLI's catalog: every model offers Fast, two run on it by default.
        self.assertEqual({model["id"]: model["fast"] for model in built_in["models"]}["gpt-6-sol"], True)
        self.assertIs(built_in["default_fast"], False)


class ReasoningEffortOptionTests(ModelListStateMixin, unittest.TestCase):
    def test_the_option_takes_any_plain_level(self):
        for value, expected in (("max", "max"), ("ultra", "ultra"), (" Turbo ", "turbo"), ("minimal", "minimal"),
                                ("", "medium"), (None, "medium"), ("--model=x", "medium"), ("a b", "medium"),
                                ("x" * 30, "medium")):
            with self.subTest(value=value):
                self.assertEqual(server.model_reasoning_effort({"model_reasoning_effort": value}), expected)

    def test_every_value_the_old_list_had_still_works(self):
        for value in ("minimal", "low", "medium", "high", "xhigh"):
            with self.subTest(value=value):
                run = server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS,
                                                   {"codex_model": "gpt-6-astra", "model_reasoning_effort": value})
                # No model supports minimal; it runs as medium, as before.
                self.assertEqual(run["reasoning_effort"], "medium" if value == "minimal" else value)

    def test_a_level_the_model_lacks_runs_on_medium(self):
        options = {"codex_model": "gpt-6-luna", "model_reasoning_effort": "ultra"}
        self.assertEqual(server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, options)["reasoning_effort"], "medium")
        options["model_reasoning_effort"] = "max"
        self.assertEqual(server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, options)["reasoning_effort"], "max")


if __name__ == "__main__":
    unittest.main()
