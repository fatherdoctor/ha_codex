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


def entry(model: str, label: str | None = None, levels=LEVELS, hidden=False, default=False) -> dict:
    """Return one `model/list` entry in the shape of app-server-protocol rust-v0.166.0 (invented models)."""
    return {
        "id": model, "model": model, "displayName": label or model.upper(), "description": "",
        "hidden": hidden, "isDefault": default, "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": level, "description": ""} for level in levels],
    }


# An invented model the built-in list does not have, as a new release of Codex would add.
NEW_MODEL = "gpt-9.9-nova"
LIVE = (
    (NEW_MODEL, "GPT-9.9 Nova", ("low", "medium", "high", "turbo")),
    ("gpt-6-astra", "GPT-6 Astra", LEVELS),
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
        server.model_list_state.update(models=None, default=None, read_at=None, tried_at=None, reading=False)
        # No test starts a real Codex: a refresh finds no binary unless a test says otherwise.
        patcher = patch.object(server, "codex_binary_path", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_live(self, models=LIVE, default=None) -> None:
        """Make the worker hold a fresh live list, with the account's default model and level when given."""
        now = server.time.monotonic()
        server.model_list_state.update(models=models, default=default, read_at=now, tried_at=now, reading=False)


class ParseCodexModelsTests(unittest.TestCase):
    def test_keeps_visible_models_with_their_levels_default_first(self):
        models = server.parse_codex_models([
            entry("gpt-6-astra", "GPT-6-Astra"),
            entry(NEW_MODEL, "GPT-9.9 Nova", ("low", "medium", "turbo"), default=True),
            entry("gpt-6-luna", "GPT-6-Luna", LEVELS[:5]),
        ])
        self.assertEqual(models, (
            (NEW_MODEL, "GPT-9.9 Nova", ("low", "medium", "turbo")),
            ("gpt-6-astra", "GPT-6-Astra", LEVELS),
            ("gpt-6-luna", "GPT-6-Luna", LEVELS[:5]),
        ))

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
        self.assertEqual([model for model, _, _ in models], ["gpt-6-astra"])

    def test_label_falls_back_to_the_id_and_levels_are_not_repeated(self):
        odd = entry("gpt-6-sol", levels=("low", "low", "high"))
        odd["displayName"] = "bad\nlabel"
        self.assertEqual(server.parse_codex_models([odd]), (("gpt-6-sol", "gpt-6-sol", ("low", "high")),))

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
        self.assertEqual(server.chat_models(), ((NEW_MODEL, "GPT-9.9 Nova", LEVELS),))

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
        self.assertEqual(run, {"model": NEW_MODEL, "reasoning_effort": "turbo"})

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
        self.assertEqual([model["id"] for model in built_in["models"]], [model for model, _, _ in server.CHAT_MODELS])
        self.assertEqual(live["models_source"], "codex")
        self.assertEqual(live["models"][0], {"id": NEW_MODEL, "label": "GPT-9.9 Nova",
                                             "efforts": ["low", "medium", "high", "turbo"], "default_effort": "medium"})


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


# The live list of an account whose default is a Sol newer than the built-in list knows (invented ids).
SOL_LIVE = (
    ("gpt-6.1-sol", "GPT-6.1 Sol", LEVELS),
    ("gpt-7.10-sol", "GPT-7.10 Sol", LEVELS),
    ("gpt-7.9-sol", "GPT-7.9 Sol", LEVELS),
    ("gpt-6-astra", "GPT-6 Astra", LEVELS),
)


class AccountDefaultTests(ModelListStateMixin, unittest.TestCase):
    """A chat on default runs on the account's default model and level, with a fallback for each missing source."""

    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(server, "seen_default_model", return_value="")
        self.seen = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_entry_marked_default_gives_the_model_and_its_level(self):
        entries = [entry("gpt-6-astra"), entry(NEW_MODEL, default=True)]
        entries[1]["defaultReasoningEffort"] = "low"
        self.assertEqual(server.parse_codex_default(entries, server.parse_codex_models(entries)),
                         server.CodexDefault(NEW_MODEL, "low"))

    def test_a_level_the_model_lacks_or_no_marked_model_falls_back(self):
        odd = entry(NEW_MODEL, levels=("low", "high"), default=True)
        odd["defaultReasoningEffort"] = "turbo"
        self.assertEqual(server.parse_codex_default([odd], server.parse_codex_models([odd])),
                         server.CodexDefault(NEW_MODEL, "low"))
        hidden = entry(NEW_MODEL, hidden=True, default=True)
        self.assertIsNone(server.parse_codex_default([hidden, entry("gpt-6-astra")],
                                                     server.parse_codex_models([hidden, entry("gpt-6-astra")])))

    def test_refresh_stores_the_default(self):
        entries = [entry("gpt-6-astra"), entry(NEW_MODEL, default=True)]
        with (
            patch.object(server, "codex_binary_path", return_value="/usr/local/bin/codex"),
            patch.object(server, "read_codex_models", return_value=entries),
        ):
            server.refresh_models()
        self.assertEqual(server.live_default(), server.CodexDefault(NEW_MODEL, "medium"))

    def test_the_live_default_comes_first(self):
        self.seen.return_value = "gpt-6-astra"
        self.use_live(SOL_LIVE, server.CodexDefault("gpt-6.1-sol", "low"))
        self.assertEqual(server.default_model({}), "gpt-6.1-sol")
        self.assertEqual(server.turn_model("default"), {"model": "gpt-6.1-sol"})

    def test_then_the_model_status_named(self):
        self.seen.return_value = "gpt-6-astra"
        self.use_live(SOL_LIVE)
        self.assertEqual(server.default_model({}), "gpt-6-astra")
        self.assertEqual(server.turn_model("default"), {"model": "gpt-6-astra"})

    def test_then_the_newest_sol_in_the_live_list(self):
        self.use_live(SOL_LIVE)
        # Versions compare as numbers: 7.10 is newer than 7.9.
        self.assertEqual(server.default_model({}), "gpt-7.10-sol")
        self.assertEqual(server.newest_sol_model(LIVE), "")

    def test_then_the_first_built_in_model(self):
        self.assertEqual(server.default_model({}), "gpt-6.2-sol")
        self.assertEqual(server.CLI_DEFAULT_MODEL, server.CHAT_MODELS[0][0])
        # Without a pick from Codex the turn names no model and Codex chooses, as before.
        self.assertEqual(server.turn_model("default"), {})

    def test_configuration_choices_match_the_built_in_models(self):
        config = server.yaml.safe_load((SERVER_PATH.parent / "config.yaml").read_text())
        models = [model for model, _, _ in server.CHAT_MODELS]
        self.assertEqual(config["schema"]["model_choice"], "list(" + "|".join(["account_default", *models, "custom"]) + ")")
        self.assertEqual(config["options"]["model_choice"], "account_default")

    def test_configuration_choice_for_display_and_for_a_turn(self):
        self.use_live(SOL_LIVE, server.CodexDefault("gpt-6.1-sol", "low"))
        for options, display, execution in (
            ({"model_choice": "account_default"}, "gpt-6.1-sol", "default"),
            ({"model_choice": "custom", "codex_model": ""}, "gpt-6.1-sol", "default"),
            ({"model_choice": "custom", "codex_model": NEW_MODEL}, NEW_MODEL, NEW_MODEL),
            ({"model_choice": "gpt-6-sol", "codex_model": NEW_MODEL}, "gpt-6-sol", "gpt-6-sol"),
            ({"model_choice": "account_default", "codex_model": NEW_MODEL}, NEW_MODEL, NEW_MODEL),
            ({"model_choice": "gpt-6-sol", "model_pin": NEW_MODEL}, NEW_MODEL, NEW_MODEL),
        ):
            with self.subTest(options=options):
                self.assertEqual(server.default_model(options), display)
                self.assertEqual(server.resolve_chat_settings({}, options)["model"], execution)

    def test_old_stored_options_load_and_suggest_the_dropdown_at_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            options_file = Path(folder) / "options.json"
            options_file.write_text('{"codex_model": "  gpt-9.9-nova  "}')
            with patch.object(server, "OPTIONS_PATH", options_file):
                options = server.read_options()
        self.assertEqual(server.resolve_chat_settings({}, options)["model"], NEW_MODEL)
        with patch("builtins.print") as log:
            server.log_legacy_model_choice(options)
            server.resolve_chat_settings({}, options)
            server.default_model(options)
            for choice in ("custom", "gpt-6-sol"):
                server.log_legacy_model_choice(dict(options, model_choice=choice))
            server.log_legacy_model_choice(server.DEFAULT_OPTIONS)
        log.assert_called_once()
        self.assertIn("drop-down", log.call_args.args[0])

    def test_the_option_or_the_pin_still_wins(self):
        self.use_live(SOL_LIVE, server.CodexDefault("gpt-6.1-sol", "low"))
        self.assertEqual(server.default_model({"codex_model": "gpt-6-astra"}), "gpt-6-astra")
        run = server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, {"model_pin": "gpt-6-astra"})
        self.assertEqual(run, {"model": "gpt-6-astra", "reasoning_effort": "medium"})

    def test_a_chat_on_default_takes_the_account_level_at_the_shipped_option(self):
        self.use_live(SOL_LIVE, server.CodexDefault("gpt-6.1-sol", "low"))
        run = server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, {})
        self.assertEqual(run, {"model": "default", "reasoning_effort": "low"})
        # The owner's own level wins, and a chat that chose a model keeps the option's level.
        run = server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, {"model_reasoning_effort": "high"})
        self.assertEqual(run["reasoning_effort"], "high")
        run = server.resolve_chat_settings({"model": "gpt-6-astra", "reasoning_effort": None}, {})
        self.assertEqual(run["reasoning_effort"], "medium")
        # Before the list is read, medium as before.
        server.model_list_state["default"] = None
        self.assertEqual(server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, {})["reasoning_effort"], "medium")

    def test_chat_options_give_the_reset_its_target(self):
        self.use_live(SOL_LIVE, server.CodexDefault("gpt-6.1-sol", "low"))
        with (
            patch.object(server, "read_options", return_value={}),
            patch.object(server, "api_token", return_value="test-token"),
        ):
            options = server.app.test_client().get(
                "/chat-options", headers={"Authorization": "Bearer test-token"}).json
        self.assertEqual(options["default_model"], "gpt-6.1-sol")
        self.assertEqual(options["defaults"], {"model": "default", "reasoning_effort": "low"})
        self.assertEqual({model["default_effort"] for model in options["models"]}, {"medium"})

    def test_a_sign_out_forgets_the_account_default(self):
        self.use_live(SOL_LIVE, server.CodexDefault("gpt-6.1-sol", "low"))
        server.forget_default_model()
        self.assertIsNone(server.live_default())


if __name__ == "__main__":
    unittest.main()
