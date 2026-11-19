"""Shared helpers for the compatibility drivers in this folder: settings, the lab's Home Assistant, the app.

Each driver is one step of the compatibility run (README.md in this folder). A runner starts it as
`python3 <driver> --core <version>` and reads its exit code:

    0   PASS
    77  NOT RUN: the step could not run here (no lab settings, no sign-in, no browser); the last line says why
    any other code, or a timeout, is a FAIL

The drivers talk to a lab Home Assistant only, never to a home that people live in. Before doing anything, each
driver checks that the instance is marked as a lab (see `require_lab`).

Settings come from environment variables, so nothing about a particular lab is in this repository:

    COMPAT_HA_URL          the lab's Home Assistant address, for example http://compat-lab.local:8123
    COMPAT_HA_TOKEN_FILE   a file that holds a long-lived access token of an administrator on the lab
    COMPAT_ADDON_REPO      the app repository to install from (default: this fork on GitHub)
    COMPAT_LAB_SHELL       a command that runs `sh` on the lab with the configuration folder at /config,
                           for example "ssh compat-lab"; only the file-edit step needs it
    COMPAT_RUN_DIR         a folder for screenshots and console logs (the runner sets it)
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

import requests
import websocket

PASS = 0
FAIL = 1
NOT_RUN = 77

DEFAULT_ADDON_REPO = "https://github.com/fatherdoctor/ha_codex"
# The app's slug in config.yaml. The store adds a prefix made from the repository, so the drivers match the end.
ADDON_SLUG_SUFFIX = "_codex_cli_worker"
INTEGRATION_DOMAIN = "codex_cli"
# The entity that marks a Home Assistant instance as a compatibility lab (README.md, "The lab").
LAB_MARKER_ENTITY = "input_boolean.compat_lab"

REQUEST_TIMEOUT = 30
# What a call to the lab raises when the lab or the connection goes away (a restart closes both).
CONNECTION_ERRORS = (OSError, RuntimeError, websocket.WebSocketException)


class StepResult(Exception):
    """Ends a step with an exit code and one line that says why."""

    def __init__(self, code: int, why: str) -> None:
        """Keep the exit code and the reason line."""
        super().__init__(why)
        self.code = code
        self.why = why


def not_run(why: str) -> StepResult:
    """A NOT RUN result: the step could not run here."""
    return StepResult(NOT_RUN, why)


def failed(why: str) -> StepResult:
    """A FAIL result: the pass rule did not hold."""
    return StepResult(FAIL, why)


def run_step(name: str, step: Callable[[Lab, argparse.Namespace], str]) -> None:
    """Parse the runner's arguments, run one step and exit with its code; the last line printed says why."""
    parser = argparse.ArgumentParser(description=f"Compatibility step: {name}")
    parser.add_argument(
        "--core",
        default="base",
        help="the Core version the runner moved the lab to (for the log)",
    )
    args = parser.parse_args()
    try:
        lab = Lab.from_environment()
        require_lab(lab)
        why = step(lab, args)
        code = PASS
    except StepResult as result:
        code, why = result.code, result.why
    # A crash in a driver is a FAIL with its type, never a PASS.
    except Exception as exc:  # noqa: BLE001
        code, why = FAIL, f"the driver stopped: {type(exc).__name__}: {exc}"
    label = {PASS: "PASS", NOT_RUN: "NOT RUN"}.get(code, "FAIL")
    print(f"{label}: {why}", flush=True)  # noqa: T201 - the runner reads the result from stdout
    sys.exit(code)


