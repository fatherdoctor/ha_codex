# Codex for Home Assistant

Ask Codex, in a chat inside Home Assistant, to explain your setup or change its configuration. Detail lives in
[the app reference](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md).

## Install and sign in

1. Add `https://github.com/fatherdoctor/ha_codex` under **Settings** > **Apps** > **Install app** > menu >
   **Repositories**, then install and start this app.
2. Restart Home Assistant when the notification asks, then add **Discovered: Codex** under **Settings** >
   **Devices & services**.
3. Open the chat with **Open Web UI**. In ChatGPT, turn on **Enable device code authorization for Codex**
   (**Settings** > **Security**). In the chat, choose **Settings** > **Sign in**, open the link and enter the
   one-time code.

More: [Codex sign-in](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#codex-sign-in).

## Connect GitHub

This step is optional. Connect GitHub when you want Codex to fetch, pull, push or clone a repository for you, for
example to keep your configuration in version control. You do every step with a button in the app; you type nothing
about GitHub into the chat.

1. On GitHub, install the **Codex for Home Assistant** GitHub App on the repositories you choose. GitHub's own page
   does this, and you can change the set there later. The App asks for Contents read and write, and GitHub adds
   Metadata read.
2. In the chat, choose **Settings** > **Accounts and connections** > **GitHub**, then **Connect GitHub**. The card
   shows a code in large letters, a link to `github.com/login/device` and a QR code of that page.
3. Open the link, or scan the QR code with a phone, type the code and approve the App. The card checks every few seconds and shows your account and
   the repositories it reaches as soon as GitHub confirms. The code works for 15 minutes. After that, choose
   **Connect GitHub** again for a new one.
4. Codex reaches only the repositories that the App is installed on. To add one, choose **Add repositories** under
   the list, pick the repository on GitHub's page, then choose **Check now** and **Accept this access**.

If your organization does not allow the App, choose **Use a token instead**. On github.com, make a fine-grained
token (**Settings** > **Developer settings** > **Fine-grained tokens**) for **one** repository, with **Contents:
Read and write** and an expiry of 90 days or less. Paste the token and the `owner/repository` name, then choose
**Use this token**.

What to know:

- The app keeps the sign-in in its own private storage. In every permission mode except Full access, Codex cannot
  read the sign-in: it asks the app to run git for it. Full access runs Codex with no sandbox, so a command there
  can read the app's private storage, the sign-in included.
- **Clone** in the card copies a connected repository to `/share/<repository>`. **Link** points a git folder you
  already have under `/share` or `/homeassistant` at a connected repository. You can also ask Codex in the chat.
- GitHub refuses a push that changes `.github/workflows/` with this access. Change workflow files on GitHub or on
  another computer.
- **Sign out** only makes this app forget the sign-in. To revoke it, use the link the card shows on GitHub.
- A restore from a Home Assistant backup does not bring the sign-in back. Sign in again.

If the card shows a warning, it says what happened and which button to press. More:
[GitHub sign-in](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#github-sign-in).

## Permission modes

Each chat uses one of four modes. Pick it with the **Permissions** button.

| Mode | What happens |
|---|---|
| Ask for approval | Codex edits files in your configuration folder. It asks you before anything that needs more access. |
| Approve for me | Like Ask for approval, but Codex's own reviewer answers the requests instead of you. |
| Read only | Codex reads files. Any change asks you first. |
| Full access | No sandbox and no questions. Off until an administrator turns it on in **Settings**. |

You can change the mode in the middle of a chat. The next message runs in the new mode, and Codex is told of the change.

A request shows a card with the full command or change and two buttons, **Allow once** and **Deny**. Only a Home
Assistant administrator can answer. Read each card before you allow it.
More: [Sandbox](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#sandbox).

## Your files

- Inside the app your Home Assistant configuration is at `/homeassistant`, as in the Claude add-on (before 0.6 it was
  `/config`; Home Assistant itself still calls it `/config`), and `/share` is shared with other apps.
- Codex starts each chat in its working folder, `/media/.agents/codex`, for its notes and drafts. It may write in
  `/homeassistant`, `/share`, `/media` and `/addon_config` (the app's own folder) without a request. The automatic
  backup leaves `/media` out unless you include it.
- Rules for your house go in **Settings** > **Workspace instructions**, the app's `AGENTS.md`. A new install gets a
  short starter file once. It is yours to change or delete.
- The app also sends built-in instructions with every chat: folders, tools, logs, secrets and GitHub. Settings shows
  them read-only above the editor. An update of the app updates them. Your `AGENTS.md` takes priority over them.
- Chats and the files you attach to a message (PNG, JPEG, GIF or WebP) stay in the app's private storage, not in
  `/homeassistant`. Attachments are removed after 7 days or with the chat.
- In the Home Assistant app on a phone or tablet, **Download** on an image or a file opens the share sheet; choose
  **Save Image** or **Save to Files** there. Where the app has no share sheet, open Home Assistant in a web browser to
  download.
- Keep your own Home Assistant backups. The app's saved copies of changed files can be missing, and `secrets.yaml`
  and `.storage` are never copied. Codex can read and change both, like the rest of `/homeassistant`; the built-in
  instructions tell it to keep secrets out of chat, other files and commits.

More: [Backups](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#backups).

## Scheduled runs

A scheduled run sends a chat's message again on a timetable, such as every morning at 7. Open a chat's menu and choose
**Schedule**, pick **Hourly**, **Daily**, **Weekly** or **Custom**, and choose **Create schedule**. Each run is the
next message in that chat, so Codex remembers everything said before. If the chat was deleted or archived, or Codex
cannot resume it, the run starts a new chat.

- Codex can **propose** a schedule. A repeating proposal stays off until an administrator approves it under
  **Settings** > **Scheduled runs**. A one-time wakeup that Codex proposes is on at once: it runs one time, in the
  same chat. Pause or delete it in the same place.
- A scheduled run still asks before a step that needs more access. Open its chat to answer with **Allow once** or
  **Deny**. The run waits for your answer only until its **Time limit for a task** (one hour by default) ends. Then
  the run stops, and the step it asked about does not run.
- Times follow your Home Assistant time zone. A run waits while another chat is working.

More: [Scheduled runs](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#scheduled-runs).

## Backups and old chats

- Before Codex changes, moves or deletes a file under `/homeassistant`, it copies the file. These copies are a safety net,
  not a check, and a copy can be missing. Keep your own Home Assistant backups.
- `secrets.yaml` and everything under `.storage` are never copied. To undo a change to one of them, restore a Home
  Assistant backup.
- **Keep backups for days** on the **Configuration** tab sets how long the copies stay (7 by default).
- **Delete chats after (days without a message)** removes a chat nobody has used for that long (14 by default).
  Pinned chats stay. Near the end, the chat shows the day it will go.
- **Full backup before every message** also archives your configuration before each message. It is off by default
  because it makes every message wait.

More: [Backups](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#backups).

## Model and usage

On the **Configuration** tab, **Model** sets the model for new chats. **Account default** lets Codex choose for your
account. A chat can also pick its own model and reasoning level with the model button. Which models you see depends
on your account.

The sidebar of the chat shows how much of your ChatGPT usage is left, for 5 hours and for 7 days, with the time it
resets. Plans differ, so some accounts show only the weekly figure.

More: [Model](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#model).

## Upgrading from a version before 0.4.0

Version 0.4.0 removed the built-in dashboard browser and its screenshots, the **Dashboard verification** Home
Assistant user it signed in as, and the **Enable built-in browser** and **Browser memory limit (MiB)** options. To
change a dashboard, Codex now reads its configuration back. The integration removes the old user on its first
start. Restart Home Assistant after the update.
More: [Upgrading from an earlier version](https://github.com/fatherdoctor/ha_codex/blob/main/codex-cli-worker/VERIFICATION.md#upgrading-from-an-earlier-version).

## What the sandbox still keeps back

In Ask for approval and Approve for me, the sandbox keeps only these limits. Read only keeps them too, and also makes every file
read-only. **Allow once** on a card gets past each of them for that one action.

- **The app's own private files are hidden.** These are `/run/s6` and, in `/data`, the app's settings, its API token,
  the remote control and GitHub sign-ins and the schedule database. The rest of `/data` is read-only, except the
  chat's backup and files folders. Why: these tokens can answer the app's own permission cards or call Home
  Assistant directly, so a task that reads them could get around the cards. They are the app's state, not your files.
- **A `.git`, `.codex` or `.agents` folder directly in `/homeassistant`, `/share` or `/media` is read-only.** For example,
  `/homeassistant/.git` needs a card, but `/share/<repository>/.git` does not. Why: this is Codex's own sandbox rule, not
  the app's. In a project, these folders hold history, settings or skills that Codex loads, so a change there could
  change what Codex does next. `/media/.agents` is a writable place of its own, so Codex's working folder is not
  affected.

More: [Sandbox](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#sandbox).

## Get help

Open the app's **Log** tab, download the integration diagnostics (**Devices & services** > Codex > three-dot menu),
remove anything private, and [open an issue](https://github.com/fatherdoctor/ha_codex/issues/new/choose). Steps:
[Reporting a bug](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#reporting-a-bug).
For every option and feature, see [the app reference](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md).
