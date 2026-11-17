"""Which Home Assistant tools each way in to Codex gets: the chat page, the ChatGPT app and the codex_cli.start_task action.

WAYS_IN is the table in DOCS.md, "Ways in". The tests build each way in's real launch arguments, start every tool
server those arguments register, ask it for its tool list, and compare the result with the table. A change that adds,
removes or moves a tool fails here until the table and DOCS.md say so, so a fix on one way in cannot pass as a fix on
all of them.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from test_server import server

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "codex-cli-worker" / "DOCS.md"
CHAT_JS = ROOT / "codex-cli-worker" / "web" / "assets" / "chat.js"
API_PATH = ROOT / "custom_components" / "codex_cli" / "api.py"

PAGE, APP, SERVICE = "Chat page", "ChatGPT app", "codex_cli.start_task"
NONE = ()

# Each request kind and the tools that serve it on each way in. An empty tuple means not available.
WAYS_IN = {
    "Read an entity's state or find an entity": {
        PAGE: ("home_assistant.verify",), APP: ("home_assistant_relay.read",), SERVICE: ("home_assistant.verify",)},
    "Read history or long-term statistics": {
        PAGE: ("home_assistant_data.history", "home_assistant_data.statistics"), APP: NONE,
        SERVICE: ("home_assistant_data.history", "home_assistant_data.statistics")},
    "Control a device or call a service": {
        PAGE: ("home_assistant_actions.act",), APP: NONE, SERVICE: ("home_assistant_actions.act",)},
    "Send a notification": {
        PAGE: ("home_assistant_actions.act",), APP: NONE, SERVICE: ("home_assistant_actions.act",)},
    "Ask the owner a question or report the turn's state": {
        PAGE: ("home_assistant_data.ask_owner", "home_assistant_data.report_failure", "home_assistant_data.exchange_info"),
        APP: NONE,
        SERVICE: ("home_assistant_data.ask_owner", "home_assistant_data.report_failure", "home_assistant_data.exchange_info")},
    "Propose a schedule or use the mailbox": {
        PAGE: ("codex_scheduling.propose_schedule", "codex_scheduling.list_schedules",
               "codex_scheduling.read_mailbox", "codex_scheduling.post_mailbox"),
        APP: NONE,
        SERVICE: ("codex_scheduling.propose_schedule", "codex_scheduling.list_schedules",
                  "codex_scheduling.read_mailbox", "codex_scheduling.post_mailbox")},
    "Work with a GitHub repository": {
        PAGE: ("codex_github.github_git",), APP: NONE, SERVICE: ("codex_github.github_git",)},
}

CONFIG = re.compile(r"mcp_servers\.([a-z_]+)\.(.+?)=(.*)", re.S)


def registered_servers(args: list[str]) -> dict[str, dict]:
    """Read the tool servers that a Codex command line registers, with their settings."""
    servers: dict[str, dict] = {}
    for flag, value in zip(args, args[1:]):
        match = CONFIG.fullmatch(value) if flag == "--config" else None
        if match:
            name, key, raw = match.groups()
            servers.setdefault(name, {})[key] = json.loads(raw)
    return servers


def listed_tools(settings: dict) -> list[str]:
    """Start one tool server as Codex starts it and return the names its tools/list answer gives."""
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    result = subprocess.run([settings["command"], *settings["args"]], input="".join(json.dumps(r) + "\n" for r in requests),
                            capture_output=True, text=True, timeout=30, check=False)
    answers = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    listing = next((a for a in answers if a.get("id") == 2), None)
    if listing is None:
        raise AssertionError(f"{settings['args']} gave no tools/list answer: {result.stderr[-500:]}")
    return [tool["name"] for tool in listing["result"]["tools"]]


def tools_of(args: list[str]) -> set[str]:
    """Every tool a launch makes available: server.tool, for each enabled server, each tool it lists."""
    tools = set()
    for name, settings in registered_servers(args).items():
        if settings.get("enabled") is not False:
            tools.update(f"{name}.{tool}" for tool in listed_tools(settings))
    return tools


def table_tools(way: str) -> set[str]:
    return {tool for row in WAYS_IN.values() for tool in row[way]}


def page_args() -> list[str]:
    """The arguments of a chat page turn; the run picks one of these two launches by the approval option."""
    return server.build_app_server_args()


def exec_args() -> list[str]:
    task = {"task_id": "t", "turns": [], "chat_settings": server.DEFAULT_CHAT_SETTINGS}
    with patch.object(server, "tasks", {"t": task}), patch.object(server, "read_options", return_value={}):
        return server.build_codex_args("t", Path("prompt"), Path("final"), None)


def app_args() -> list[str]:
    """The tool arguments the remote-control relay adds when the ChatGPT app starts a session."""
    return server.remote_control_supervisor.tool_args_factory()


def docs_row(request: str) -> str:
    def cell(tools):
        return ", ".join(f"`{tool}`" for tool in tools) if tools else "not available"
    row = WAYS_IN[request]
    return f"| {request} | {cell(row[PAGE])} | {cell(row[APP])} | {cell(row[SERVICE])} |"


class WaysInTests(unittest.TestCase):
    """The table of request kinds against ways in matches the tools each way in really gets."""

    @classmethod
    def setUpClass(cls):
        cls.page = tools_of(page_args())
        cls.app = tools_of(app_args())

    def test_the_chat_page_gets_exactly_the_tools_in_the_table(self):
        self.assertEqual(self.page, table_tools(PAGE))

    def test_the_chatgpt_app_gets_exactly_the_tools_in_the_table(self):
        self.assertEqual(self.app, table_tools(APP))

    def test_both_page_launches_register_the_same_tools(self):
        """A turn runs through app-server or exec by an option, never by way in; both give the same tools."""
        self.assertEqual(tools_of(exec_args()), self.page)

    def test_every_page_tool_is_preapproved(self):
        """The page cannot show Codex's own request for a tool call, so a tool without approval_mode would fail there."""
        for name, settings in registered_servers(page_args()).items():
            for tool in listed_tools(settings):
                with self.subTest(tool=f"{name}.{tool}"):
                    self.assertEqual(settings.get(f"tools.{tool}.approval_mode"), "approve")

    def test_the_service_action_starts_the_same_task_as_the_page(self):
        """codex_cli.start_task posts to the worker's /tasks, the route the page uses, so it gets the page's tools."""
        spec = importlib.util.spec_from_file_location("codex_api_matrix", API_PATH)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        response = MagicMock(status=200, json=AsyncMock(return_value={"ok": True}))
        context = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
        session = MagicMock()
        session.request.return_value = context
        asyncio.run(api.CodexCliApiClient(session, "http://worker", "token").start_task("Turn on a light"))
        self.assertEqual(session.request.call_args.args, ("POST", "http://worker/tasks"))
        self.assertRegex(CHAT_JS.read_text(encoding="utf-8"), r':\s*"tasks"')
        for request, row in WAYS_IN.items():
            with self.subTest(request=request):
                self.assertEqual(row[SERVICE], row[PAGE])

    def test_docs_hold_the_same_table(self):
        text = DOCS.read_text(encoding="utf-8")
        section = text.split("\n## Ways in\n", 1)[1].split("\n#", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| Request")
                and not line.startswith("|---")]
        self.assertEqual(rows, [docs_row(request) for request in WAYS_IN])


if __name__ == "__main__":
    unittest.main()