def wait_for(check: Callable[[], Any], timeout: float, interval: float = 5.0) -> Any:
    """Call `check` until it returns a true value or `timeout` seconds pass; return the last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def run_dir() -> Path:
    """The folder for this step's screenshots and logs: COMPAT_RUN_DIR, or the current folder."""
    folder = Path(os.environ.get("COMPAT_RUN_DIR") or ".")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class Lab:
    """The lab's Home Assistant: its REST API, its WebSocket API and, through them, the Supervisor and the app."""

    def __init__(self, url: str, token: str, addon_repo: str, shell: list[str]) -> None:
        """Keep the lab's address, its access token, the app repository and the lab shell command."""
        self.url = url.rstrip("/")
        self.token = token
        self.addon_repo = addon_repo.rstrip("/")
        self.shell = shell
        self._message_id = 0

    @classmethod
    def from_environment(cls) -> Lab:
        """Read the settings from the environment; a missing one is NOT RUN."""
        url = os.environ.get("COMPAT_HA_URL", "").strip()
        token_file = os.environ.get("COMPAT_HA_TOKEN_FILE", "").strip()
        if not url:
            raise not_run("COMPAT_HA_URL is not set")
        if not token_file:
            raise not_run("COMPAT_HA_TOKEN_FILE is not set")
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise not_run(f"cannot read COMPAT_HA_TOKEN_FILE: {exc.strerror}") from exc
        if not token:
            raise not_run("COMPAT_HA_TOKEN_FILE is empty")
        repo = os.environ.get("COMPAT_ADDON_REPO", "").strip() or DEFAULT_ADDON_REPO
        shell = shlex.split(os.environ.get("COMPAT_LAB_SHELL", ""))
        return cls(url, token, repo, shell)

    # ---- Home Assistant's REST API ----

    def rest(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        text: bool = False,
    ) -> Any:
        """Call the lab's REST API; return the JSON answer, or the text when `text` is true."""
        response = requests.request(
            method,
            self.url + path,
            json=body,
            timeout=REQUEST_TIMEOUT,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()
        return response.text if text else response.json()

    def is_up(self) -> bool:
        """True when Home Assistant answers its API, as it does again after a restart."""
        try:
            return self.rest("GET", "/api/") == {"message": "API running."}
        except (requests.RequestException, ValueError):
            return False

    def state(self, entity_id: str) -> str | None:
        """The state of one entity, or None when Home Assistant has no such entity."""
        try:
            return self.rest("GET", f"/api/states/{entity_id}")["state"]
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    # ---- Home Assistant's WebSocket API ----

    def ws(
        self, message_type: str, timeout: float = REQUEST_TIMEOUT, **fields: Any
    ) -> Any:
        """Send one WebSocket command on a new connection and return its result."""
        ws_url = "ws" + self.url[len("http") :] + "/api/websocket"
        connection = websocket.create_connection(ws_url, timeout=timeout)
        try:
            connection.recv()  # auth_required
            connection.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(connection.recv()).get("type") != "auth_ok":
                raise failed("the lab refused the access token")
            self._message_id += 1
            connection.send(
                json.dumps({"id": self._message_id, "type": message_type, **fields})
            )
            while True:
                answer = json.loads(connection.recv())
                if (
                    answer.get("id") == self._message_id
                    and answer.get("type") == "result"
                ):
                    break
        finally:
            connection.close()
        if not answer.get("success"):
            error = answer.get("error") or {}
            raise RuntimeError(
                f"{message_type} failed: {error.get('code', '')} {error.get('message', '')}".strip()
            )
        return answer.get("result")

    def supervisor(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        """Call the Supervisor through Home Assistant, as the frontend does."""
        fields: dict[str, Any] = {
            "endpoint": endpoint,
            "method": method,
            "timeout": timeout,
        }
        if data is not None:
            fields["data"] = data
        return self.ws("supervisor/api", timeout=timeout + 10, **fields)

    # ---- the app ----

    def addon_slug(self) -> str | None:
        """The store slug of the app from COMPAT_ADDON_REPO, or None when that repository is not in the store."""
        store = self.supervisor("get", "/store")
        repositories = {
            repo.get("url", "").rstrip("/"): repo.get("slug")
            for repo in store.get("repositories", [])
        }
        repository = repositories.get(self.addon_repo)
        for addon in store.get("addons", []):
            if addon.get("repository") == repository and str(
                addon.get("slug", "")
            ).endswith(ADDON_SLUG_SUFFIX):
                return addon["slug"]
        return None

    def addon_info(self, slug: str) -> dict[str, Any]:
        """The Supervisor's information on the installed app (state, version, Ingress entry)."""
        return self.supervisor("get", f"/addons/{slug}/info")

    def addon_log(self, slug: str) -> str:
        """The app's log as the Supervisor keeps it."""
        return self.rest("GET", f"/api/hassio/addons/{slug}/logs", text=True)

    def worker(self, slug: str) -> Worker:
        """The app's own API, reached through Ingress like the chat page (no worker token needed or used)."""
        entry = self.addon_info(slug).get("ingress_entry") or ""
        if not entry:
            raise failed("the app has no Ingress entry")
        session = self.supervisor("post", "/ingress/session")["session"]
        return Worker(self.url + entry, session)

    def config_entries(self, domain: str) -> list[dict[str, Any]]:
        """The config entries of one integration, with their states."""
        return self.ws("config_entries/get", domain=domain)

    # ---- the lab's shell ----

    def sh(self, script: str, timeout: float = REQUEST_TIMEOUT) -> str:
        """Run a short shell script on the lab through COMPAT_LAB_SHELL; return its output."""
        if not self.shell:
            raise not_run("COMPAT_LAB_SHELL is not set")
        done = subprocess.run(
            [*self.shell, "sh", "-s"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if done.returncode != 0:
            raise failed(
                f"the lab shell exited {done.returncode}: {done.stderr.strip()[-200:]}"
            )
        return done.stdout


class Worker:
    """The app's API through an Ingress session: the same requests the chat page makes."""

    def __init__(self, base_url: str, session: str) -> None:
        """Keep the app's Ingress address and the Ingress session that admits the requests."""
        self.base_url = base_url.rstrip("/")
        self.session = session

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call the app's API through Ingress and return its JSON answer."""
        response = requests.request(
            method,
            self.base_url + path,
            json=body,
            timeout=REQUEST_TIMEOUT,
            cookies={"ingress_session": self.session},
        )
        response.raise_for_status()
        return response.json()

    def wait_for_task(self, task_id: str, timeout: float) -> dict[str, Any]:
        """Poll a chat until its turn ends or asks for permission; return the task as the API gives it."""

        def ended() -> dict[str, Any] | None:
            task = self.call("GET", f"/tasks/{task_id}")["task"]
            if task.get("permission_required") or task.get("status") not in {
                "queued",
                "running",
                "in_queue",
            }:
                return task
            return None

        task = wait_for(ended, timeout, interval=3)
        if not task:
            self.cancel(task_id)
            raise failed(f"chat {task_id} did not end within {int(timeout)} seconds")
        if task.get("permission_required"):
            self.cancel(
                task_id
            )  # Leave the app idle for the next step; the driver judges the request.
        return task

    def cancel(self, task_id: str) -> None:
        """Stop a chat, so the app is idle for the next step."""
        self.call("POST", f"/tasks/{task_id}/cancel")


def require_lab(lab: Lab) -> None:
    """Refuse to touch an instance that is not marked as a lab.

    The steps install and remove an app, restart Core and edit a file under /config. A wrong COMPAT_HA_URL must
    not do that to a home, so the instance has to carry the lab marker that only a lab's configuration creates.
    """
    if not lab.is_up():
        raise failed(f"Home Assistant at {lab.url} does not answer")
    if lab.state(LAB_MARKER_ENTITY) != "on":
        raise not_run(
            f"this Home Assistant is not marked as a lab ({LAB_MARKER_ENTITY} is not on)"
        )


def error_lines(log: str) -> list[str]:
    """The lines of a log that report an error."""
    return [
        line
        for line in log.splitlines()
        if " ERROR " in f" {line} " or "[ERROR]" in line
    ]
