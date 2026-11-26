# Security Policy

Codex for Home Assistant can read and modify a Home Assistant configuration folder. Please treat security and privacy issues with care.

Codex's checks of Home Assistant (entity state, the configuration check, Core logs and dashboard readback) run
through the worker with its automatic Supervisor token; the app creates no Home Assistant user. Core log reads
need both an administrator token and the paired worker token, and read only Core's logs. Known credential files
are excluded from backups and snapshots, but inline secrets and old snapshots need separate care. See
[verification security](codex-cli-worker/VERIFICATION.md). Version 0.4.0 removed the dashboard browser and its
**Dashboard verification** user; the integration removes that user the first time it sets up
with the worker.

SSH connections use user-managed keys and server permissions. Keys stored under
`/data/.ssh` are accessible to Codex tasks and are excluded from app backups;
this storage is not a secret vault isolated from the AI process. Remote key
restrictions must be enforced by the SSH server. The Default and Read only
permission modes ask an administrator before shell networking. Auto-review lets
Codex's own reviewer answer. Full access runs without the sandbox and asks
nothing; it is off until an administrator turns it on in the app's Settings, and
the `codex_sandbox` option does not accept it. Review each approved command and
keep connections short-lived. See [SSH setup and
limits](docs/app-reference.md#ssh-connections).

From 0.3.6 the app has no command check of its own: Codex's permission modes and the
sandbox are the protection. A command you allow (once, or for this chat) runs outside the sandbox and can
read private files or change the app's settings, and the app does not scan what Codex
writes in `/homeassistant`. Read each permission card before you allow it.

To check these protections in the code, see [AUDITING.md](AUDITING.md).

## Supported Versions

Security fixes are intended for the latest published release and the current `main` branch.

Development and canary testing may happen on the `dev` branch. Older releases are not actively supported unless a maintainer says otherwise in a specific issue or release note.

## Reporting a Vulnerability

Please do not open a public issue with exploit details, working proof-of-concept code, private logs, tokens, device codes, or personal Home Assistant configuration.

Report a vulnerability privately: open the repository's **Security** tab, choose **Report a vulnerability**, and describe the issue there. Only the maintainers can see the report.

If the button is missing, open a minimal public issue that says you have a security concern and asks for a private channel. Put no details in that issue.

## What to Include

When reporting a vulnerability privately, include as much of the following as you can safely share:

- A clear description of the issue.
- The affected version or commit.
- Whether the issue affects the worker app, the custom integration, or both.
- Steps to reproduce in a safe test environment.
- The expected impact.
- Any relevant logs with secrets and private configuration removed.
- Suggested mitigations, if you know them.

## Security-Sensitive Areas

Please use extra care when changing or reviewing:

- Codex CLI authentication and logout behavior.
- Worker API token generation, storage, rotation, or validation.
- Home Assistant Ingress validation.
- Home Assistant Supervisor or Core API calls, including the bundled Home Assistant MCP server (`hass_mcp.py`,
  `hass_mcp_launch.py`), which holds the Supervisor token and must never pass it to a command Codex runs.
- File reads and writes under `/homeassistant`.
- Task execution, cancellation, user replies, and task logs.
- AppArmor, container permissions, networking, mounted paths, and app/add-on configuration.

## Responsible Testing

Test security reports and fixes only in an environment you own or have permission to use. Do not attempt to access, modify, or disclose another person's Home Assistant instance, configuration, credentials, logs, or devices.

## Public Disclosure

Please give the maintainers reasonable time to investigate and fix confirmed vulnerabilities before publishing details publicly. Coordinated disclosure helps protect users while a fix is prepared.
