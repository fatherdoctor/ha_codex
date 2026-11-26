"""Which Home Assistant tools each way in to Codex gets: the chat page, the ChatGPT app and the codex_cli.start_task action.

WAYS_IN is the table in docs/app-reference.md, "Ways in". The tests build each way in's real launch arguments, start every tool
server those arguments register, ask it for its tool list, and compare the result with the table. A change that adds,
removes or moves a tool fails here until the table and the app reference say so, so a fix on one way in cannot pass as a fix on
all of them.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from chat_scripts import chat_source
from test_server import server
import scheduling

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "app-reference.md"
API_PATH = ROOT / "custom_components" / "codex_cli" / "api.py"

PAGE, APP, SERVICE = "Chat page", "ChatGPT app", "codex_cli.start_task"
NONE = ()

# The read tools of the bundled Home Assistant MCP server (option home_assistant_mcp, on by default), by kind.
MCP = "home_assistant_mcp."
MCP_FIND = tuple(MCP + tool for tool in (
    "get_entity", "list_entities", "get_entities_by_area", "search_entities_tool", "domain_summary_tool",
    "system_overview", "get_version"))
MCP_HISTORY = tuple(MCP + tool for tool in ("get_history", "get_history_range", "get_statistics",
                                            "get_statistics_range"))
MCP_SETUP = tuple(MCP + tool for tool in ("list_automations", "list_dashboards", "get_dashboard_config",
                                          "list_view_sections", "list_dashboard_backups", "get_error_log"))

# The tools of the same server that change something: Codex asks before each call ("prompt").
MCP_CONTROL = tuple(MCP + tool for tool in ("entity_action", "call_service_tool", "restart_ha"))
MCP_DASHBOARD = tuple(MCP + tool for tool in (
    "set_dashboard_config", "add_card", "update_card", "remove_card", "move_card", "add_view", "remove_view",
    "update_view", "restore_dashboard"))

# Each request kind and the tools that serve it on each way in. An empty tuple means not available.
WAYS_IN = {
    "Read an entity's state or find an entity": {
        PAGE: ("home_assistant.verify", *MCP_FIND), APP: ("home_assistant_relay.read", *MCP_FIND),
        SERVICE: ("home_assistant.verify", *MCP_FIND)},
    "Read history or long-term statistics": {
        PAGE: ("home_assistant_data.history", "home_assistant_data.statistics", *MCP_HISTORY), APP: MCP_HISTORY,
        SERVICE: ("home_assistant_data.history", "home_assistant_data.statistics", *MCP_HISTORY)},
    "Read automations, dashboards or the error log": {PAGE: MCP_SETUP, APP: MCP_SETUP, SERVICE: MCP_SETUP},
    "Control a device or call a service": {
        PAGE: ("home_assistant_call.call", *MCP_CONTROL), APP: ("home_assistant_call.call", *MCP_CONTROL),
        SERVICE: ("home_assistant_call.call", *MCP_CONTROL)},
    "Change a dashboard": {PAGE: MCP_DASHBOARD, APP: MCP_DASHBOARD, SERVICE: MCP_DASHBOARD},
    "Send a notification": {
        PAGE: ("home_assistant_call.call",), APP: ("home_assistant_call.call",), SERVICE: ("home_assistant_call.call",)},
    "Ask the owner a question or report the turn's state": {
        PAGE: ("home_assistant_data.ask_owner", "home_assistant_data.report_failure", "home_assistant_data.exchange_info"),
        APP: NONE,
        SERVICE: ("home_assistant_data.ask_owner", "home_assistant_data.report_failure", "home_assistant_data.exchange_info")},
    "Work with a GitHub repository": {
        PAGE: ("codex_github.github_git",), APP: NONE, SERVICE: ("codex_github.github_git",)},
}

WAYS_IN["Propose a schedule"] = {
    PAGE: ("codex_scheduling.propose_schedule", "codex_scheduling.list_schedules"),
    APP: NONE,
    SERVICE: ("codex_scheduling.propose_schedule", "codex_scheduling.list_schedules")}

# The app's own tool that changes the house: Codex asks for it each time, so only a launch that can ask gets it.
CALL_TOOL = "home_assistant_call.call"
# Every tool Codex asks about before each call ("prompt"); every other tool is approved ahead.
ASKING_TOOLS = {CALL_TOOL, *MCP_CONTROL, *MCP_DASHBOARD}

CONFIG = re.compile(r"mcp_servers\.([a-z_]+)\.(.+?)=(.*)", re.S)


def registered_servers(args: list[str]) -> dict[str, dict]:
    """Read the tool servers that a Codex command line registers, with their settings, from --config or -c."""
    servers: dict[str, dict] = {}
    for flag, value in zip(args, args[1:]):
        match = CONFIG.fullmatch(value) if flag in ("--config", "-c") else None
        if match:
            name, key, raw = match.groups()
            servers.setdefault(name, {})[key] = json.loads(raw)
    return servers


def listed_tools(settings: dict) -> list[str]:
    """Start one tool server as Codex starts it and return the names its tools/list answer gives."""
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    command = [settings["command"], *settings["args"]]
    lines = "".join(json.dumps(r) + "\n" for r in requests)
    result = subprocess.run(command, input=lines, capture_output=True, text=True, timeout=30, check=False)
    answers = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    listing = next((a for a in answers if a.get("id") == 2), None)
    if listing is None:
        raise AssertionError(f"{settings['args']} gave no tools/list answer: {result.stderr[-500:]}")
    return [tool["name"] for tool in listing["result"]["tools"]]


def offered_tools(settings: dict) -> list[str]:
    """The tools Codex offers from one server: its tools/list answer, cut to enabled_tools when the server has one.

    hass-mcp runs only in the image (/opt/hass-mcp), so for it the enabled_tools list stands in for its answer;
    test_ha_mcp_option.py checks that list against every tool of the pinned version.
    """
    if settings.get("args") == [server.hass_mcp.LAUNCHER]:
        return list(settings["enabled_tools"])
    tools = listed_tools(settings)
    allowed = settings.get("enabled_tools")
    return tools if allowed is None else [tool for tool in tools if tool in allowed]


def tools_of(args: list[str]) -> set[str]:
    """Every tool a launch makes available: server.tool, for each enabled server, each tool it lists."""
    tools = set()
    for name, settings in registered_servers(args).items():
        if settings.get("enabled") is not False:
            tools.update(f"{name}.{tool}" for tool in offered_tools(settings))
    return tools


def table_tools(way: str) -> set[str]:
    return {tool for row in WAYS_IN.values() for tool in row[way]}


def page_args() -> list[str]:
    """The arguments of a chat page turn; a scheduled run starts with the same ones (tests/test_sched_parity.py)."""
    return server.build_app_server_args()


def app_args() -> list[str]:
    """The relay's full command line when the ChatGPT app starts a session, built by Supervisor._command.

    The namespace probe is patched to succeed and the supervisor's state is restored afterward. Reading the whole
    command line, not only tool_args_factory, means a tool server added in remote_control.py also shows here.
    """
    supervisor = server.remote_control_supervisor
    probe = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    with patch.multiple(supervisor, run=probe, status={}, _name_supported=None, _checked_name=None):
        return supervisor._command("app-server", "--listen", "unix:///tmp/socket", "--remote-control")


def task_started_by_service(prompt: str) -> dict:
    """Post the request that codex_cli.start_task sends to the worker's /tasks, and return the task it opens.

    start_background_task is patched, so no Codex process starts; the task is removed from the worker afterward.
    """
    started = MagicMock()
    with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
        root = Path(temp_dir)
        stack.enter_context(patch.object(server.store, "task_dir", side_effect=lambda task_id: root / task_id))
        stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        stack.enter_context(patch.object(server, "start_background_task", started))
        stack.enter_context(patch.object(server.store, "save_index"))
        for name in ("fire_ha_event", "notify", "refresh_usage_status_async"):
            stack.enter_context(patch.object(server, name))
        response = server.app.test_client().post(
            "/tasks", headers={"Authorization": "Bearer test-token"}, json={"prompt": prompt})
    payload = response.get_json()
    if not payload.get("ok"):
        raise AssertionError(f"/tasks refused the service request: {payload}")
    task_id = payload["task_id"]
    with server.lock:
        task = server.tasks.pop(task_id)
        server.active_task_runners.discard(task_id)
    started.assert_called_once_with(task_id, prompt)
    return task


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

    def test_the_app_asks_for_each_change_and_approves_only_the_read_tools(self):
        """In the app as on the page, every tool that changes something asks each time ("prompt"); reads are approved ahead."""
        for name, settings in registered_servers(app_args()).items():
            for tool in offered_tools(settings):
                with self.subTest(tool=f"{name}.{tool}"):
                    expected = "prompt" if f"{name}.{tool}" in ASKING_TOOLS else "approve"
                    self.assertEqual(settings.get(f"tools.{tool}.approval_mode"), expected)

    def test_every_page_tool_is_preapproved_except_the_tools_that_change_something(self):
        """A read tool is approved ahead; a tool that changes something asks each time ("prompt"), so the person is the gate."""
        for name, settings in registered_servers(page_args()).items():
            for tool in offered_tools(settings):
                with self.subTest(tool=f"{name}.{tool}"):
                    expected = "prompt" if f"{name}.{tool}" in ASKING_TOOLS else "approve"
                    self.assertEqual(settings.get(f"tools.{tool}.approval_mode"), expected)

    def test_a_task_from_the_service_action_launches_with_the_page_tools(self):
        """The task that the service's request opens on /tasks is a page task, so it gets the page's tools.

        Every task starts with the same launch. The request that the service sends opens a task that is not a
        scheduled run, so it also runs in its own permission mode and notifies like a chat on the page.
        """
        task = task_started_by_service("Turn on a light")
        self.assertNotIn(task.get("task_source"), {"schedule", "lead"})
        with patch.dict(server.tasks, {task["task_id"]: task}, clear=True):
            self.assertFalse(server.background_task(task["task_id"]))
        self.assertEqual(tools_of(page_args()), self.page)

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
        self.assertRegex(chat_source(), r':\s*"tasks"')
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
