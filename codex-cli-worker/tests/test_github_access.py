"""The GitHub sign-in's worker side: the store, the device sign-in, the access check and git for Codex.

Every token, code and account here is invented. No test reaches GitHub: the HTTP calls go to a fake, and the
git tests push to a local bare repository that stands in for github.com.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER))

import github_access as gh  # noqa: E402
import github_mcp  # noqa: E402

APP_TOKEN = "ghu_" + "A1b2C3d4E5" * 4  # synthetic
PAT = "github_pat_" + "Z9y8X7w6V5" * 8  # synthetic
REPO = "example-owner/house"
OTHER = "example-owner/garage"
# The kit's guard at fatherdoctor/whole_home_assistant afb8282 (templates/guard/push_guard.py), copied unchanged.
KIT_GUARD_SHA256 = "c864ae31dd0190d71235aad389379c487dc8ca7090eef2e06cb8068e85a5c3e9"
GIT_ID = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
          "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeHttp:
    """Answers by URL; POST answers are taken in order. Records every call, never sends one."""

    def __init__(self, gets=None, posts=None):
        self.gets = dict(gets or {})
        self.posts = list(posts or [])
        self.calls = []

    def get(self, url, token, params=None):
        self.calls.append(("GET", url, token))
        answer = self.gets.get(url)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else Response(404, {"message": "Not Found"})

    def post(self, url, data):
        self.calls.append(("POST", url, dict(data)))
        return self.posts.pop(0)


def app_answers(repos=(REPO,), permissions=None, admin=True, push=True, user_status=200):
    """Canned GitHub answers for an App user token. The owner owns the repositories, so admin is true (F2)."""
    return {
        gh.API + "/user": Response(user_status, {"login": "example-owner", "id": 1}),
        gh.API + "/user/installations": Response(200, {"installations": [
            {"id": 7, "account": {"login": "example-owner"},
             "permissions": permissions if permissions is not None else {"contents": "write", "metadata": "read"}}]}),
        gh.API + "/user/installations/7/repositories": Response(200, {"repositories": [
            {"full_name": name, "private": True, "permissions": {"admin": admin, "push": push, "pull": True}}
            for name in repos]}),
    }


def token_answers(listed=(REPO,), hooks=403, push=True, expires="2026-12-20 12:00:00 UTC"):
    return {
        gh.API + "/user": Response(200, {"login": "example-owner", "id": 1}),
        f"{gh.API}/repos/{REPO}": Response(200, {"full_name": REPO, "private": True,
                                                 "permissions": {"admin": True, "push": push, "pull": True}},
                                           {"GitHub-Authentication-Token-Expiration": expires}),
        gh.API + "/user/repos": Response(200, [{"full_name": name} for name in listed]),
        f"{gh.API}/repos/{REPO}/hooks": Response(hooks, {"message": "Resource not accessible"}),
    }


def git(repo, *args, env=None, check=True):
    full = {**os.environ, **GIT_ID, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **(env or {})}
    return subprocess.run(["git", "-C", str(repo), *args], env=full, capture_output=True, text=True, check=check)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events, self.notices, self.logs = [], [], []
        self.http = FakeHttp(app_answers())
        self.service = gh.GitHubService(
            self.root / "data" / "github", client_id=lambda: "Iv23liExampleClient", http=self.http,
            fire_event=lambda event, data: self.events.append((event, data)),
            notify=lambda title, message: self.notices.append((title, message)),
            find_task=lambda cap: "task-1" if cap == "cap-live" else None,
            read_only=lambda task: False, log=self.logs.append)

    def connect_app(self):
        self.service._complete_app(APP_TOKEN)
        self.events.clear()
        self.notices.clear()


class StoreTests(ServiceCase):
    def test_credential_is_0600_in_a_0700_folder_and_state_has_no_token(self):
        self.connect_app()
        folder = self.root / "data" / "github"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / "credential.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((folder / "state.json").stat().st_mode), 0o600)
        self.assertNotIn(APP_TOKEN, (folder / "state.json").read_text())
        self.assertNotIn(APP_TOKEN, json.dumps(self.service.status()))
        self.assertNotIn(APP_TOKEN, "\n".join(self.logs))

    def test_a_loose_umask_still_writes_0600(self):
        old = os.umask(0)
        try:
            self.service.store.write_credential("app", APP_TOKEN, None)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.service.store.credential_path.stat().st_mode), 0o600)

    def test_disconnect_forgets_the_sign_in_and_gives_the_revoke_link(self):
        self.connect_app()
        status = self.service.disconnect()
        self.assertEqual(status["state"], "disconnected")
        self.assertEqual(status["revoke_url"], gh.APP_REVOKE_URL)
        self.assertFalse(self.service.store.credential_path.exists())
        self.assertFalse(self.service.store.state_path.exists())
        self.assertEqual(self.events[-1][1]["state"], "disconnected")


class DeviceFlowTests(ServiceCase):
    def flow(self, *answers):
        self.http.posts = [Response(200, {"device_code": "synthetic-device-code", "user_code": "WXYZ-1234",
                                          "verification_uri": "https://github.com/login/device",
                                          "expires_in": 900, "interval": 5}), *answers]
        flow = gh.DeviceFlow(self.http, "Iv23liExampleClient", self.service._complete_app, sleep=lambda s: False)
        public = flow.start()
        self.assertEqual(public["status"], "waiting_for_user")
        self.assertEqual(public["user_code"], "WXYZ-1234")
        self.assertNotIn("synthetic-device-code", json.dumps(public))
        return flow

    def test_pending_then_slow_down_then_success(self):
        flow = self.flow(Response(200, {"error": "authorization_pending"}), Response(200, {"error": "slow_down"}),
                         Response(200, {"access_token": APP_TOKEN, "token_type": "bearer"}))
        flow.run()
        self.assertEqual(flow.interval, 10)
        self.assertEqual(flow.public["status"], "completed")
        self.assertEqual(flow.public["user_code"], "")
        self.assertEqual(self.service.store.read_token(), APP_TOKEN)
        self.assertEqual(self.service.status()["state"], "ok")
        self.assertEqual(self.service.status()["repositories"], [{"full_name": REPO, "private": True, "push": True}])
        self.assertNotIn(APP_TOKEN, json.dumps(self.service.status()))
        posted = [call for call in self.http.calls if call[0] == "POST"]
        self.assertTrue(all("client_secret" not in call[2] for call in posted))

    def test_expired_and_denied_store_nothing(self):
        for error, status in (("expired_token", "expired"), ("access_denied", "failed")):
            with self.subTest(error=error):
                flow = self.flow(Response(200, {"error": error}))
                flow.run()
                self.assertEqual(flow.public["status"], status)
                self.assertIsNone(self.service.store.read_token())

    def test_no_client_id_is_a_plain_refusal(self):
        with self.assertRaises(gh.GitHubError):
            gh.DeviceFlow(self.http, "", lambda token: None).start()


class CheckTests(ServiceCase):
    def check_with(self, answers):
        self.http.gets = answers
        return self.service.check()

    def test_owner_admin_on_the_repository_is_still_ok(self):
        """F2: the user's admin role is not the App's access."""
        self.connect_app()
        self.assertEqual(self.check_with(app_answers(admin=True))["state"], "ok")
        self.assertEqual(self.events, [])

    def test_each_bad_state_from_canned_answers(self):
        cases = [
            ("missing repository", app_answers(repos=()), "access_changed", "missing"),
            ("extra repository", app_answers(repos=(REPO, OTHER)), "access_changed", "extra"),
            ("administration permission", app_answers(permissions={"contents": "write", "metadata": "read",
                                                                   "administration": "write"}), "access_changed", None),
            ("read only", app_answers(permissions={"contents": "read", "metadata": "read"}), "access_changed", None),
            ("user lost write", app_answers(push=False), "access_changed", None),
            ("revoked", app_answers(user_status=401), "revoked", None),
        ]
        for name, answers, state, field in cases:
            with self.subTest(name=name):
                self.service.store.clear()
                self.http.gets = app_answers()
                self.connect_app()
                status = self.check_with(answers)
                self.assertEqual(status["state"], state)
                if field:
                    self.assertEqual(status[field], [REPO] if field == "missing" else [OTHER])
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.events[0][0], "codex_cli_github_access")
                self.assertEqual(self.events[0][1]["state"], state)
                self.assertEqual(self.events[0][1]["previous_state"], "ok")
                self.assertEqual(self.events[0][1]["source"], "worker")
                self.assertEqual(len(self.notices), 1)
                self.assertNotIn(APP_TOKEN, json.dumps(self.events) + json.dumps(self.notices))
                # The same state again: no second event, no second notice (one cause, one warning).
                self.check_with(answers)
                self.assertEqual(len(self.events), 1)
                self.assertEqual(len(self.notices), 1)

    def test_more_or_less_is_named(self):
        self.connect_app()
        status = self.check_with(app_answers(permissions={"contents": "write", "metadata": "read", "issues": "write"}))
        self.assertEqual(status["permission_problem"], "more than Contents read and write")
        status = self.check_with(app_answers(permissions={"contents": "read", "metadata": "read"}))
        self.assertEqual(status["permission_problem"], "read only")

    def test_expiry_near_and_past(self):
        now = datetime.now(timezone.utc)
        live = {"repositories": [{"full_name": REPO, "push": True}], "permission_problem": None}
        near = gh.judge({**live, "expires_at": gh.iso(now + timedelta(days=3))}, {"repositories": [REPO]}, now)
        past = gh.judge({**live, "expires_at": gh.iso(now - timedelta(minutes=1))}, {"repositories": [REPO]}, now)
        far = gh.judge({**live, "expires_at": gh.iso(now + timedelta(days=60))}, {"repositories": [REPO]}, now)
        self.assertEqual((near["state"], past["state"], far["state"]), ("near_expiry", "expired", "ok"))

    def test_an_unreachable_github_is_failed_only_after_three_tries(self):
        self.connect_app()
        broken = {gh.API + "/user": OSError("synthetic network error")}
        for _ in range(2):
            self.assertEqual(self.check_with(broken)["state"], "ok")
        self.assertEqual(self.check_with(broken)["state"], "failed")
        self.assertEqual(len(self.events), 1)

    def test_accept_takes_the_new_set_only_when_the_permissions_are_exact(self):
        """F10."""
        self.connect_app()
        self.check_with(app_answers(repos=(REPO, OTHER)))
        status = self.service.accept()
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["expected_repositories"], [OTHER, REPO])
        self.check_with(app_answers(permissions={"contents": "write", "metadata": "read", "administration": "read"}))
        with self.assertRaises(gh.GitHubError):
            self.service.accept()

    def test_the_read_test_reports_each_repository(self):
        self.connect_app()
        self.http.gets[f"{gh.API}/repos/{REPO}"] = Response(200, {"full_name": REPO})
        self.assertTrue(self.service.read_test()["last_test"]["ok"])
        self.http.gets[f"{gh.API}/repos/{REPO}"] = Response(404, {})
        result = self.service.read_test()["last_test"]
        self.assertFalse(result["ok"])
        self.assertIn("HTTP 404", result["text"])


class TokenFallbackTests(ServiceCase):
    def test_an_owner_token_with_admin_role_but_contents_only_is_accepted(self):
        """F2: accepted by /user/repos and the hooks probe, not by the admin flag."""
        self.http.gets = token_answers()
        status = self.service.paste_token(PAT, REPO)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["kind"], "token")
        self.assertEqual(status["expires_at"], "2026-12-20T12:00:00+00:00")
        self.assertEqual(status["revoke_url"], gh.TOKEN_REVOKE_URL)
        self.assertNotIn(PAT, json.dumps(status) + "\n".join(self.logs))

    def test_a_wider_token_is_refused_and_never_echoed(self):
        for name, answers in (("administration", token_answers(hooks=200)),
                              ("two repositories", token_answers(listed=(REPO, OTHER))),
                              ("read only", token_answers(push=False))):
            with self.subTest(name=name):
                self.http.gets = answers
                with self.assertRaises(gh.GitHubError) as raised:
                    self.service.paste_token(PAT, REPO)
                self.assertNotIn(PAT, str(raised.exception))
                self.assertIsNone(self.service.store.read_token())

    def test_only_a_fine_grained_token_and_a_plain_repository_name(self):
        for token, repository in (("ghp_" + "a" * 36, REPO), (PAT, "../etc"), (PAT, "owner/repo/extra"), (PAT, "-x/y")):
            with self.subTest(repository=repository), self.assertRaises(gh.GitHubError):
                self.service.paste_token(token, repository)
        self.assertEqual(self.http.calls, [])

    def test_a_near_expiry_token_is_reported(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.http.gets = token_answers(expires=soon)
        self.service.paste_token(PAT, REPO)
        self.assertEqual(self.service.check()["state"], "near_expiry")


class NameTests(unittest.TestCase):
    def test_branch_names_that_force_delete_or_pass_options_are_refused(self):
        """F4."""
        for name in ("+main", ":main", "--force", "-d", "main:other", "a..b", "main.lock", "HEAD@{1}", "refs/../x",
                     "/main", "main/", "ma in", "main~1", "main^", "", ".hidden"):
            with self.subTest(name=name), self.assertRaises(gh.GitHubError):
                gh.valid_branch(name)
        for name in ("main", "feature/github-sign-in", "release-0.3", "v1.2_fix"):
            self.assertEqual(gh.valid_branch(name), name)

    def test_repository_and_folder_names(self):
        for name in ("owner/repo/extra", "../repo", "owner/..", "owner/.git", "+owner/repo", ":owner/repo", "owner",
                     "owner/repo.git"):
            with self.subTest(name=name), self.assertRaises(gh.GitHubError):
                gh.valid_repository(name)
        for name in ("..", ".", "a/b", "-x", "a.b"):
            with self.subTest(folder=name), self.assertRaises(gh.GitHubError):
                gh.valid_folder(name)

    def test_redaction_knows_github_token_shapes(self):
        text = f"x {APP_TOKEN} y {PAT} z ghp_{'b' * 36}"
        self.assertEqual(gh.redact_token(text), "x [redacted] y [redacted] z [redacted]")


class AskpassTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        credential = root / "credential.json"
        credential.write_text(json.dumps({"kind": "app", "token": APP_TOKEN}))
        self.helper = root / "askpass.py"
        self.helper.write_text((WORKER / "github_askpass.py").read_text().replace(
            'CREDENTIAL = "/data/github/credential.json"', f"CREDENTIAL = {str(credential)!r}"))

    def ask(self, prompt):
        return subprocess.run([sys.executable, "-I", str(self.helper), prompt], capture_output=True, text=True)

    def test_answers_only_github_prompts_and_only_on_stdout(self):
        user = self.ask("Username for 'https://github.com': ")
        self.assertEqual((user.returncode, user.stdout, user.stderr), (0, "x-access-token\n", ""))
        password = self.ask("Password for 'https://x-access-token@github.com': ")
        self.assertEqual((password.returncode, password.stdout, password.stderr), (0, APP_TOKEN + "\n", ""))
        for prompt in ("Password for 'https://x-access-token@github.com.example.net': ",
                       "Password for 'https://x-access-token@example.net': ",
                       "Password for 'https://someone@github.com': ", "Username for 'https://example.net': ",
                       "Password for 'http://x-access-token@github.com': ", ""):
            with self.subTest(prompt=prompt):
                other = self.ask(prompt)
                self.assertEqual((other.returncode, other.stdout), (1, ""))


class GitRunnerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config"
        self.share = root / "share"
        self.share.mkdir()
        self.repo = self.config / "house"
        self.repo.mkdir(parents=True)
        self.markers = root / "markers"
        self.markers.mkdir()
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "remote", "add", "origin", f"https://github.com/{REPO}.git")
        (self.repo / "README.md").write_text("house\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "first")
        self.runner = self.make_runner(gh.GitRunner)

    def make_runner(self, base):
        remote = self.remote

        class LocalRemote(base):
            """Stands the local bare repository in for github.com: a test-only rewrite added after the overrides."""

            def env(self, credentialed=True):
                env = super().env(credentialed)
                count = int(env["GIT_CONFIG_COUNT"])
                for key, value in ((f"url.{remote.as_uri()}.insteadOf", f"https://github.com/{REPO}.git"),
                                   ("protocol.file.allow", "always")):
                    env[f"GIT_CONFIG_KEY_{count}"], env[f"GIT_CONFIG_VALUE_{count}"] = key, value
                    count += 1
                env["GIT_CONFIG_COUNT"] = str(count)
                env.update(GIT_ID)
                return env

            def origin_repository(self, repo):
                return REPO

        return LocalRemote(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share,
                           askpass="/bin/false", guard=str(WORKER / "push_guard.py"))


class GitRunnerTests(GitRunnerCase):
    def test_the_environment_and_argv_carry_no_token(self):
        env = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO]).env()
        self.assertEqual(env["GIT_ASKPASS"], gh.ASKPASS_PATH)
        self.assertNotIn(APP_TOKEN, json.dumps(env))
        for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_TOKEN", "GIT_TRACE", "GIT_CURL_VERBOSE"):
            self.assertNotIn(key, env)
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        self.assertEqual(pairs["core.hooksPath"], "/dev/null")
        self.assertEqual(pairs["credential.helper"], "")
        self.assertEqual(pairs["core.fsmonitor"], "false")
        self.assertEqual(pairs["protocol.allow"], "never")
        calls = []
        real = subprocess.run

        def record(args, **kwargs):
            calls.append((list(args), kwargs.get("env")))
            return real(args, **kwargs)

        with patch.object(gh.subprocess, "run", side_effect=record):
            self.runner.fetch(str(self.repo))
        self.assertTrue(calls)
        for args, env in calls:
            self.assertNotIn(APP_TOKEN, " ".join(args) + json.dumps(env or {}))

    def test_paths_outside_config_and_protected_folders_are_refused(self):
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir()
        git(outside, "init", "-q")
        (self.config / ".storage").mkdir()
        git(self.config / ".storage", "init", "-q")
        for path in (str(outside), "../elsewhere", str(self.config / ".storage"), str(self.repo / ".git"), "", "x\x00y"):
            with self.subTest(path=path), self.assertRaises(gh.GitHubError):
                self.runner.prepare(path)

    def test_a_git_file_pointing_elsewhere_is_refused(self):
        linked = self.config / "linked"
        linked.mkdir()
        (linked / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        with self.assertRaises(gh.GitHubError):
            self.runner.prepare(str(linked))

    def test_a_remote_that_is_not_a_connected_github_repository_is_refused(self):
        real = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share)
        self.assertEqual(real.prepare(str(self.repo))[1], REPO)
        for url in ("https://example.net/example-owner/house.git", f"https://github.com/{OTHER}.git",
                    f"https://user@github.com/{REPO}.git", f"git@github.com:{REPO}.git"):
            with self.subTest(url=url):
                git(self.repo, "remote", "set-url", "origin", url)
                with self.assertRaises(gh.GitHubError):
                    real.prepare(str(self.repo))

    def test_planted_repo_config_cannot_run_or_redirect(self):
        """F1: planted settings refuse the operation, and the overrides alone keep them from running."""
        real = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share)
        fsmonitor = self.markers / "fsmonitor"
        helper = self.markers / "helper"
        hook = self.markers / "hook"
        git(self.repo, "config", "core.fsmonitor", f"touch {fsmonitor}; false")
        git(self.repo, "config", "credential.helper", f"!touch {helper}; true")
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "reference-transaction").write_text(f"#!/bin/sh\ntouch {hook}\n")
        (hooks / "reference-transaction").chmod(0o755)
        with self.assertRaises(gh.GitHubError) as refused:
            real.prepare(str(self.repo))
        self.assertIn("core.fsmonitor", str(refused.exception))
        self.assertIn("credential.helper", str(refused.exception))
        self.assertNotIn("touch", str(refused.exception))  # key names only, never values
        self.assertFalse(fsmonitor.exists() or helper.exists() or hook.exists())

        def exercise(env):
            subprocess.run(["git", "-C", str(self.repo), "status"], env=env, capture_output=True)
            subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "x"], env=env,
                           capture_output=True)
            subprocess.run(["git", "-C", str(self.repo), "credential", "approve"], env=env, capture_output=True,
                           input="protocol=https\nhost=github.com\nusername=x-access-token\npassword=synthetic\n\n",
                           text=True)

        # The allowlist bypassed: the environment overrides alone leave no marker file.
        exercise({**real.env(), **GIT_ID})
        self.assertFalse(fsmonitor.exists(), "fsmonitor ran")
        self.assertFalse(helper.exists(), "credential helper ran")
        self.assertFalse(hook.exists(), "hook ran")
        # Without the overrides each of them does run, so the check above means something.
        exercise({**gh._plain_env(), **GIT_ID})
        self.assertTrue(fsmonitor.exists() and helper.exists())
        try:  # some hosts mount temporary folders without exec; there the hook cannot run either way
            subprocess.run([str(hooks / "reference-transaction")], capture_output=True)
        except PermissionError:
            return
        self.assertTrue(hook.exists())

    def test_push_url_rewrites_includes_and_filters_are_refused(self):
        real = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share)
        for key, value in (("remote.origin.pushurl", "https://example.net/collect.git"),
                           ("url.https://example.net/.insteadOf", "https://github.com/"),
                           ("url.https://example.net/.pushInsteadOf", "https://github.com/"),
                           ("include.path", "/tmp/other"), ("filter.x.smudge", "touch y"),
                           ("http.proxy", "http://127.0.0.1:1"), ("http.https://github.com/.sslVerify", "false"),
                           ("pull.rebase", "true"), ("core.worktree", "/"), ("remote.origin.uploadpack", "touch z")):
            with self.subTest(key=key):
                git(self.repo, "config", key, value)
                try:
                    with self.assertRaises(gh.GitHubError) as refused:
                        real.prepare(str(self.repo))
                    self.assertNotIn(value, str(refused.exception))
                finally:
                    git(self.repo, "config", "--unset-all", key, check=False)
        self.assertEqual(real.prepare(str(self.repo))[1], REPO)


class PushTests(GitRunnerCase):
    def commit(self, name, text):
        (self.repo / name).write_text(text)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", "change " + name)

    def remote_heads(self):
        return git(self.remote, "for-each-ref", "--format=%(refname) %(objectname)").stdout.strip()

    def test_a_clean_push_runs_the_guard_and_pins_the_commit(self):
        result = self.runner.push(str(self.repo))
        self.assertTrue(result["ok"], result["text"])
        self.assertIn("push_guard: clean", result["text"])
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.remote_heads(), f"refs/heads/main {head}")

    def test_a_planted_secret_is_refused_and_the_advice_line_is_removed(self):
        self.commit("secrets_here.yaml", "api_password: Synthetic9Value42\n")
        result = self.runner.push(str(self.repo))
        self.assertFalse(result["ok"])
        self.assertIn("ABORT literal secret: secrets_here.yaml:1", result["text"])
        self.assertNotIn("guard: allow", result["text"])
        self.assertNotIn("Synthetic9Value42", result["text"])
        self.assertEqual(self.remote_heads(), "")

    def test_allow_mark_added_by_the_push_is_refused(self):
        """F3: a mark the agent adds cannot clear the guard; nothing is pushed."""
        self.commit("secrets_here.yaml", "api_password: Synthetic9Value42  # guard: allow test fixture value\n")
        result = self.runner.push(str(self.repo))
        self.assertFalse(result["ok"])
        self.assertIn("An allow mark needs the owner", result["text"])
        self.assertEqual(self.remote_heads(), "")

    def test_force_delete_and_option_branches_never_reach_git(self):
        for branch in ("+main", ":main", "--force", "-d", "main:refs/tags/x", "--tags", "--all"):
            with self.subTest(branch=branch), self.assertRaises(gh.GitHubError):
                self.runner.push(str(self.repo), branch)
        self.assertEqual(self.remote_heads(), "")

    def test_a_guard_refusal_stops_the_push(self):
        fake = Path(self.temp.name) / "guard" / "push_guard.py"
        fake.parent.mkdir()
        fake.write_text((WORKER / "push_guard.py").read_text().replace(
            'print("push_guard: clean")', 'print("ABORT test: forced"); return 1'))
        runner = self.make_runner(gh.GitRunner)
        runner.guard = str(fake)
        result = runner.push(str(self.repo))
        self.assertFalse(result["ok"])
        self.assertIn("refused", result["text"])
        self.assertEqual(self.remote_heads(), "")

    def test_pull_never_checks_out(self):
        """C1: pull is a credentialed fetch only; a filter planted in the repository never runs in the worker."""
        self.assertTrue(self.runner.push(str(self.repo))["ok"])
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        other = Path(self.temp.name) / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True, capture_output=True)
        (other / ".gitattributes").write_text("*.txt filter=planted\n")
        (other / "new.txt").write_text("upstream\n")
        git(other, "add", ".gitattributes", "new.txt")
        git(other, "commit", "-q", "-m", "upstream change")
        git(other, "push", "-q", "origin", "HEAD:main")
        marker = self.markers / "smudge"
        before = git(self.repo, "rev-parse", "HEAD").stdout
        runner = self.runner
        real_check = runner.check_config
        planted = []

        def plant_after_check(repo):
            real_check(repo)
            if not planted:  # the race: settings written after the check passed
                planted.append(True)
                git(self.repo, "config", "filter.planted.smudge", f"touch {marker}; cat")

        with patch.object(runner, "check_config", side_effect=plant_after_check):
            result = runner.pull(str(self.repo))
        self.assertTrue(result["ok"], result["text"])
        self.assertIn("git merge --ff-only origin/main", result["text"])
        self.assertFalse(marker.exists(), "a planted filter ran in the worker")
        self.assertFalse((self.repo / "new.txt").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout, before)
        self.assertEqual(git(self.repo, "rev-parse", "origin/main").stdout,
                         git(other, "rev-parse", "HEAD").stdout)
        self.assertIn("settings changed while git ran", result["text"])
        with self.assertRaises(gh.GitHubError):  # and from now on the planted setting refuses the repository
            self.runner.fetch(str(self.repo))

    def plant_after_check(self, runner, plant):
        real_check = runner.check_config
        done = []

        def check(repo):
            real_check(repo)
            if not done:
                done.append(True)
                plant()

        return patch.object(runner, "check_config", side_effect=check)

    def test_settings_planted_after_the_check_never_run_in_fetch_or_push(self):
        """The overrides, not the check, keep a hook, fsmonitor or ssh command planted in the gap from running."""
        hook_marker, fsmonitor, ssh = (self.markers / name for name in ("hook", "fsmonitor", "ssh"))

        def plant():
            hooks = self.repo / ".git" / "hooks"
            hooks.mkdir(exist_ok=True)
            for name in ("reference-transaction", "pre-push", "post-checkout"):
                (hooks / name).write_text(f"#!/bin/sh\ntouch {hook_marker}\n")
                (hooks / name).chmod(0o755)
            git(self.repo, "config", "core.fsmonitor", f"touch {fsmonitor}; false")
            git(self.repo, "config", "core.sshCommand", f"touch {ssh}; false")

        for operation in ("fetch", "push"):
            with self.subTest(operation=operation):
                for name in ("core.fsmonitor", "core.sshCommand"):
                    git(self.repo, "config", "--unset-all", name, check=False)
                for name in ("reference-transaction", "pre-push", "post-checkout"):
                    (self.repo / ".git" / "hooks" / name).unlink(missing_ok=True)
                (self.repo / "change.md").write_text(operation + "\n")
                git(self.repo, "add", "change.md")
                git(self.repo, "commit", "-q", "-m", operation)
                with self.plant_after_check(self.runner, plant):
                    result = getattr(self.runner, operation)(str(self.repo))
                self.assertTrue(result["ok"], result["text"])
                self.assertIn("settings changed while git ran", result["text"])
                self.assertFalse(hook_marker.exists(), "a hook ran")
                self.assertFalse(fsmonitor.exists(), "fsmonitor ran")
                self.assertFalse(ssh.exists(), "the ssh command ran")

    def test_a_redirect_planted_after_the_check_gets_no_token(self):
        """A URL rewrite planted in the gap cannot be undone by the environment; the askpass helper answers
        only github.com, so the redirected request gets no token (see the askpass test), and the run fails."""
        credential = Path(self.temp.name) / "credential.json"
        credential.write_text(json.dumps({"kind": "app", "token": APP_TOKEN}))
        log = self.markers / "asked"
        helper = Path(self.temp.name) / "askpass"
        helper.write_text(f"#!{sys.executable} -I\n" + (WORKER / "github_askpass.py").read_text()
                          .replace('CREDENTIAL = "/data/github/credential.json"', f"CREDENTIAL = {str(credential)!r}")
                          .replace("def main(argv):", f"def main(argv):\n    open({str(log)!r}, 'a').write(repr(argv[1:]) + '\\n')"))
        helper.chmod(0o755)
        runner = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share, askpass=str(helper))

        def plant():
            git(self.repo, "config", "url.http://127.0.0.1:9/.insteadOf", "https://github.com/")

        real_origin = runner.origin_repository

        def origin(repo):  # planted after the effective-URL check too, the last check before git runs
            name = real_origin(repo)
            plant()
            return name

        with patch.object(runner, "origin_repository", side_effect=origin):
            result = runner.fetch(str(self.repo))
        self.assertFalse(result["ok"])
        self.assertNotIn(APP_TOKEN, result["text"])
        if log.exists():
            self.assertNotIn("github.com", log.read_text())  # never asked as GitHub, so never given the token

    def test_the_overrides_clear_a_remote_proxy(self):
        env = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO]).env()
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        self.assertEqual(pairs["remote.origin.proxy"], "")


class CloneLinkTests(GitRunnerCase):
    def published(self):
        self.assertTrue(self.runner.push(str(self.repo))["ok"])
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")

    def test_clone_goes_to_share_by_default_with_a_plain_origin_and_no_hooks(self):
        self.published()
        result = self.runner.clone(REPO)
        self.assertTrue(result["ok"], result["text"])
        target = self.share / "house"
        self.assertEqual(result["path"], str(target.resolve()))
        self.assertEqual((target / "README.md").read_text(), "house\n")
        config = (target / ".git" / "config").read_text()
        self.assertIn(f"url = https://github.com/{REPO}.git", config)
        self.assertNotIn(APP_TOKEN, config)
        self.assertNotIn("helper", config)
        hooks = target / ".git" / "hooks"
        self.assertEqual(list(hooks.glob("*")) if hooks.exists() else [], [])
        real = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share)
        self.assertEqual(real.prepare(str(target))[1], REPO)

    def test_clone_into_a_named_folder_under_share_or_config(self):
        self.published()
        for folder, target in (("copy_a", self.share / "copy_a"), (f"{self.share}/copy_b", self.share / "copy_b"),
                               (f"{self.config}/copy_c", self.config / "copy_c")):
            with self.subTest(folder=folder):
                result = self.runner.clone(REPO, folder)
                self.assertTrue(result["ok"], result["text"])
                self.assertTrue((target / "README.md").exists())

    def test_clone_refuses_bad_names_existing_folders_elsewhere_and_other_repositories(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.share / "HomeAssistant").mkdir()
        (self.share / "escape").symlink_to(outside)
        (self.share / "taken").mkdir()
        for repository, folder in ((REPO, ".."), (REPO, "a/b"), (REPO, "-x"), (REPO, "taken"), (OTHER, "new"),
                                   ("+example-owner/house", "new"), (REPO, "/etc/x"), (REPO, f"{outside}/x"),
                                   (REPO, f"{self.share}/../outside"), (REPO, "HomeAssistant"),
                                   (REPO, f"{self.share}/escape"), (REPO, f"{self.share}/escape/x"),
                                   (REPO, f"{self.config}/house")):
            with self.subTest(repository=repository, folder=folder), self.assertRaises(gh.GitHubError):
                self.runner.clone(repository, folder)
        self.assertEqual(list(outside.iterdir()), [])

    def test_link_sets_origin_and_writes_no_credential(self):
        real = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO, OTHER], config_root=self.config, share_root=self.share)
        git(self.repo, "remote", "remove", "origin")
        result = real.link(OTHER, str(self.repo))
        self.assertTrue(result["ok"], result["text"])
        self.assertEqual(git(self.repo, "remote", "get-url", "origin").stdout.strip(), f"https://github.com/{OTHER}.git")
        self.assertTrue(real.link(REPO, str(self.repo))["ok"])
        self.assertEqual(real.prepare(str(self.repo))[1], REPO)
        config = (self.repo / ".git" / "config").read_text()
        self.assertNotIn(APP_TOKEN, config)
        self.assertNotIn("helper", config)

    def test_link_works_under_share(self):
        local = self.share / "notes"
        local.mkdir()
        git(local, "init", "-q", "-b", "main")
        real = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share)
        self.assertTrue(real.link(REPO, str(local))["ok"])
        self.assertEqual(real.prepare(str(local))[1], REPO)

    def test_link_keeps_the_allowlist_and_refuses_protected_and_escaping_paths(self):
        real = gh.GitRunner(lambda: APP_TOKEN, lambda: [REPO], config_root=self.config, share_root=self.share)
        git(self.repo, "config", "core.fsmonitor", "touch marker")
        with self.assertRaises(gh.GitHubError):
            real.link(REPO, str(self.repo))
        git(self.repo, "config", "--unset", "core.fsmonitor")
        house = self.share / "HomeAssistant"
        house.mkdir()
        git(house, "init", "-q")
        outside = Path(self.temp.name) / "outside_repo"
        outside.mkdir()
        git(outside, "init", "-q")
        (self.share / "through_link").symlink_to(outside)
        before = (house / ".git" / "config").read_text()
        for repository, path in (("example-owner/other", str(self.repo)), (REPO, "/tmp"), ("../x", str(self.repo)),
                                 (REPO, str(house)), (REPO, str(self.share / "through_link")), (REPO, str(outside))):
            with self.subTest(repository=repository, path=path), self.assertRaises(gh.GitHubError):
                real.link(repository, path)
        self.assertEqual((house / ".git" / "config").read_text(), before)
        self.assertNotIn("github.com", (outside / ".git" / "config").read_text())


class DispatchTests(ServiceCase):
    def payload(self, operation, cap="cap-live", **arguments):
        return {"operation": operation, "arguments": arguments, "capability": cap}

    def test_a_wrong_or_stale_capability_is_refused(self):
        self.connect_app()
        for cap in ("", "cap-old", None, 7):
            with self.subTest(cap=cap):
                answer = self.service.answer(json.dumps(self.payload("repos", cap=cap)).encode())
                self.assertFalse(answer["ok"])
                self.assertIn("ended", answer["text"])

    def test_read_only_chats_get_repos_and_fetch_only(self):
        """F5."""
        self.connect_app()
        self.service.read_only = lambda task: True
        calls = []

        class Runner:
            def fetch(self, path):
                calls.append(("fetch", path))
                return gh.reply(True, "fetched")

            def pull(self, path, branch=None):
                calls.append(("pull", path))

            def push(self, path, branch=None):
                calls.append(("push", path))

        self.service.runner = Runner()
        for operation, args in (("push", {"path": "/config"}), ("pull", {"path": "/config"}),
                                ("clone", {"repository": REPO, "folder": "x"}),
                                ("link", {"repository": REPO, "path": "/config"})):
            answer = self.service.answer(json.dumps(self.payload(operation, **args)).encode())
            self.assertFalse(answer["ok"])
            self.assertIn("read only", answer["text"])
        self.assertTrue(self.service.answer(json.dumps(self.payload("repos")).encode())["ok"])
        self.assertTrue(self.service.answer(json.dumps(self.payload("fetch", path="/config")).encode())["ok"])
        self.assertEqual(calls, [("fetch", "/config")])

    def test_no_reply_ever_holds_the_token(self):
        """F9: the socket has no operation that returns the credential."""
        self.connect_app()
        replies = [self.service.answer(json.dumps(self.payload(op, **args)).encode()) for op, args in (
            ("repos", {}), ("fetch", {"path": "/nowhere"}), ("push", {"path": "/config", "branch": "+main"}),
            ("token", {}), ("credential", {}), ("repos", {"path": "x"}))]
        replies.append(self.service.answer(b"not json"))
        self.assertNotIn(APP_TOKEN, json.dumps(replies))
        self.assertNotIn(APP_TOKEN, json.dumps(self.service.status()))

    def test_not_connected_is_one_plain_sentence(self):
        answer = self.service.answer(json.dumps(self.payload("repos")).encode())
        self.assertEqual(answer, {"ok": False, "text": "GitHub is not connected on this box; the owner connects it "
                                                       "on the add-on page."})

    def test_the_mcp_tool_schema_is_closed_and_bounded(self):
        (tool,) = github_mcp.TOOLS
        self.assertEqual(tool["name"], "github_git")
        schema = tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["operation"]["enum"]), set(gh.OPERATIONS))
        self.assertTrue(all("maxLength" in value for key, value in schema["properties"].items() if key != "operation"))


class GuardRowsTests(unittest.TestCase):
    """The text checks name the folder too; the mask in codex-bwrap is the one that holds."""

    def test_approvals_and_gate_protect_the_github_folder(self):
        import approvals
        import gate_rules
        for path in ("/data/github/credential.json", "/data/github", "/data/github.sock", "/data//github/state.json"):
            with self.subTest(path=path):
                self.assertTrue(approvals.touches_protected_path(path))
                self.assertEqual(gate_rules._policy_restriction(f"cat {path}"), "accesses the add-on's GitHub sign-in")
        self.assertEqual(gate_rules._policy_restriction("cat '/data/git''hub/credential.json'"),
                         "accesses the add-on's GitHub sign-in")

    def test_the_backup_leaves_the_github_folder_out(self):
        lines = (WORKER / "config.yaml").read_text().split("backup_exclude:\n", 1)[1].splitlines()
        exclusions = [line.split("- ", 1)[1].strip('"') for line in lines[:lines.index(next(
            line for line in lines if not line.startswith("  - ")))]]
        self.assertIn("*/github", exclusions)
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            for relative in ("github/credential.json", "github/state.json", "github/credential.json.tmp",
                             "github-notes", "tasks/kept.txt"):
                (data / relative).parent.mkdir(parents=True, exist_ok=True)
                (data / relative).write_text("fixture")
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                archive.add(data, arcname="data", filter=lambda info: None if any(
                    PurePosixPath(info.name).match(pattern) for pattern in exclusions) else info)
            buffer.seek(0)
            names = tarfile.open(fileobj=buffer).getnames()
        self.assertIn("data/github-notes", names)
        self.assertFalse(any(name == "data/github" or name.startswith("data/github/") for name in names), names)

    def test_the_bundled_guard_is_the_kit_guard_unchanged(self):
        self.assertEqual(hashlib.sha256((WORKER / "push_guard.py").read_bytes()).hexdigest(), KIT_GUARD_SHA256)

    def test_the_image_installs_the_helper_and_a_read_only_guard(self):
        docker = (WORKER / "Dockerfile").read_text()
        self.assertIn("COPY github_askpass.py /usr/local/bin/codex-github-askpass", docker)
        self.assertIn("COPY push_guard.py /usr/share/ha_codex/push_guard.py", docker)
        self.assertIn("chmod 0444 /usr/share/ha_codex/push_guard.py", docker)
        self.assertIn("COPY github_access.py github_mcp.py /", docker)


if __name__ == "__main__":
    unittest.main()
