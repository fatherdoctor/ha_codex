#!/usr/bin/env python3
"""cx-02 sign-in: the app's own check of the ChatGPT sign-in answers ok.

The runner restores the lab's sign-in file before this step. The app reports OpenAI's last answer for that sign-in
as `codex_login.server_check` in its status: "ok", "rejected" or "unchecked". The answer comes from a usage read,
which asks OpenAI. A status read starts a usage read only when the app's last one is older than its refresh interval
(`USAGE_REFRESH_INTERVAL_SECONDS` in server.py, 300 seconds), and the app has no route that forces one. cx-01 reads
the status before the runner restores the file, so the step reads the status until the answer is no longer
"unchecked", for longer than that interval.

An expired or missing sign-in is NOT RUN, not a FAIL: it says nothing about compatibility, and the person who owns
the lab signs in again. The runner then marks the later steps that need the sign-in NOT RUN too.
"""

from __future__ import annotations

import argparse
from typing import Any

import lab as compat

# The app's usage refresh interval (server.py), and how long the step waits: one full interval and a margin.
APP_USAGE_REFRESH_SECONDS = 300
CHECK_TIMEOUT = APP_USAGE_REFRESH_SECONDS + 30


def judge(login: dict[str, Any]) -> tuple[int, str]:
    """Turn the app's `codex_login` status into the step's result."""
    if not login.get("has_auth_file"):
        return (
            compat.NOT_RUN,
            "no sign-in file in the app (the runner restores it before this step)",
        )
    server_check = login.get("server_check")
    if server_check == "rejected":
        return (
            compat.NOT_RUN,
            "sign-in expired: OpenAI refused it; sign in again on the lab",
        )
    if not login.get("status_ok"):
        return (
            compat.FAIL,
            f"the Codex CLI does not accept the sign-in file: {login.get('message') or 'no message'}",
        )
    if server_check == "ok":
        return compat.PASS, "the app's server check answered ok"
    return (
        compat.FAIL,
        f"the app's server check did not answer within {CHECK_TIMEOUT} seconds ({server_check})",
    )


def step(lab: compat.Lab, args: argparse.Namespace) -> str:
    """Read the app's status until the server check answers, then judge it."""
    slug = lab.addon_slug()
    if slug is None:
        raise compat.failed("the app is not in the store; cx-01 installs it")
    worker = lab.worker(slug)

    def answered() -> dict[str, Any] | None:
        login = worker.call("GET", "/status")["codex_login"]
        return (
            login
            if login.get("server_check") != "unchecked"
            or not login.get("has_auth_file")
            else None
        )

    login = (
        compat.wait_for(answered, CHECK_TIMEOUT)
        or worker.call("GET", "/status")["codex_login"]
    )
    code, why = judge(login)
    if code != compat.PASS:
        raise compat.StepResult(code, why)
    return why


if __name__ == "__main__":
    compat.run_step("cx-02 sign-in", step)
