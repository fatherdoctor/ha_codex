#!/usr/bin/env python3
"""cx-03 chat task: one fixed prompt on the chat page, in a headless browser, answers with a fixed word.

Passes when the word arrives on the page, the turn ends as completed, and the page logged no console error.

The prompt spells the word in two halves, so the word on the page can only come from the answer, not from the
prompt that the page also shows. The step needs Playwright for Python (`pip install playwright`, then
`playwright install chromium`); without it the step is NOT RUN.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import lab as compat

WORD = "PERIWINKLE"
PROMPT = (
    "This is an automated compatibility check. Reply with one word only: the letters PERI followed by the "
    "letters WINKLE, written together in capitals. Do not run commands, read files or change anything."
)
TURN_TIMEOUT = 150
PAGE_TIMEOUT_MS = 30_000


def console_problems(messages: list[tuple[str, str]]) -> list[str]:
    """The console messages that count against the step: errors and uncaught page errors."""
    return [text for kind, text in messages if kind in {"error", "pageerror"}]


def step(lab: compat.Lab, args: argparse.Namespace) -> str:
    """Send the prompt from the chat page and wait for the word, the end of the turn and the console."""
    try:
        # Optional: only this step needs a browser, so a lab without one reports NOT RUN.
        from playwright.sync_api import (  # noqa: PLC0415
            TimeoutError as PlaywrightTimeout,
            sync_playwright,
        )
    except ImportError as exc:
        raise compat.not_run("Playwright for Python is not installed") from exc
    slug = lab.addon_slug()
    if slug is None:
        raise compat.failed("the app is not in the store; cx-01 installs it")
    worker = lab.worker(slug)
    console: list[tuple[str, str]] = []
    shot = compat.run_dir() / "cx-03-chat.png"

    with sync_playwright() as playwright:
        # The package can be present without its browser; that is a lab that cannot run the step, not a failure.
        if not Path(playwright.chromium.executable_path).is_file():
            raise compat.not_run(
                "the Chromium browser for Playwright is not installed (playwright install chromium)"
            )
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_cookies(
                [{"name": "ingress_session", "value": worker.session, "url": lab.url}]
            )
            page = context.new_page()
            page.on(
                "console", lambda message: console.append((message.type, message.text))
            )
            page.on(
                "pageerror", lambda error: console.append(("pageerror", str(error)))
            )
            page.goto(worker.base_url + "/", wait_until="networkidle")
            page.fill("#message", PROMPT)
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/tasks")
                ),
                timeout=PAGE_TIMEOUT_MS,
            ) as started:
                page.click("#send")
            task_id = started.value.json().get("task_id")
            if not task_id:
                raise compat.failed(
                    f"the page did not start a chat: {started.value.json().get('error')}"
                )
            task = worker.wait_for_task(task_id, TURN_TIMEOUT)
            if task.get("status") != "completed":
                raise compat.failed(
                    f"the turn ended as {task.get('status')}: {task.get('summary') or task.get('error')}"
                )
            try:
                page.locator("#messages").get_by_text(WORD, exact=False).first.wait_for(
                    timeout=PAGE_TIMEOUT_MS
                )
            except PlaywrightTimeout as exc:
                raise compat.failed(
                    f"the word {WORD} did not appear on the page"
                ) from exc
            finally:
                page.screenshot(path=str(shot), full_page=True)
        finally:
            browser.close()

    problems = console_problems(console)
    if problems:
        for text in problems[:20]:
            print(f"console: {text}")  # noqa: T201 - kept in the step's log
        raise compat.failed(f"the page logged {len(problems)} console error(s)")
    return f"the answer {WORD} arrived on the page and the turn completed (chat {task_id}); screenshot {shot.name}"


if __name__ == "__main__":
    compat.run_step("cx-03 chat task", step)
