<p align="center"><img src="brand/courtyard/logo.svg" alt="Codex for Home Assistant" width="320"></p>

# Codex for Home Assistant

[![License][license-badge]][license-url]

Ask OpenAI's Codex, in plain words, to explain your Home Assistant or to change its configuration. You talk to it in a
chat page inside Home Assistant. It reads your files and makes the change you ask for. In the default mode it edits
files in your configuration folder without a question, and it asks for your permission before a step that needs more
access. It is for the household member who looks after Home Assistant and can read a configuration file, but does not
want to write every automation by hand.

**Status: pre-release.** The app image is published for each version, but no version is called stable yet. Install it
on a test Home Assistant first.

## What it does

- A chat page in Home Assistant, with saved conversations and a task queue. Codex works in a sandbox.
- In the default mode, Codex edits files in your configuration folder without a question. It asks before a command
  that uses the network, a write outside your configuration folder, `/share` and `/media`, or a change to a device. Only an
  administrator can answer. The other [permission modes](#permission-modes) ask less or more.
- Codex is told to save the earlier copy of a file before it changes it, and the chat lists the copies it saved. A
  copy can be missing, and `secrets.yaml` and the files under `.storage` (dashboards kept there too) are never
  copied, so keep your own Home Assistant backups ([Backups](codex-cli-worker/DOCS.md#backups)).
- After a change to YAML, the app asks Home Assistant to check the configuration, unless you turned that option off
  or Home Assistant cannot run the check. The chat says which ([Validation](codex-cli-worker/DOCS.md#validation)).
- The Codex program is pinned and never updates itself.

## Quick start

1. Add `https://github.com/fatherdoctor/ha_codex` under **Settings** > **Apps** > **Install app** > menu >
   **Repositories**.
2. Install and start **Codex for Home Assistant**.
3. Restart Home Assistant when the notification asks.
4. Add **Discovered: Codex** under **Settings** > **Devices & services**.
5. Open the chat and [sign in](#sign-in).

Details and other ways to install: [Installation](#installation). If a step fails because a download is not there,
see [If a download is missing](#if-a-download-is-missing).

## Contents

- [Installation](#installation)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [An example, step by step](#an-example-step-by-step)
- [Permission modes](#permission-modes)
- [Safety](#safety)
- [Sign-in](#sign-in)
- [Scheduled runs](#scheduled-runs)
- [Using it from Assist and automations](#using-it-from-assist-and-automations)
- [Troubleshooting](#troubleshooting)
- [Versions](#versions)
- [Auditing](#auditing)
- [Reporting a problem and testing](#reporting-a-problem-and-testing)
- [Credit](#credit)
- [Contributing](#contributing)
- [License](#license)

## Installation

You need Home Assistant OS or Supervised (the worker is an app), Home Assistant 2026.5 or later, and a ChatGPT account
with Codex access. As of November 13, 2026, OpenAI lists Codex across ChatGPT plans, including Free and Go. Limits
and workspace access vary. See [OpenAI's current availability](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan).

1. **Add the app repository.** **Settings** > **Apps** > **Install app** > menu (top right) > **Repositories**. Add
   `https://github.com/fatherdoctor/ha_codex`.
2. **Install and start the app.** Install **Codex for Home Assistant**, read its options and start it. At start, the
   app installs the Codex integration of the same version in `/config/custom_components/codex_cli`.
3. **Restart Home Assistant once.** A notification asks for it: **Settings** > **System** > restart.
4. **Add the integration.** **Settings** > **Devices & services** shows **Discovered: Codex**. Choose **Add**. It
   finds the app by itself; there is no address or token to enter. If nothing is shown, choose **Add integration** >
   **Codex**.
5. **Sign in.** Choose **Open Web UI** on the app page to open the chat. You can turn on **Show in sidebar** there.
   Then follow [Sign-in](#sign-in).

The chat works without the integration. The integration adds the entities, the `codex_cli.*` actions, repairs and
notification buttons. The web page opens only through Home Assistant Ingress; the app opens no port on your network.

**Installing the integration yourself.** Turn off the app option **Install the Codex integration**
(`install_integration`) first. Then do one of these, and restart Home Assistant:

- Home Assistant 2026.11 or later: connect a GitHub account, then open **Settings** > **Marketplace** >
  **Custom repositories** and add `https://github.com/fatherdoctor/ha_codex` with the category **Integration**.
  Accept the warning shown the first time, and install **Codex**.
- Older releases: add the same custom repository in HACS as an **Integration**.
- By hand: copy `custom_components/codex_cli` to `/config/custom_components/codex_cli`.

The app never changes an integration folder that it did not install, or that was changed after it installed it. It
says so once in a notification.

### If a download is missing

The app image for each version is published at `ghcr.io/fatherdoctor/codex-cli-worker`, for `amd64` and `aarch64`.
The app's own install of the integration (steps 2 to 4) needs nothing else.

- **The app does not install or update.** The image for that version is not published yet. Wait and try again, or
  [report it](#reporting-a-problem-and-testing).
- **Marketplace or HACS does not offer Codex, or the download fails.** That route needs a GitHub release of this
  repository that holds `codex_cli.zip`, and a version can have an image and no such release. Let the app install the
  integration, or copy the folder by hand.

Every app option is in [codex-cli-worker/DOCS.md](codex-cli-worker/DOCS.md).

Put rules for your house in the app's `AGENTS.md`, kept in its own `addon_config` folder and not in `/config`. The chat has an editor for it under **Settings** > **Workspace instructions** >
**Workspace rules**. Codex uses its defaults and your workspace instructions; the app bundles no house rules. A new
install that has no `AGENTS.md` gets a short starter file there once, which is yours to change or delete.

## Updating

The app and the integration have the same version number. Update the app in **Settings** > **Apps**. At its next
start, it updates the integration it installed and asks for a restart of Home Assistant. An integration you installed
yourself is updated the same way you installed it, to the app's version. Let Home Assistant take a backup of the app
before an app update.

How releases are tested and how a bad release is undone: [docs/updates-and-rollback.md](docs/updates-and-rollback.md).

## Uninstalling

1. **Settings** > **Devices & services** > **Codex**. Delete the integration entry.
2. Uninstall **Codex for Home Assistant** under **Settings** > **Apps**.
3. Uninstalling the app does not remove the integration. Delete `/config/custom_components/codex_cli` (or remove
   **Codex** in **Settings** > **Marketplace** if you installed it there), and restart Home Assistant.

The chats and their logs are in the app's private folder (`/data/codex_tasks`), which uninstalling the app removes.

## An example, step by step

You ask: "Turn the porch light on at sunset and off at 11 pm."

```mermaid
flowchart LR
    A[You write in the chat] --> B[Codex reads your configuration]
    B --> C[Codex writes the automation]
    C --> D{Does the step need your permission?}
    D -- no --> E[Codex reloads automations and names the action]
    D -- yes --> F[Permission needed card: Allow once or Deny]
    F --> E
    E --> G[Chat shows what changed and which files can be restored]
```

1. You write the request. Codex reads your files and the local copy of the Home Assistant documentation. It may ask a
   question and offer two or three short answers as buttons.
2. Codex edits the automation file. In the default `workspace-write` mode, an edit inside your configuration folder
   needs no permission. Codex is told to save the earlier copy first, and the chat lists the copies that were saved.
3. Codex asks to reload automations. A permission card shows the action, and you choose **Allow once** or **Deny**.
4. When Codex ends its turn, the app asks Home Assistant to check the configuration, unless the `config_check`
   option is off or Home Assistant cannot run the check. The chat shows the result.
5. You read the summary. If you do not like it, ask Codex to undo it, or restore the saved earlier copy if the chat
   lists one.

Screenshots of these screens are listed in [screenshots/SCREENSHOTS.md](screenshots/SCREENSHOTS.md).

## Permission modes

Each chat picks one of the four modes of the ChatGPT app: Default, Auto-review, Read only or Full access. Each mode is
only Codex's own settings.

| Mode | What Codex can do without asking |
|---|---|
| Default | Edit files in your configuration folder. Shell networking and a write outside your configuration folder, `/share` and `/media` need your permission. |
| Auto-review | As Default, but Codex's own reviewer answers each request instead of you. |
| Read only | Read files. A write, a device change or shell networking needs your permission for that one action. |
| Full access | Everything, without a sandbox and without asking. Off until an administrator turns it on in Settings. |

A request shows a **Permission needed** card with the full command or file change, and the buttons **Allow once**
and **Deny**. Only a Home Assistant administrator can answer it.

## Safety

Codex's own permission modes and the sandbox are the protection, as in the Claude add-on. The app adds no command
check of its own.

- **The sandbox.** Commands run with their own user, mount, process and network space. The sandbox hides
  `secrets.yaml`, Home Assistant's sign-in store and the app's own private files.
- **Complete cards.** A command or file change that is incomplete, too large or redacted can be denied but never
  allowed: its card has no **Allow once** button.
- **A protected policy.** The allowed modes come from a file inside the app image. Options and chats cannot loosen it.

What the safeguards do not do:

- They do not promise that every action is safe or can be undone. **Read each permission card before you allow it.**
- An allowed command runs outside the sandbox, with the app's file and network access. Nothing stops it from
  reading a private file or changing the app's settings.
- The app does not check what Codex writes in `/config`. An edit can add a shell command that Home Assistant runs
  later; review what a chat changed before you reload or restart.
- Backups depend on Codex following its instructions, and `secrets.yaml` and the files under `.storage` are never
  copied. Keep your own Home Assistant backups.

The app asks for no host networking, Docker access or elevated Supervisor roles, and it keeps AppArmor on. Your
`/config` folder is mounted for writing, because editing it is the purpose of the app. More detail:
[Security Model](codex-cli-worker/DOCS.md#security-model), [Sandbox](codex-cli-worker/DOCS.md#sandbox) and
[Backups](codex-cli-worker/DOCS.md#backups).

## Sign-in

Codex uses your ChatGPT account.

1. In ChatGPT, open **Settings** > **Security** and turn on **Enable device code authorization for Codex**. In a
   managed workspace, an administrator may need to allow device-code sign-in; see
   [Codex Sign-In](codex-cli-worker/DOCS.md#codex-sign-in).
2. In the chat, open **Settings** and choose **Sign in**.
3. Open the link, or scan the QR code (on a phone, under **Show QR code**), and enter the one-time code. The code stays on screen, with the time it still
   works, until the sign-in ends or you cancel it. If it runs out, choose **Get a new code**.

When the sign-in is missing, ended or rejected, a banner returns above the chat, the integration raises a Home
Assistant repair, and the **Signed in** sensor turns off. A good sign-in clears all three. The Codex program does not
report when a sign-in will end, so the app shows no countdown. Sign out any time with **Settings** > **Sign out**.

## Scheduled runs

Open a chat's menu and choose **Schedule**. You save a name, a prompt, a model and a trigger: hourly, daily, weekly,
or a custom interval, cron rule or single time, in your Home Assistant time zone. Each run starts a new chat.
**Settings** > **Scheduled runs** lists every schedule.

- A schedule you save is approved when you save it. Codex can only propose one; it stays off until an administrator
  reads the full prompt and approves it.
- A scheduled run asks for permission like any other run and waits for you in its chat.
- Limits: 100 approved schedules, 24 runs per schedule per UTC day, and 64 scheduled runs per UTC day.

Details: [Scheduled runs](codex-cli-worker/DOCS.md#scheduled-runs).

## Using it from Assist and automations

The integration adds actions that scripts, automations and Assist can call.

| Action | What it does |
|---|---|
| `codex_cli.start_task` | Start a Codex task with a prompt. |
| `codex_cli.reply_task` | Answer a task that waits for input. |
| `codex_cli.continue_task` | Continue a saved conversation. |
| `codex_cli.get_task`, `codex_cli.list_tasks` | Read a task or the list of tasks. |
| `codex_cli.cancel_task` | Stop a task. |
| `codex_cli.start_login`, `codex_cli.logout`, `codex_cli.get_login_status` | Manage the sign-in. |

Who can call them: a call that carries a Home Assistant user is refused unless that user is an administrator. A call
with no user is allowed. An automation that runs by its own trigger has no user, so it can start a task while nobody
is there. An automation or script that a person starts carries that person. A task started this way still uses the
normal permission cards; the integration cannot answer them. A notification about a chat shows only while the chat
waits for you, and goes away when the wait ends; a completed answer or a failure sends none. The one exception is a block by OpenAI's
safety filter, which sends one notification that stays until you dismiss it. Set `notify_service` in the app options for
phone pushes, or leave it empty for persistent notifications.

The event `codex_cli_task_result` carries the result for automations. The full list of entities and actions, the task
output format and an Assist example are in the [integration README](custom_components/codex_cli/README.md). A
starter Assist script is in [examples/scripts.yaml](examples/scripts.yaml).

## Troubleshooting

| What you see | What to do |
|---|---|
| A line at the top of the chat says the integration is missing, old or not connected | Restart Home Assistant if a notification asks for it, then add **Codex** under **Devices & services**. If you installed the integration yourself, update it to the app's version. |
| A banner offers **Sign in** | The sign-in is missing, ended or rejected. Sign in again. |
| A request waits and nothing moves | A permission card is open. Open the chat and answer it. |
| A task stops with no output for 10 minutes | The stall limit (`task_stall_seconds`) ended it. A quiet long command can trigger it. Raise the limit. |
| A task fails before it starts, with a sandbox error | The host may not allow the sandbox. See [the sandbox test steps](codex-cli-worker/SANDBOX_TESTING.md). |
| An answer is wrong or a change broke something | Restore the saved earlier copy that the chat lists, or use a Home Assistant backup. |

## Versions

A version has three numbers, such as `0.3.6`. The first two versions, `0.1.81.1` and `0.1.81.2`, had four: the number
of the release that the project started from ([Credit](#credit)), plus one more.

| Version | Changes |
|---|---|
| 0.3.6 | The app's own command checks are removed: Codex's permission modes and the sandbox are the protection. Four permission modes, with Full access off until an administrator turns it on. A notification shows only while a chat waits for you. |
| 0.3.0 (unreleased) | GitHub sign-in on the page (device sign-in, clone and link under /share or /config, every push through the push guard, a Repairs issue when access changes). The app installs and updates its own integration (one install and one restart); setup from discovery asks for confirmation. |
| 0.1.81.2 (unreleased) | No full-access mode (changed in 0.3.6); a protected permission policy in the image, no bundled house rules and owner workspace instructions; permission answers and the integration's actions only from Home Assistant administrators; a permission-prompt sensor. |
| 0.1.81.1 (unreleased) | Codex CLI 0.166.0 pinned by checksum, no self-update, base image pinned by digest, links and maintainer for this repository. |

The full list is in the [changelog](codex-cli-worker/CHANGELOG.md).

## Auditing

[AUDITING.md](AUDITING.md) maps each protection to the code that enforces it and the tests that show it.

## Reporting a problem and testing

This is a pre-release. Install it on a test Home Assistant first, not on one that runs your house.

To report a problem, use the [bug report form](https://github.com/fatherdoctor/ha_codex/issues/new?template=bug_report.yml).
Include the app, integration and Home Assistant versions, the steps that cause it, the app log lines around the time,
and the integration diagnostics file. Remove tokens, device codes, private URLs and personal configuration before you
post. Steps to collect logs: [Reporting a bug](codex-cli-worker/DOCS.md#reporting-a-bug).

There is no public beta. Each version is tested on a Home Assistant OS test machine before it is released. To report a
security problem, use the **Report a vulnerability** button on the Security tab. See [SECURITY.md](SECURITY.md).

## Credit

This project is a fork of [moryoav/home-assistant-codex](https://github.com/moryoav/home-assistant-codex) by Yoav Mor
(MIT). It started from release 0.1.81 of that project and has its own versions and its own code since then.

This project is not affiliated with OpenAI or with Home Assistant.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT: [LICENSE](LICENSE). Copyright Yoav Mor and The Backshop.

[license-badge]: https://img.shields.io/github/license/fatherdoctor/ha_codex
[license-url]: LICENSE
