"""The add-on's GitHub sign-in: the stored credential, the device sign-in, the access check and git for Codex.

The worker holds the credential in /data/github (0700, files 0600). Codex never reads it: the folder is masked
in every sandbox (codex-bwrap), and Codex reaches GitHub only through the `github_git` tool, which asks the
worker over a local socket. The worker runs git itself with an askpass helper, so the token is never in an
argument list, an environment, a remote URL or a log. Every push runs the bundled push guard first.

Nothing here runs a model. Only state names and HTTP status codes are logged, never a response body.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

# The Client ID of the public GitHub App "Codex for Home Assistant". It is public by design (GitHub's device
# sign-in needs nothing else); the add-on option `github_client_id` overrides it for an owner's own App.
GITHUB_CLIENT_ID = ""
GITHUB_CHECK_SECONDS = 3600
NEAR_EXPIRY_DAYS = 7
FAILURES_BEFORE_FAILED = 3
EXPECTED_PERMISSIONS = {"contents": "write", "metadata": "read"}
API = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
APP_REVOKE_URL = "https://github.com/settings/apps/authorizations"
TOKEN_REVOKE_URL = "https://github.com/settings/tokens"
HTTP_TIMEOUT = 10
GIT_TIMEOUT_SECONDS = 300
GIT_STALL_SECONDS = 60
REPLY_LIMIT = 8000
GUARD_PATH = "/usr/share/ha_codex/push_guard.py"
ASKPASS_PATH = "/usr/local/bin/codex-github-askpass"
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
ALLOW_MARK = "guard: allow"
OPERATIONS = ("repos", "fetch", "pull", "push", "clone", "link")
READ_ONLY_OPERATIONS = ("repos", "fetch")
BAD_STATES = ("near_expiry", "expired", "revoked", "access_changed", "failed")
STATE_WORDS = {
    "ok": "working", "near_expiry": "expires within 7 days", "expired": "expired",
    "revoked": "refused by GitHub (revoked)", "access_changed": "reaches different repositories or access",
    "failed": "cannot be checked", "disconnected": "not connected",
}

# F4: names Codex passes are checked before they reach git.
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")
TOKEN_RE = re.compile(r"^github_pat_[A-Za-z0-9_]{20,255}$")

# F1: the only repository settings a credentialed git command runs with. Anything else (a hook path, a filter,
# fsmonitor, a credential helper, a push URL, an include, a URL rewrite, a proxy) refuses the operation.
ALLOWED_CONFIG_KEYS = frozenset({
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates", "core.ignorecase",
    "core.precomposeunicode", "core.symlinks", "remote.origin.url", "remote.origin.fetch", "user.name",
    "user.email", "pull.ff", "pull.rebase",
})
BRANCH_CONFIG_RE = re.compile(r"^branch\.[^\n]+\.(?:remote|merge)$")
# Set for every git child through the environment, so the guard's own git calls get them too. They win over the
# repository's settings, so a setting planted after the check still cannot run a hook, a monitor or a helper.
CONFIG_OVERRIDES = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("credential.helper", ""),
    ("credential.useHttpPath", "false"),
    ("core.sshCommand", "false"),
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
    ("http.sslVerify", "true"),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
    ("push.recurseSubmodules", "no"),
    ("core.pager", "cat"),
    ("remote.origin.proxy", ""),
)
GUARD_ADVICE_RE = re.compile(r"^push_guard: \d+ finding\(s\); push refused\..*$", re.M)
GITHUB_TOKEN_RE = re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}")

# Scans exactly what the guard's --push route will read (its own hook_lines/pushed_ranges/changes), in a child
# process with the worker's git environment, and counts added lines that carry an allow mark (F3).
ALLOW_SCAN = r"""
import sys
sys.path.insert(0, sys.argv[1])
import push_guard as guard
repo, remote, spec = sys.argv[2], sys.argv[3], sys.argv[4]
lines, _ = guard.hook_lines(repo, remote, [spec])
ranges, _, _ = guard.pushed_ranges(repo, remote, lines)
hits = 0
for rng in ranges:
    for _path, _n, line in guard.changes(repo, rng):
        if isinstance(line, str) and "guard: allow" in line:
            hits += 1
