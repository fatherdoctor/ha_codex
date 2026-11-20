# Contributing to Codex for Home Assistant

Thank you for your interest in improving Codex for Home Assistant.

This project has two main parts:

- `codex-cli-worker`: the Home Assistant app/add-on that runs Codex CLI tasks against `/config`.
- `custom_components/codex_cli`: the Home Assistant custom integration that exposes entities, actions, diagnostics, and setup flow support.

Contributions are welcome, including bug reports, documentation improvements, compatibility fixes, security hardening, and feature ideas.

## Before You Start

Please open an issue before starting large or risky changes. This helps avoid duplicated work and gives maintainers a chance to discuss the approach first.

Small fixes, documentation updates, and clearly scoped bug fixes can usually go straight to a pull request.

## Reporting Bugs

When reporting a bug, please include:

- Both the worker app and Codex integration versions.
- Your Home Assistant version.
- Whether you installed through HACS, manually, or from the development branch.
- Your architecture, such as `amd64` or `aarch64`.
- Clear steps to reproduce the issue.
- The date and time of the failed interaction, with your time zone.
- Relevant logs (see "Getting your logs" below).
- What you expected to happen.
- What actually happened.

### Getting your logs

Logs are usually the fastest way to find a bug. On Home Assistant 2026.11, collect whichever of these apply:

- **Worker app log:** go to Settings, Apps, Codex for Home Assistant, then the Log tab. Copy the lines around the time of the failure.
- **Integration diagnostics:** go to Settings, Devices & services, Codex, open the three-dot menu, and choose Download diagnostics. Attach the downloaded file to the issue. It carries the anonymized details of the last interactions; read the whole file before you post it.
- **Home Assistant Core log:** go to Settings, System, Logs and type `codex` in the filter box. Copy the matching lines.

Please remove secrets, tokens, URLs, device codes, personal paths, and private Home Assistant configuration before sharing logs or screenshots.

## Suggesting Features

Feature requests are welcome. Please describe:

- The problem you want to solve.
- The workflow you expect to use in Home Assistant.
- Whether the change belongs in the worker app, the custom integration, or both.
- Any security or privacy concerns the feature may introduce.

Because this project can modify a Home Assistant configuration folder, features that expand write access, automation behavior, authentication, or remote control should include a clear safety rationale.

## Development Setup

Clone the repository:

```bash
git clone https://github.com/fatherdoctor/ha_codex.git
cd ha_codex
```

The repository layout is:

```text
codex-cli-worker/              Home Assistant app/add-on worker
custom_components/codex_cli/   Home Assistant custom integration
examples/                      Example Home Assistant scripts
.github/workflows/             GitHub Actions validation, test, build, and release workflows
```

For local Home Assistant testing, install or copy the integration into:

```text
/config/custom_components/codex_cli
```

For worker app testing, add this repository as a Home Assistant app repository. To use a development branch, append `#branch-name` to the repository URL. That branch selection applies only to the worker app; install integration changes from the branch manually.

## Pull Request Guidelines

Please keep pull requests focused. A good pull request should:

- Explain what changed and why.
- Mention any related issue.
- Keep unrelated formatting or refactoring out of the change.
- Update documentation when behavior, installation, options, actions, or entities change.
- Include screenshots when changing Home Assistant UI or notifications.
- Avoid committing secrets, credentials, device codes, task logs, or private Home Assistant configuration.

If you change the worker app version, update the relevant version fields consistently.

## Testing

Before opening a pull request, test the parts you changed as much as practical.

Run the full Python suite from the repository root:

```sh
pytest
```

The Home Assistant tests need Linux and Python 3.14. Use WSL on Windows because Home Assistant imports Unix-only modules such as `fcntl`. The [verification workflow](.github/workflows/verification.yaml) pins compatible Home Assistant Core and `pytest-homeassistant-custom-component` versions and lists the remaining Python and browser dependencies. Use a matching pair from that matrix when preparing a test environment. The script `codex-cli-worker/tests/install_frontend_test_deps.py` installs the extra dependencies used by the frontend tests.

For the worker's standalone Python tests, without the Home Assistant pytest fixtures, run:

```sh
python -m unittest discover -s codex-cli-worker/tests -v
```

The suite includes `test_code_checks.py`, which runs `python scripts/code_checks.py` against the repository. You can
also run that script alone; it needs only the standard library and takes about a second. It fails when a source file
grows past 1,000 lines without an entry in its exemption list, or when Python code reads a module global by a
computed name (`globals()[name]` and the like). It also prints, as a warning only, how many places in the tests patch
a name inside `server.py`. The five rules behind these checks are listed under "Code standard" in `AGENTS.md`.

For a local chat UI check, start `python codex-cli-worker/tests/web_fixture.py`, redirecting its output to a file, then run `node codex-cli-worker/tests/browser_smoke.cjs` in another terminal with Playwright on `NODE_PATH`. Set `CODEX_CHAT_BROWSER=msedge` to use installed Edge when Playwright's Chromium is unavailable. The fixture uses temporary data and never contacts Home Assistant or starts Codex. Stop it after testing and before starting another instance on port 9137. See [verification checks](codex-cli-worker/VERIFICATION.md#validation) and [sandbox checks](codex-cli-worker/SANDBOX_TESTING.md) for those features.

