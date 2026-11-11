# AGENTS.md

## What this is

ha_codex is a public fork (MIT) of moryoav/home-assistant-codex: a Home Assistant app (add-on) that runs the Codex
CLI against a configuration folder, and a custom integration (`codex_cli`) that exposes it to Home Assistant.
Pre-release: nothing is tagged or published from this fork yet, and no change here reaches any install by itself.

## Commands

- Test, everything: `pytest` - green is `N passed` (a few skipped is normal). Needs Linux, Python 3.14 and
  `pip install pytest-homeassistant-custom-component==0.13.374 flask pyyaml requests websocket-client`
  (the newest pair in `.github/workflows/verification.yaml`; that plugin pins Home Assistant Core).
- Test, worker only: `python -m unittest discover -s codex-cli-worker/tests -v` - green ends `OK`. Python 3.12 or
  later with `pip install aiohttp flask pyyaml requests websocket-client`; no Home Assistant needed.
- Check versions and build the HACS ZIP: `python scripts/build_hacs_release.py v<version> <output folder>`, with
  `<version>` from `custom_components/codex_cli/manifest.json` - green prints the path of `codex_cli.zip`, exit 0.
- Check whether a version may be tagged: `python scripts/fork_release.py check-tag <version>` - changes nothing;
  `refused: no lab-pass record ...` with exit 1 is the expected answer until a release is being prepared.
- Build: none for the integration. The worker image (`docker build codex-cli-worker`) and the browser checks
  (`node codex-cli-worker/tests/browser_*.cjs`) need Docker, or Node 22 with Playwright; they run in the
  Verification checks workflow, and CONTRIBUTING.md says how to run them by hand.
- Run: only inside Home Assistant OS or Supervised, by a person; `python codex-cli-worker/tests/web_fixture.py`
  serves the chat page alone on port 9137 with temporary data, without Home Assistant or Codex.
- Before a push: the full `pytest` run, or the worker-only run when 3.14 is not at hand (say which in the PR).

The first four were run as written from a fresh clone on 2026-11-11 (512 passed, 2 skipped; 478 tests OK).

## Layout

| Path | What is there |
|---|---|
| `codex-cli-worker/` | the app: `server.py` (API and chat page), `policy.py`, `approvals.py`, `run.sh`, `Dockerfile`, `config.yaml` (options and the app's version) |
| `codex-cli-worker/policy/` | the permission policy and house rules built into the image |
| `codex-cli-worker/web/` | the chat page (plain JavaScript and CSS, no build step) |
| `codex-cli-worker/tests/` | every test: `test_*.py`, the Home Assistant tests in `ha/`, browser checks `browser_*.cjs` |
| `custom_components/codex_cli/` | the integration; `manifest.json` holds its version |
| `scripts/` | `build_hacs_release.py`, `fork_release.py` (tag after a lab pass, rollback) |
| `docs/` | `updates-and-rollback.md`; `upstream-README.md` is upstream's README, kept for reference only |
| `ci/` | workflow files staged for the maintainer to copy into `.github/workflows/` |
| `examples/`, `screenshots/`, `brand/` | example scripts and images; `screenshots/NEEDED.md` lists shots still to take |

## Rules

- Work on a branch and open the pull request inside this fork, base `main`. Never push to `main`, and never open
  a pull request, issue or comment on the upstream repository.
- This repository is public from its first commit: commit messages and pull request text are written for a
  stranger - plain, complete, no private shorthand, no names or details of any real home.
- A commit may end with a `Co-Authored-By:` line for the agent; never a session link or other URL back to an
  agent session, in a commit or a pull request.
- US English in code, comments, documentation and commit messages.
- A pull request follows `.github/pull_request_template.md` and stays a draft until it is ready for review.
- Every pull request gets an independent adversarial review before it merges; each finding is fixed, or declined
  in writing with the reason.
- A change to permissions, approvals, sign-in, the sandbox, remote control or writes under `/config` states its
  safety reasoning in the pull request and comes with tests that fail without it.
- Keep the pins: the base image by digest, the packaged Codex CLI by version and checksum, no self-update
  (`codex-cli-worker/tests/test_fork_pins.py` checks them). Moving a pin is its own pull request.
- The app and the integration carry one version: `codex-cli-worker/config.yaml` and `manifest.json` change
  together, with a `## <version> (unreleased)` entry in `codex-cli-worker/CHANGELOG.md`.
- Update the documentation a behavior change touches, in the same pull request (the list is in CONTRIBUTING.md).
- Screenshots are never faked or mocked up; a missing one is listed in `screenshots/NEEDED.md`.
- Tests and fixtures use invented data only: no real tokens, device codes, logs or Home Assistant configuration.
- Keep the fork's differences from upstream small and separate, so an upstream release can still be merged.

## Release

Not released yet: nothing is tagged or published, and the release and image workflows stay disabled until the
first release is planned. When one is:

1. Set the same version in `codex-cli-worker/config.yaml` and `custom_components/codex_cli/manifest.json`; a fork
   version is upstream's version plus one more number, counted from 1 for each upstream release.
2. The version passes on a Home Assistant OS test machine, and its lab-pass record is committed.
3. The commit that removes `(unreleased)` from the changelog heading is the one tagged, by the maintainer, with
   `python scripts/fork_release.py tag <version>`; the release workflow then builds the images and the HACS ZIP.
4. A bad release is undone with `fork_release.py rollback`, never by deleting a tag.

`.github/workflows/upstream-watch.yml` opens one issue when upstream releases something newer.

## Do not touch

- `main`, tags and GitHub releases - the maintainer's, after review and a lab pass.
- `.github/workflows/` - an agent's token usually cannot push there; put the changed file in `ci/` and say so in
  the pull request. Never enable the release, image build or Pullfrog workflows.
- `codex-cli-worker/policy/` - what the packaged Codex is allowed to do on an install (`house-rules.md` becomes
  that Codex's own `AGENTS.md`). It is product code, not guidance for working here; change it only when the task
  is a policy change, with the review above.
- `docs/upstream-README.md`, `LICENSE`, `codex-cli-worker/web/assets/vendor/` - upstream's and third parties' text.
- Any real Home Assistant install: nothing in this repository is to be tested against a home that people live in.

## Read more

- `README.md` - what it does and how a person installs it.
- `CONTRIBUTING.md` - test setup in full, manual checks, what a pull request holds.
- `SECURITY.md` - reporting a vulnerability, and what the app can reach.
- `docs/updates-and-rollback.md` - how a release reaches an install, the lab pass, rollback.
- `codex-cli-worker/DOCS.md`, `VERIFICATION.md` and `SANDBOX_TESTING.md` (same folder) - options, the
  verification tools and the sandbox checks.
- `codex-cli-worker/CHANGELOG.md` - what each version changed.