print(hits)
"""


class GitHubError(Exception):
    """A fixed sentence for the owner or the agent; it never quotes a token or a response body."""


class Revoked(GitHubError):
    pass


class Unreachable(GitHubError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    for form in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text.replace("Z", "+0000") if form.endswith("%z") else text, form)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def valid_repository(value: Any) -> str:
    if not isinstance(value, str) or not REPOSITORY_RE.fullmatch(value) or ".." in value or value.endswith(".git"):
        raise GitHubError("Use a repository name of the form owner/repository.")
    return value


def valid_branch(value: Any) -> str:
    """A plain branch name: no leading +, :, - or dot, no .., no refspec or option."""
    if (not isinstance(value, str) or not BRANCH_RE.fullmatch(value) or ".." in value or "//" in value
            or value.endswith(("/", ".", ".lock")) or "/." in value or "@{" in value):
        raise GitHubError("Use a plain branch name (letters, digits, dot, dash, underscore and slash).")
    proc = subprocess.run(["git", "check-ref-format", "--branch", value], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=10, env=_plain_env())
    if proc.returncode != 0 or proc.stdout.strip() != value:
        raise GitHubError("Use a plain branch name (letters, digits, dot, dash, underscore and slash).")
    return value


def valid_folder(value: Any) -> str:
    if not isinstance(value, str) or not FOLDER_RE.fullmatch(value) or ".." in value or value.startswith("-"):
        raise GitHubError("Use one folder name: letters, digits, dot, dash and underscore; no slash, no .., "
                          "and no leading dot.")
    return value


def redact_token(text: str, token: str | None = None) -> str:
    if token:
        text = text.replace(token, "[redacted]")
    return GITHUB_TOKEN_RE.sub("[redacted]", text)


def _plain_env() -> dict[str, str]:
    """No worker secret and no user git configuration; only what git needs to run."""
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/nonexistent", "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0"}


class Store:
    """/data/github: credential.json (the token, 0600) and state.json (no secret)."""

    def __init__(self, root: Path | Callable[[], Path]):
        self._root = root
        self.lock = threading.RLock()

    @property
    def root(self) -> Path:
        return Path(self._root() if callable(self._root) else self._root)

    @property
    def credential_path(self) -> Path:
        return self.root / "credential.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def ensure(self) -> None:
        if self.root.is_symlink():
            raise OSError("The GitHub folder was replaced by a link.")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _write(self, path: Path, value: Any) -> None:
        self.ensure()
        temporary = path.with_name(path.name + ".tmp")
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            if path.is_symlink():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def write_credential(self, kind: str, token: str, expires_at: str | None) -> None:
        with self.lock:
            self._write(self.credential_path, {"kind": kind, "token": token, "created_at": iso(utc_now()),
                                               "expires_at": expires_at})

    def read_credential(self) -> dict[str, Any] | None:
        with self.lock:
            value = self._read(self.credential_path)
        if not value or not isinstance(value.get("token"), str) or value.get("kind") not in ("app", "token"):
            return None
        return value

    def read_token(self) -> str | None:
        credential = self.read_credential()
        return credential["token"] if credential else None

    def read_state(self) -> dict[str, Any]:
        with self.lock:
            return self._read(self.state_path) or {}

    def write_state(self, value: dict[str, Any]) -> None:
        with self.lock:
            self._write(self.state_path, value)

    def clear(self) -> None:
        with self.lock:
            for path in (self.credential_path, self.state_path,
                         self.credential_path.with_name("credential.json.tmp"),
                         self.state_path.with_name("state.json.tmp")):
                path.unlink(missing_ok=True)


class Http:
    """A small requests wrapper: no proxies from the environment, JSON only, bounded time."""

    def __init__(self, session=None):
        self._session = session

    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.trust_env = False
        return self._session

    def get(self, url, token, params=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "Authorization": "Bearer " + token, "User-Agent": "codex-for-home-assistant"}
        return self.session().get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT, allow_redirects=False)

    def post(self, url, data):
        headers = {"Accept": "application/json", "User-Agent": "codex-for-home-assistant"}
        return self.session().post(url, data=data, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=False)


def _json(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _paged(http, url, token, key=None, pages=10) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        response = http.get(url, token, params={"per_page": 100, "page": page})
        if response.status_code == 401:
            raise Revoked("GitHub refused the stored sign-in.")
        if response.status_code != 200:
            raise GitHubError(f"GitHub answered HTTP {response.status_code}.")
        body = _json(response)
        chunk = body.get(key) if key and isinstance(body, dict) else body
        if not isinstance(chunk, list):
            raise GitHubError("GitHub sent an answer the add-on cannot read.")
        items.extend(item for item in chunk if isinstance(item, dict))
        if len(chunk) < 100:
            break
    return items


def _repo_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    name = item.get("full_name")
    if not isinstance(name, str) or not REPOSITORY_RE.fullmatch(name):
        return None
    permissions = item.get("permissions") if isinstance(item.get("permissions"), dict) else {}
    return {"full_name": name, "private": bool(item.get("private")), "push": bool(permissions.get("push"))}


def inspect_access(http, token: str, kind: str, token_repository: str | None = None) -> dict[str, Any]:
    """Read what the credential reaches now. Raises Revoked on a 401, Unreachable on a network error.

    F2: repository `permissions` describe the signed-in user, not the credential, so `admin` is never used.
    For the App, "more" or "less" comes from the installation's permission map and the repository set; for a
    pasted token, from /user/repos (exactly one repository) and a probe of an Administration endpoint.
    """
    try:
        response = http.get(API + "/user", token)
        if response.status_code == 401:
            raise Revoked("GitHub refused the stored sign-in.")
        if response.status_code != 200:
            raise GitHubError(f"GitHub answered HTTP {response.status_code} for the account.")
        user = _json(response) or {}
        live: dict[str, Any] = {"account": {"login": str(user.get("login") or ""), "id": user.get("id")},
                                "expires_at": None, "permission_problem": None}
        if kind == "app":
            installations = _paged(http, API + "/user/installations", token, key="installations")
            live["installations"] = []
            permissions: dict[str, str] = {}
            repositories: dict[str, dict[str, Any]] = {}
            for installation in installations:
                inst_id = installation.get("id")
                if not isinstance(inst_id, int):
                    continue
                inst_permissions = installation.get("permissions") if isinstance(installation.get("permissions"), dict) else {}
                live["installations"].append({"id": inst_id, "account": str((installation.get("account") or {}).get("login") or ""),
                                              "permissions": dict(inst_permissions)})
                for name, level in inst_permissions.items():
                    permissions[str(name)] = str(level)
                for item in _paged(http, f"{API}/user/installations/{inst_id}/repositories", token, key="repositories"):
                    entry = _repo_entry(item)
                    if entry:
                        repositories[entry["full_name"].lower()] = entry
            live["permissions"] = permissions or None
            live["repositories"] = sorted(repositories.values(), key=lambda r: r["full_name"].lower())
            live["permission_problem"] = permission_problem(permissions if installations else None, live["repositories"])
            return live
        repository = valid_repository(token_repository)
        response = http.get(f"{API}/repos/{repository}", token)
        if response.status_code == 401:
            raise Revoked("GitHub refused the stored sign-in.")
        expiry = parse_time(response.headers.get("GitHub-Authentication-Token-Expiration", ""))
        live["expires_at"] = iso(expiry)
        if response.status_code != 200:
            live.update(repositories=[], permissions=None,
                        permission_problem=f"the token cannot read {repository} (HTTP {response.status_code})")
            return live
        entry = _repo_entry(_json(response) or {}) or {"full_name": repository, "private": True, "push": False}
        listed = {item.get("full_name", "").lower() for item in _paged(http, API + "/user/repos", token)}
        hooks = http.get(f"{API}/repos/{repository}/hooks", token)
        problems = []
        if listed - {repository.lower()}:
            problems.append("the token reaches more than one repository")
        if hooks.status_code not in (403, 404):
            problems.append("more than Contents read and write")
        if not entry["push"]:
            problems.append("read only")
        live["repositories"] = [entry]
        live["permissions"] = None
        live["permission_problem"] = "; ".join(problems) or None
        return live
    except (Revoked, GitHubError):
        raise
    except Exception as exc:  # requests' connection and timeout errors, without their text
        raise Unreachable("GitHub could not be reached.") from exc


def permission_problem(permissions: dict[str, str] | None, repositories: list[dict[str, Any]]) -> str | None:
    """The App's installation must hold exactly Contents write and Metadata read; the user must still write."""
    if permissions is None:
        return "the App is not installed on any repository"
    if permissions != EXPECTED_PERMISSIONS:
        extra = set(permissions) - set(EXPECTED_PERMISSIONS)
        if not extra and permissions.get("contents") == "read":
            return "read only"
        return "more than Contents read and write" if extra or permissions.get("contents") not in ("read", None) \
            else "less than Contents read and write"
    if any(not repo.get("push") for repo in repositories):
        return "read only"
    return None