For integration changes, verify that Home Assistant can:

- Load the `codex_cli` integration.
- Complete the config flow.
- Create the expected entities.
- Call the relevant actions.
- Reload or restart without errors.

For worker app changes, verify that the app can:

- Start successfully.
- Serve the Ingress UI.
- Report status.
- Start and track a task.
- Handle login, logout, cancellation, and task input flows when relevant.

For documentation-only changes, please check that links, paths, and examples are accurate.

Codex is reached three ways: the chat page, the ChatGPT app through remote control, and the `codex_cli.start_task`
action. A fix that works on one of them may be missing on another. [Ways in](codex-cli-worker/DOCS.md#ways-in) lists
the Home Assistant tools each one gets, and `codex-cli-worker/tests/test_surface_matrix.py` checks that list against
the real launch of each. If your change adds, removes or moves a tool, update both. In the pull request, name the ways
in that you tested.

Automated tests do not cover every installed dashboard, device, or HAOS kernel. Include clear manual test notes for behavior the suite does not exercise.

## Security Notes

This project is powerful by design. It gives Codex access to the Home Assistant configuration folder so it can inspect and modify dashboards, automations, scripts, integrations, and related files.

Please be especially careful with changes involving:

- Authentication or token handling.
- Home Assistant Supervisor or Core API calls.
- Ingress validation.
- File writes under `/config`.
- Task execution, cancellation, and user replies.
- AppArmor, container permissions, networking, or mounted paths.

Do not include real credentials, API tokens, device codes, private logs, or personal Home Assistant configuration in issues or pull requests.

If you believe you found a security vulnerability, please do not open a public issue with exploit details. Follow the reporting process in [SECURITY.md](SECURITY.md).

## Documentation

Please update documentation when changing user-facing behavior. Depending on the change, this may include:

- `README.md`
- `custom_components/codex_cli/README.md`
- `codex-cli-worker/README.md`
- `codex-cli-worker/DOCS.md`
- `examples/scripts.yaml`

Use plain, direct language and include Home Assistant examples where they make the workflow easier to understand.

## Releases

The release workflow builds the HACS ZIP and the app images when a version tag is pushed. Ordinary pushes to
`main` run validation and tests only. They do not make a release or publish images. A change to documentation
only does not need a new version.

Version bumps happen only in the release pull request, never in a feature merge. The add-on store offers an update
as soon as `config.yaml` on `main` names a newer version, so a version raised in a feature pull request offers a
release that does not exist yet. A feature pull request adds its notes under the `## <version> (unreleased)` heading
in `codex-cli-worker/CHANGELOG.md` and leaves both version lines as they are on `main`. The release pull request is
the one whose title starts with `Release <version>`, for example `Release 0.3.6`; the
[Version rule workflow](.github/workflows/version-rule.yml) runs `scripts/check_version_bump.py` on every pull request
to `main` and fails any other pull request that changes either version line. If a wrong version reaches `main`, the
pull request that sets it back fails this check; the maintainer merges it anyway and its text says why.

A stable version is released from `main`. A beta is released from the `beta` branch, for the Home Assistant OS
test machine only; there is no public beta. The version rules, what a beta install is, and how `beta` follows `main`
are in [Updates and rollback](docs/updates-and-rollback.md#versions).

### A stable release

1. Open a pull request titled `Release <version>`. In it, set the same version in `codex-cli-worker/config.yaml` and
   `custom_components/codex_cli/manifest.json`.
2. Add the notes under `## <version> (unreleased)` in `codex-cli-worker/CHANGELOG.md`. Validation of branches and
   pull requests accepts that heading. Tagging and the release build refuse it.
3. Remove ` (unreleased)` from the heading. Merge to `main` and wait for green CI.
4. Install that commit on a Home Assistant OS test machine and test it. Then add the test record
   `release/lab-passes/v<version>.json` on `main`.
5. Tag and push: `python3 scripts/fork_release.py tag <version> --push`. The script tags the tested commit.

### A beta for the test machine

1. Check out `beta` and merge `main` into it. If the branch does not exist yet, make it from `main` and set the
   name in `repository.yaml` to `Codex for Home Assistant (beta)`.
2. Do steps 1 to 4 above with a beta version, for example `0.2.0-beta.1`, on `beta` instead of `main`. The test
   record goes on `beta`.
3. Tag and push from the `beta` checkout: `python3 scripts/fork_release.py tag 0.2.0-beta.1 --push`.

How to undo a bad release is in [Updates and rollback](docs/updates-and-rollback.md).

## Working together

- Be respectful and specific. Say what you saw, what you expected, and what you tried.
- Never post tokens, device codes, private logs, or personal configuration in an issue, pull request, or comment. Remove them first, and tell a maintainer if you posted one by mistake.
- Maintainers may close, lock, or limit a thread that harms the project or the people in it.
- Security problems go through [SECURITY.md](SECURITY.md), not a public issue.