def judge(live: dict[str, Any], expected: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Compare the live access with what the owner connected; return state, missing, extra and the reason."""
    now = now or utc_now()
    live_names = {repo["full_name"].lower(): repo["full_name"] for repo in live.get("repositories") or []}
    expected_names = {name.lower(): name for name in expected.get("repositories") or []}
    missing = sorted(expected_names[key] for key in set(expected_names) - set(live_names))
    extra = sorted(live_names[key] for key in set(live_names) - set(expected_names))
    expires = parse_time(live.get("expires_at"))
    problem = live.get("permission_problem")
    if expires and expires <= now:
        state, reason = "expired", "the sign-in has expired"
    elif missing or extra or problem:
        state = "access_changed"
        reason = problem or ("repositories added" if extra and not missing else "repositories removed")
    elif expires and expires - now <= timedelta(days=NEAR_EXPIRY_DAYS):
        state, reason = "near_expiry", "the sign-in expires within 7 days"
    else:
        state, reason = "ok", ""
    return {"state": state, "reason": reason, "missing": missing, "extra": extra}


class DeviceFlow:
    """GitHub's device sign-in for a GitHub App: only the public Client ID is used."""

    def __init__(self, http, client_id: str, on_success: Callable[[str], None],
                 sleep: Callable[[float], bool] | None = None):
        self.http = http
        self.client_id = client_id
        self.on_success = on_success
        self.cancelled = threading.Event()
        self.sleep = sleep or (lambda seconds: self.cancelled.wait(seconds))
        self.device_code = ""
        self.interval = 5
        self.deadline = 0.0
        self.public: dict[str, Any] = {"status": "idle", "user_code": "", "verification_uri": "", "expires_at": None,
                                       "error": ""}

    def start(self) -> dict[str, Any]:
        if not self.client_id:
            raise GitHubError("No GitHub App Client ID is set. Set the add-on option github_client_id.")
        response = self.http.post(DEVICE_CODE_URL, {"client_id": self.client_id})
        body = _json(response) if response.status_code == 200 else None
        if not isinstance(body, dict) or not body.get("device_code") or not body.get("user_code"):
            reason = body.get("error") if isinstance(body, dict) else ""
            raise GitHubError("GitHub did not start a sign-in" + (f" ({reason})" if reason in (
                "device_flow_disabled", "incorrect_client_credentials", "unauthorized_client") else "") + ".")
        self.device_code = str(body["device_code"])
        self.interval = max(int(body.get("interval") or 5), 1)
        expires_in = max(int(body.get("expires_in") or 900), 1)
        self.deadline = time.monotonic() + expires_in
        uri = str(body.get("verification_uri") or "https://github.com/login/device")
        if not uri.startswith("https://github.com/"):
            uri = "https://github.com/login/device"
        self.public = {"status": "waiting_for_user", "user_code": str(body["user_code"])[:16], "verification_uri": uri,
                       "expires_at": iso(utc_now() + timedelta(seconds=expires_in)), "error": ""}
        return dict(self.public)

    def finish(self, status: str, error: str = "") -> None:
        self.device_code = ""
        self.public = {**self.public, "status": status, "user_code": "" if status != "waiting_for_user" else
                       self.public.get("user_code", ""), "error": error}

    def poll_once(self) -> bool:
        """One poll; returns True while the flow should keep waiting."""
        if self.cancelled.is_set():
            self.finish("canceled")
            return False
        if time.monotonic() >= self.deadline:
            self.finish("expired", "The code expired. Press Connect for a new one.")
            return False
        try:
            response = self.http.post(ACCESS_TOKEN_URL, {"client_id": self.client_id, "device_code": self.device_code,
                                                         "grant_type": DEVICE_GRANT})
            body = _json(response)
        except Exception:
            return True  # a network hiccup: keep polling until the code expires
        body = body if isinstance(body, dict) else {}
        token = body.get("access_token")
        if isinstance(token, str) and token:
            try:
                self.on_success(token)
            except GitHubError as exc:
                self.finish("failed", str(exc))
                return False
            self.finish("completed")
            return False
        error = body.get("error")
        if error == "authorization_pending":
            return True
        if error == "slow_down":
            self.interval += 5
            return True
        if error == "expired_token":
            self.finish("expired", "The code expired. Press Connect for a new one.")
        elif error == "access_denied":
            self.finish("failed", "The sign-in was refused on GitHub.")
        elif error == "device_flow_disabled":
            self.finish("failed", "The GitHub App does not allow device sign-in.")
        else:
            self.finish("failed", "GitHub did not complete the sign-in.")
        return False

    def run(self) -> None:
        while not self.sleep(self.interval):
            if not self.poll_once():
                return
        self.finish("canceled")

    def cancel(self) -> None:
        self.cancelled.set()


class GitRunner:
    """Run git for the agent with the stored credential, only in a checked repository under /config."""

    def __init__(self, token_reader: Callable[[], str | None], connected: Callable[[], list[str]],
                 config_root: Path = Path("/config"), askpass: str = ASKPASS_PATH, guard: str = GUARD_PATH,
                 protected: tuple[str, ...] = (".storage", ".git/hooks"), python: str = sys.executable,
                 share_root: Path = Path("/share"), protected_share: tuple[str, ...] = ("HomeAssistant",)):
        self.token_reader = token_reader
        self.connected = connected
        self.config_root = Path(config_root)
        self.share_root = Path(share_root)
        self.protected_share = protected_share
        self.askpass = askpass
        self.guard = guard
        self.protected = protected
        self.python = python

    def env(self, credentialed: bool = True) -> dict[str, str]:
        env = _plain_env()
        env["GIT_HTTP_LOW_SPEED_LIMIT"] = "1000"
        env["GIT_HTTP_LOW_SPEED_TIME"] = str(GIT_STALL_SECONDS)
        if os.path.exists(CA_BUNDLE):
            env["GIT_SSL_CAINFO"] = CA_BUNDLE
        if credentialed:
            env["GIT_ASKPASS"] = self.askpass
            env["SSH_ASKPASS"] = self.askpass
        env["GIT_CONFIG_COUNT"] = str(len(CONFIG_OVERRIDES))
        for index, (key, value) in enumerate(CONFIG_OVERRIDES):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        return env

    def _git(self, repo: Path, *args: str, credentialed: bool = False, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args], env=self.env(credentialed), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def roots(self) -> list[tuple[Path, tuple[str, ...]]]:
        """The two places git work may happen, each resolved, with its protected folders."""
        return [(self.share_root.resolve(), self.protected_share), (self.config_root.resolve(), self.protected)]

    def inside(self, resolved: Path) -> Path:
        """Refuse a resolved path outside /share and /config, or in a protected folder; return its root."""
        for root, protected in self.roots():
            if resolved == root or root in resolved.parents:
                relative = resolved.relative_to(root).as_posix() if resolved != root else ""
                if any(relative == item or relative.startswith(item + "/") for item in protected):
                    raise GitHubError("That folder is protected.")
                return root
        raise GitHubError("The repository must be under /share or /config.")

    def repository_path(self, value: Any) -> Path:
        """A work tree under /share or /config (links resolved first) with a real .git folder, not protected."""
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise GitHubError("Give the path of a git work tree under /share or /config.")
        candidate = Path(value) if value.startswith("/") else self.config_root.resolve() / value
        resolved = Path(os.path.realpath(candidate))
        self.inside(resolved)
        dot_git = resolved / ".git"
        if dot_git.is_symlink() or not dot_git.is_dir():
            raise GitHubError("That folder is not a git work tree with its own .git folder.")
        proc = self._git(resolved, "rev-parse", "--show-toplevel")
        if proc.returncode != 0 or Path(proc.stdout.strip()).resolve() != resolved:
            raise GitHubError("That folder is not the top of a git work tree.")
        return resolved

    def check_config(self, repo: Path) -> None:
        """F1: refuse a repository whose settings could run a command, redirect a push or hand out the token."""
        env = _plain_env()
        proc = subprocess.run(["git", "-C", str(repo), "config", "--list", "--show-scope", "--includes", "-z"],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        if proc.returncode not in (0, 1):
            raise GitHubError("The repository's git settings could not be read.")
        refused: list[str] = []
        urls = 0
        fields = proc.stdout.split("\x00")
        if fields and fields[-1] == "":
            fields.pop()
        if len(fields) % 2:
            raise GitHubError("The repository's git settings could not be read.")
        # -z with --show-scope: "<scope>\0<key>\n<value>\0" per setting (no "\n<value>" for a bare key).
        for scope, entry in zip(fields[0::2], fields[1::2]):
            key, _, value = entry.partition("\n")
            lowered = key.lower()
            bad = False
            if scope not in ("local", "worktree") or lowered.startswith(("include.", "includeif.")) \
                    or lowered.endswith(".pushurl"):
                bad = True
            elif lowered in ALLOWED_CONFIG_KEYS or BRANCH_CONFIG_RE.fullmatch(lowered):
                if lowered in ("pull.rebase", "core.bare") and value.strip().lower() not in ("false", "0", "no", "off"):
                    bad = True
                if lowered == "remote.origin.url":
                    urls += 1
            else:
                bad = True
            if bad and key not in refused:
                refused.append(key)
        if urls > 1:
            refused.append("remote.origin.url")
        if refused:
            names = ", ".join(sorted(set(refused))[:12])
            raise GitHubError("This repository has git settings the add-on does not use with your GitHub sign-in: "
                              + names + ".")

    def origin_repository(self, repo: Path) -> str:
        """F1: the effective fetch and push URLs (rewrites applied) must both be a connected GitHub repository."""
        connected = {name.lower(): name for name in self.connected()}
        found = None
        for args in (("remote", "get-url", "origin"), ("remote", "get-url", "--push", "origin")):
            proc = self._git(repo, *args)
            urls = [line for line in proc.stdout.splitlines() if line.strip()]
            if proc.returncode != 0 or len(urls) != 1:
                raise GitHubError("The repository needs one origin remote at https://github.com/<owner>/<repository>.")
            match = re.fullmatch(r"https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?", urls[0].strip())
            if not match or match.group(1).lower() not in connected:
                raise GitHubError("The origin remote is not one of the connected GitHub repositories.")
            name = connected[match.group(1).lower()]
            if found and found != name:
                raise GitHubError("The origin remote is not one of the connected GitHub repositories.")
            found = name
        return found or ""

    def prepare(self, path: Any) -> tuple[Path, str]:
        repo = self.repository_path(path)
        self.check_config(repo)
        return repo, self.origin_repository(repo)

    def current_branch(self, repo: Path) -> str:
        proc = self._git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        if proc.returncode != 0:
            raise GitHubError("The repository has no branch checked out; name the branch.")
        return valid_branch(proc.stdout.strip())

    def _credentialed(self, repo: Path, *args: str) -> tuple[int, str]:
        token = self.token_reader()
        if not token:
            raise GitHubError("GitHub is not connected on this box; the owner connects it on the add-on page.")
        try:
            proc = self._git(repo, *args, credentialed=True, timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return 124, "git took too long and was stopped."
        return proc.returncode, redact_token((proc.stdout or "") + (proc.stderr or ""), token) + self.recheck(repo)

    def recheck(self, repo: Path) -> str:
        """The settings are read again after the run: a change made while git ran is reported, never silent."""
        try:
            self.check_config(repo)
        except GitHubError:
            return ("\nWarning: this repository's git settings changed while git ran; the add-on refuses it from "
                    "now on until they are removed.")
        return ""

    def connected_name(self, repository: Any) -> str:
        name = valid_repository(repository)
        connected = {item.lower(): item for item in self.connected()}
        if name.lower() not in connected:
            raise GitHubError("That repository is not one of the connected GitHub repositories.")
        return connected[name.lower()]

    def clone_target(self, name: str, folder: Any) -> Path:
        """/share/<folder>: one folder name (the repository name when none is given), never one that exists."""
        if folder is None or folder == "":
            folder = name.split("/", 1)[1]
        target = Path(os.path.realpath(self.share_root)) / valid_folder(folder)
        self.inside(target)
        if os.path.lexists(target):
            raise GitHubError("That folder already exists; choose a new folder name.")
        return target

    def clone(self, repository: Any, folder: Any = None) -> dict[str, Any]:
        """Clone a connected repository into a new folder /share/<folder>. No credential is written into it."""
        name = self.connected_name(repository)
        target = self.clone_target(name, folder)
        token = self.token_reader()
        if not token:
            raise GitHubError("GitHub is not connected on this box; the owner connects it on the add-on page.")
        try:
            # --template= gives the new repository no hook files; the overrides cover the rest.
            proc = subprocess.run(["git", "-C", str(target.parent), "clone", "--template=", "--origin", "origin",
                                   "--no-recurse-submodules", "--", f"https://github.com/{name}.git", target.name],
                                  env=self.env(True), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return reply(False, "git clone took too long and was stopped.")
        text = redact_token((proc.stdout or "") + (proc.stderr or ""), token)
        if proc.returncode != 0:
            return reply(False, "git clone failed.\n" + text)
        self.prepare(str(target))
        return {**reply(True, f"Cloned {name} into {target}.\n" + text), "path": str(target)}

    def link(self, repository: Any, path: Any) -> dict[str, Any]:
        """Point an existing work tree's origin at a connected repository. Only the plain https URL is set: no
        user name, no token and no credential helper are written into the repository."""
        name = self.connected_name(repository)
        repo = self.repository_path(path)
        self.check_config(repo)
        url = f"https://github.com/{name}.git"
        has_origin = self._git(repo, "remote", "get-url", "origin").returncode == 0
        proc = self._git(repo, "remote", "set-url" if has_origin else "add", "origin", url)
        if proc.returncode != 0:
            return reply(False, "The origin remote could not be set.")
        self.check_config(repo)
        self.origin_repository(repo)
        return {**reply(True, f"{repo} now uses {name} as origin."), "path": str(repo)}

    def fetch(self, path: Any) -> dict[str, Any]:
        repo, _ = self.prepare(path)
        code, text = self._credentialed(repo, "fetch", "--no-recurse-submodules", "--prune", "origin")
        return reply(code == 0, text or ("Fetched." if code == 0 else "git fetch failed."))

    def pull(self, path: Any, branch: Any = None) -> dict[str, Any]:
        """A credentialed fetch of the branch only. The worker never checks out or merges: a work-tree update runs
        filters and other repository settings as the worker, so the agent fast-forwards in its own sandbox."""
        repo, _ = self.prepare(path)
        name = valid_branch(branch) if branch is not None else self.current_branch(repo)
        code, text = self._credentialed(repo, "fetch", "--no-recurse-submodules", "origin",
                                        f"+refs/heads/{name}:refs/remotes/origin/{name}")
        if code != 0:
            return reply(False, text or "git fetch failed.")
        return reply(True, (text + "\n" if text else "") + f"Fetched origin/{name}. The worker does not update the "
                     f"work tree; run `git merge --ff-only origin/{name}` in your own shell to fast-forward.")

    def push(self, path: Any, branch: Any = None) -> dict[str, Any]:
        """Guard, then push: the commit is pinned by id, an added allow mark refuses, then the guard's --push route."""
        repo, _ = self.prepare(path)
        name = valid_branch(branch) if branch is not None else self.current_branch(repo)
        proc = self._git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}^{{commit}}")
        sha = proc.stdout.strip()
        if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise GitHubError("That branch does not exist in this repository.")
        spec = f"{sha}:refs/heads/{name}"
        token = self.token_reader()
        if not token:
            raise GitHubError("GitHub is not connected on this box; the owner connects it on the add-on page.")
        try:
            scan = subprocess.run([self.python, "-I", "-c", ALLOW_SCAN, os.path.dirname(self.guard), str(repo),
                                   "origin", spec], env=self.env(True), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return reply(False, "The push check took too long and was stopped; nothing was pushed.")
        if scan.returncode != 0 or not scan.stdout.strip().isdigit():
            return reply(False, "The push could not be checked; nothing was pushed.\n"
                         + redact_token(scan.stderr[-2000:], token))
        if int(scan.stdout.strip()):
            return reply(False, "An allow mark needs the owner; this push is refused. Nothing was pushed.")
        try:
            guard = subprocess.run([self.python, "-I", self.guard, "--repo", str(repo), "--push", "origin", spec],
                                   env=self.env(True), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return reply(False, "The push took too long and was stopped.")
        text = redact_token((guard.stdout or "") + (guard.stderr or ""), token) + self.recheck(repo)
        text = GUARD_ADVICE_RE.sub("push_guard: push refused. Remove what it found and commit again.", text)
        if guard.returncode == 1:
            return reply(False, "The push guard refused this push; nothing was pushed.\n" + text)
        if guard.returncode != 0:
            return reply(False, "The push did not complete.\n" + text)
        return reply(True, f"Pushed {sha[:12]} to {name}.\n" + text)


def reply(ok: bool, text: str) -> dict[str, Any]:
    return {"ok": bool(ok), "text": redact_token(str(text))[:REPLY_LIMIT]}


class GitHubService:
    """The sign-in's state, the routes' actions, the hourly check and the socket the github_git tool uses."""

    def __init__(self, root: Path | Callable[[], Path], *, client_id: Callable[[], str] = lambda: "", http=None,
                 fire_event: Callable[[str, dict], Any] = lambda *_: None,
                 notify: Callable[[str, str], Any] = lambda *_: None,
                 find_task: Callable[[Any], str | None] = lambda _cap: None,
                 read_only: Callable[[str], bool] = lambda _task: True,
                 runner: GitRunner | None = None, log: Callable[[str], Any] | None = None):
        self.store = Store(root)
        self.client_id = client_id
        self.http = http or Http()
        self.fire_event = fire_event
        self.notify = notify
        self.find_task = find_task
        self.read_only = read_only
        self.log = log or (lambda text: print(text, flush=True))
        self.lock = threading.RLock()
        self.git_lock = threading.Lock()
        self.flow: DeviceFlow | None = None
        self.flow_thread: threading.Thread | None = None
        self.last_test: dict[str, Any] | None = None
        self.failures = 0
        self.next_check = 0.0
        self.runner = runner or GitRunner(self.store.read_token, self.connected_repositories)

    # -- state ---------------------------------------------------------------------------------------------
    def connected_repositories(self) -> list[str]:
        state = self.store.read_state()
        if not self.store.read_credential() or state.get("last_check", {}).get("state") not in ("ok", "near_expiry"):
            return []
        return [repo["full_name"] for repo in state.get("repositories") or [] if isinstance(repo, dict)
                and isinstance(repo.get("full_name"), str) and repo.get("push")]

    def client_id_value(self) -> tuple[str, str]:
        option = (self.client_id() or "").strip()
        return (option, "option") if option else (GITHUB_CLIENT_ID, "built_in")

    def current_state(self) -> str:
        if not self.store.read_credential():
            flow = self.flow.public if self.flow else {}
            return "connecting" if flow.get("status") == "waiting_for_user" else "disconnected"
        return str(self.store.read_state().get("last_check", {}).get("state") or "ok")

    def status(self) -> dict[str, Any]:
        """The page contract: never a token, never a device code, never an email."""
        state = self.store.read_state() if self.store.read_credential() else {}
        credential = self.store.read_credential() or {}
        last = state.get("last_check") if isinstance(state.get("last_check"), dict) else None
        flow = dict(self.flow.public) if self.flow else {"status": "idle", "user_code": "", "verification_uri": "",
                                                         "expires_at": None, "error": ""}
        return {
            "state": self.current_state(),
            "kind": credential.get("kind"),
            "account": {"login": state["account"]["login"]} if isinstance(state.get("account"), dict)
            and state["account"].get("login") else None,
            "client_id_source": self.client_id_value()[1],
            "repositories": [{"full_name": r["full_name"], "private": bool(r.get("private")), "push": bool(r.get("push"))}
                             for r in state.get("repositories") or [] if isinstance(r, dict) and r.get("full_name")],
            "expected_repositories": list((state.get("expected") or {}).get("repositories") or []),
            "missing": list(state.get("missing") or []),
            "extra": list(state.get("extra") or []),
            "permissions": state.get("permissions"),
            "permission_problem": state.get("permission_problem"),
            "expires_at": state.get("expires_at"),
            "connected_at": state.get("connected_at"),
            "last_check": {"at": last.get("at"), "state": last.get("state"), "reason": last.get("reason")} if last else None,
            "last_test": dict(self.last_test) if self.last_test else None,
            "revoke_url": TOKEN_REVOKE_URL if credential.get("kind") == "token" else APP_REVOKE_URL,
            "flow": flow,
        }

    def prompt_note(self) -> str:
        if self.current_state() not in ("ok", "near_expiry"):
            return ""
        return ("To clone, link, fetch, pull or push a GitHub repository use the github_git tool; git has no network and no "
                "credential in your shell.\n\n")

    # -- connecting ----------------------------------------------------------------------------------------
    def _save_connection(self, kind: str, token: str, live: dict[str, Any], token_repository: str | None) -> None:
        names = [repo["full_name"] for repo in live.get("repositories") or []]
        result = judge(live, {"repositories": names})
        now = iso(utc_now())
        state = {
            "account": live.get("account"), "client_id": self.client_id_value()[0] if kind == "app" else "",
            "installations": live.get("installations") or [], "repositories": live.get("repositories") or [],
            "permissions": live.get("permissions"), "permission_problem": live.get("permission_problem"),
            "token_repository": token_repository, "connected_at": now, "expires_at": live.get("expires_at"),
            "missing": result["missing"], "extra": result["extra"],
            "last_check": {"at": now, "state": result["state"], "reason": result["reason"]},
            "expected": {"repositories": names, "permissions": live.get("permissions")},
        }
        with self.store.lock:
            previous = self.store.read_state().get("last_check", {}).get("state") if self.store.read_credential() else "disconnected"
            self.store.write_credential(kind, token, live.get("expires_at"))
            self.store.write_state(state)
        self.failures = 0
        self.next_check = time.monotonic() + GITHUB_CHECK_SECONDS
        self._announce(previous or "disconnected", state)

    def _complete_app(self, token: str) -> None:
        try:
            live = inspect_access(self.http, token, "app")
        except GitHubError as exc:
            raise GitHubError("The new sign-in could not be checked: " + str(exc)) from None
        if not live.get("repositories"):
            raise GitHubError("The GitHub App is not installed on any repository you can reach. Install it on "
                              "the repositories of your choice, then press Connect again.")
        self._save_connection("app", token, live, None)
        self.log("GitHub sign-in connected (app).")

    def connect(self) -> dict[str, Any]:
        with self.lock:
            if self.flow and self.flow.public.get("status") == "waiting_for_user":
                return self.status()
            client_id = self.client_id_value()[0]
            flow = DeviceFlow(self.http, client_id, self._complete_app)
            flow.start()
            self.flow = flow
            self.flow_thread = threading.Thread(target=flow.run, daemon=True, name="github-device-flow")
            self.flow_thread.start()
        return self.status()

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            if self.flow:
                self.flow.cancel()
                self.flow.finish("canceled")
        return self.status()

    def paste_token(self, token: Any, repository: Any) -> dict[str, Any]:
        """The fallback: a fine-grained token for one repository, Contents read and write, nothing more."""
        repository = valid_repository(repository)
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token.strip()):
            raise GitHubError("Paste a fine-grained personal access token (it starts with github_pat_).")
        token = token.strip()
        try:
            live = inspect_access(self.http, token, "token", repository)
        except Revoked:
            raise GitHubError("GitHub refused this token.") from None
        except GitHubError as exc:
            raise GitHubError(str(exc)) from None
        if not live.get("repositories") or live.get("permission_problem"):
            raise GitHubError("This token was not accepted: " + str(live.get("permission_problem") or
                              "it cannot read the repository") + ". Make one for this repository only, with "
                              "Contents read and write and nothing else.")
        self._save_connection("token", token, live, repository)
        self.log("GitHub sign-in connected (token).")
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        with self.lock:
            if self.flow:
                self.flow.cancel()
                self.flow = None
            previous = self.current_state()
            self.store.clear()
            self.last_test = None
            self.failures = 0
        if previous not in ("disconnected", "connecting"):
            self._fire(previous, {"last_check": {"state": "disconnected", "reason": "disconnected on the page"}})
        self.log("GitHub sign-in disconnected.")
        return self.status()

    def accept(self) -> dict[str, Any]:
        """F10: take the live repository set as the new baseline, only when the permissions are exact."""
        with self.store.lock:
            state = self.store.read_state()
            if not self.store.read_credential() or state.get("last_check", {}).get("state") != "access_changed":
                raise GitHubError("There is no access change to accept.")
            if state.get("permission_problem"):
                raise GitHubError("The access is not exactly Contents read and write; change it on GitHub instead.")
            state["expected"] = {"repositories": [r["full_name"] for r in state.get("repositories") or []],
                                 "permissions": state.get("permissions")}
            self.store.write_state(state)
        return self.check()

    # -- checking ------------------------------------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        """The monitoring check: at start, hourly and on Check now. One event and one notice per change."""
        credential = self.store.read_credential()
        self.next_check = time.monotonic() + GITHUB_CHECK_SECONDS
        if not credential:
            return self.status()
        with self.store.lock:
            state = self.store.read_state()
        previous = str(state.get("last_check", {}).get("state") or "ok")
        now = utc_now()
        try:
            live = inspect_access(self.http, credential["token"], credential["kind"], state.get("token_repository"))
        except Revoked:
            result = {"state": "revoked", "reason": "GitHub refused the stored sign-in", "missing": [], "extra": []}
            live = {}
            self.failures = 0
        except Unreachable:
            self.failures += 1
            if self.failures < FAILURES_BEFORE_FAILED:
                last = dict(state.get("last_check") or {})
                last.update(at=iso(now), reason="unreachable")
                state["last_check"] = last
                self.store.write_state(state)
                return self.status()
            result = {"state": "failed", "reason": "GitHub could not be reached three times in a row",
                      "missing": [], "extra": []}
            live = {}
        except GitHubError as exc:
            self.failures = 0
            result = {"state": "failed", "reason": str(exc)[:200], "missing": [], "extra": []}
            live = {}
        else:
            self.failures = 0
            if credential.get("expires_at") and not live.get("expires_at"):
                live["expires_at"] = credential["expires_at"]
            result = judge(live, state.get("expected") or {}, now)
        if live:
            for key in ("account", "installations", "repositories", "permissions", "permission_problem", "expires_at"):
                state[key] = live.get(key)
        state.update(missing=result["missing"], extra=result["extra"],
                     last_check={"at": iso(now), "state": result["state"], "reason": result["reason"]})
        with self.store.lock:
            if self.store.read_credential():  # a Disconnect during the check wins
                self.store.write_state(state)
            else:
                return self.status()
        self.log(f"GitHub access check: {result['state']}.")
        self._announce(previous, state)
        return self.status()

    def _announce(self, previous: str, state: dict[str, Any]) -> None:
        current = state.get("last_check", {}).get("state")
        if current == previous:
            return
        self._fire(previous, state)
        if current in BAD_STATES:
            when = state.get("last_check", {}).get("at") or ""
            self.notify("GitHub sign-in", f"GitHub sign-in on the Codex add-on: {STATE_WORDS.get(current, current)} "
                        f"since {when}. Open the Codex page > Settings > GitHub.")

    def _fire(self, previous: str, state: dict[str, Any]) -> None:
        last = state.get("last_check") or {}
        account = state.get("account") if isinstance(state.get("account"), dict) else {}
        try:
            self.fire_event("codex_cli_github_access", {
                "state": last.get("state"), "previous_state": previous, "reason": last.get("reason") or "",
                "account": account.get("login") or "", "missing": list(state.get("missing") or []),
                "extra": list(state.get("extra") or []), "permission_problem": state.get("permission_problem"),
                "expires_at": state.get("expires_at"), "addon": "codex_cli_worker", "source": "worker"})
        except Exception:
            self.log("Could not fire the GitHub access event.")

    def read_test(self) -> dict[str, Any]:
        """GET /repos/{owner}/{repo} for every connected repository, 10 seconds in all."""
        credential = self.store.read_credential()
        if not credential:
            raise GitHubError("GitHub is not connected.")
        state = self.store.read_state()
        names = [r["full_name"] for r in state.get("repositories") or [] if isinstance(r, dict)]
        started, good, failure = time.monotonic(), 0, ""
        for name in names:
            if time.monotonic() - started > 10:
                failure = failure or "the test ran out of time"
                break
            try:
                response = self.http.get(f"{API}/repos/{name}", credential["token"])
            except Exception:
                failure = failure or f"{name}: GitHub could not be reached"
                continue
            if response.status_code == 200:
                good += 1
            else:
                failure = failure or f"{name}: HTTP {response.status_code}"
        ok = bool(names) and not failure
        text = (f"Read OK: {good} repositor{'y' if good == 1 else 'ies'}." if ok
                else "Read failed: " + (failure or "no repository is connected") + ".")
        self.last_test = {"at": iso(utc_now()), "ok": ok, "text": text}
        return self.status()

    def tick(self) -> None:
        if time.monotonic() >= self.next_check and self.store.read_credential():
            self.check()

    def loop(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.next_check = 0.0
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                self.log("GitHub access check failed to run.")
            stop.wait(60)

    # -- the github_git tool -------------------------------------------------------------------------------
    def dispatch(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"operation", "arguments", "capability"}:
            raise GitHubError("A GitHub request needs exactly operation, arguments and capability.")
        task_id = self.find_task(payload["capability"])
        if not task_id:
            raise GitHubError("This exchange has ended.")
        operation, args = payload["operation"], payload["arguments"]
        if operation not in OPERATIONS:
            raise GitHubError("The operations are " + ", ".join(OPERATIONS) + ".")
        allowed = {"repos": set(), "fetch": {"path"}, "pull": {"path", "branch"}, "push": {"path", "branch"},
                   "clone": {"repository", "folder"}, "link": {"repository", "path"}}[operation]
        required = {"repos": set(), "clone": {"repository"}, "link": {"repository", "path"}}.get(operation, {"path"})
        if not isinstance(args, dict) or set(args) - allowed or required - set(args):
            raise GitHubError(f"{operation} takes " + (", ".join(sorted(allowed)) or "no arguments") + ".")
        if operation not in READ_ONLY_OPERATIONS and self.read_only(task_id):
            raise GitHubError("This chat is read only; only repos and fetch are available.")
        if self.current_state() not in ("ok", "near_expiry"):
            return reply(False, "GitHub is not connected on this box; the owner connects it on the add-on page.")
        if operation == "repos":
            status = self.status()
            return reply(True, json.dumps({"account": (status["account"] or {}).get("login"),
                                           "repositories": status["repositories"]}))
        if operation in ("clone", "link"):
            return self.local(operation, args)
        if not self.git_lock.acquire(blocking=False):
            return reply(False, "Another GitHub operation is running; try again when it ends.")
        try:
            if operation == "fetch":
                return self.runner.fetch(args["path"])
            if operation == "pull":
                return self.runner.pull(args["path"], args.get("branch"))
            return self.runner.push(args["path"], args.get("branch"))
        finally:
            self.git_lock.release()

    def local(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        """clone or link, for the tool and for the page."""
        if self.current_state() not in ("ok", "near_expiry"):
            raise GitHubError("GitHub is not connected on this box; the owner connects it on the add-on page.")
        if not self.git_lock.acquire(blocking=False):
            return reply(False, "Another GitHub operation is running; try again when it ends.")
        try:
            if operation == "clone":
                return self.runner.clone(args.get("repository"), args.get("folder"))
            return self.runner.link(args.get("repository"), args.get("path"))
        finally:
            self.git_lock.release()

    def answer(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > 65536:
            return {"ok": False, "text": "The request is larger than 64 KiB."}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {"ok": False, "text": "The request is not valid JSON."}
        try:
            return self.dispatch(payload)
        except GitHubError as exc:
            return reply(False, str(exc))
        except Exception:
            return {"ok": False, "text": "The GitHub request failed."}

    def serve(self, path: Path):
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(GIT_TIMEOUT_SECONDS * 2 + 30)
                result = service.answer(self.rfile.readline(65537))
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass

        Path(path).unlink(missing_ok=True)
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        server.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True, name="github-socket").start()
        return server


def register(worker, service: GitHubService) -> GitHubService:
    """The page routes (section 2.5 of the design). Reads need the worker token or Ingress; every change also
    needs Ingress and a Home Assistant administrator."""
    from flask import jsonify, request

    app = worker.app

    @app.after_request
    def no_cached_github(response):
        if request.path.startswith("/github"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def body() -> dict[str, Any]:
        if request.content_length is not None and request.content_length > 16384:
            raise GitHubError("Use a JSON request no larger than 16 KiB.")
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}

    def admin(func):
        @worker.require_auth
        @worker.require_ingress
        @wraps(func)
        def run(*args, **kwargs):
            person, refusal = worker.ingress_admin()
            if person is None:
                return jsonify(ok=False, error=refusal), 403
            try:
                return func(*args, **kwargs)
            except GitHubError as exc:
                return jsonify(ok=False, error=str(exc), github=service.status()), 400
            except Exception:
                return jsonify(ok=False, error="The GitHub request failed.", github=service.status()), 503
        return run

    @app.get("/github")
    @worker.require_auth
    def github_status():
        return jsonify(ok=True, github=service.status())

    @app.post("/github/connect", endpoint="github_connect")
    @admin
    def github_connect():
        return jsonify(ok=True, github=service.connect())

    @app.post("/github/cancel", endpoint="github_cancel")
    @admin
    def github_cancel():
        return jsonify(ok=True, github=service.cancel())

    @app.post("/github/token", endpoint="github_token")
    @admin
    def github_token():
        value = body()
        return jsonify(ok=True, github=service.paste_token(value.get("token"), value.get("repository")))

    @app.post("/github/test", endpoint="github_test")
    @admin
    def github_test():
        return jsonify(ok=True, github=service.read_test())

    @app.post("/github/check", endpoint="github_check")
    @admin
    def github_check():
        return jsonify(ok=True, github=service.check())

    @app.post("/github/accept", endpoint="github_accept")
    @admin
    def github_accept():
        return jsonify(ok=True, github=service.accept())

    def local_route(operation, keys):
        value = body()
        result = service.local(operation, {key: value.get(key) for key in keys})
        return jsonify(ok=result["ok"], text=result["text"], path=result.get("path"),
                       github=service.status()), 200 if result["ok"] else 400

    @app.post("/github/repos/clone", endpoint="github_repos_clone")
    @admin
    def github_repos_clone():
        return local_route("clone", ("repository", "folder"))

    @app.post("/github/repos/link", endpoint="github_repos_link")
    @admin
    def github_repos_link():
        return local_route("link", ("repository", "path"))

    @app.post("/github/disconnect", endpoint="github_disconnect")
    @admin
    def github_disconnect():
        if worker.active_task_id():
            return jsonify(ok=False, error="Wait for the running task to end, then disconnect.",
                           github=service.status()), 409
        return jsonify(ok=True, github=service.disconnect())

    return service


def find_task_by_capability(ha_data, capability: Any) -> str | None:
    """The running exchange that holds this capability, the same check the scheduling tools use."""
    if not isinstance(capability, str) or not capability or not capability.isascii():
        return None
    with ha_data.lock:
        task_id = next((key for key, value in ha_data.capabilities.items()
                        if hmac.compare_digest(value, capability)), None)
    return task_id if task_id and ha_data.active(task_id, capability) else None
