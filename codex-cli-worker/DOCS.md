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

## Permission modes

Each chat uses one of four modes. Pick it with the **Permissions** button.

| Mode | What happens |
|---|---|
| Default permissions | Codex edits files in your configuration folder. It asks you before anything that needs more access. |
| Auto-review | Like Default, but Codex's own reviewer answers the requests instead of you. |
| Read only | Codex reads files. Any change asks you first. |
| Full access | No sandbox and no questions. Off until an administrator turns it on in **Settings**. |

A request shows a card with the full command or change and two buttons, **Allow once** and **Deny**. Only a Home
Assistant administrator can answer. Read each card before you allow it.
More: [Sandbox](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#sandbox).

## Your files

- Your Home Assistant configuration is at `/config`, and `/share` is shared with other apps.
- Rules for your house go in **Settings** > **Workspace instructions**, the app's `AGENTS.md`. A new install gets a
  short starter file once. It is yours to change or delete.
- Chats and the files you attach to a message (PNG, JPEG, GIF or WebP) stay in the app's private storage, not in
  `/config`. Attachments are removed after 7 days or with the chat.
- Keep your own Home Assistant backups. The app's saved copies of changed files can be missing, and `secrets.yaml`
  and `.storage` are never copied.

More: [Backups](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#backups).

## Upgrading from a version before 0.4.0

Version 0.4.0 removed the built-in dashboard browser and its screenshots, the **Dashboard verification** Home
Assistant user it signed in as, and the **Enable built-in browser** and **Browser memory limit (MiB)** options. To
change a dashboard, Codex now reads its configuration back. The integration removes the old user on its first
start. Restart Home Assistant after the update.
More: [Upgrading from an earlier version](https://github.com/fatherdoctor/ha_codex/blob/main/codex-cli-worker/VERIFICATION.md#upgrading-from-an-earlier-version).

## Get help

Open the app's **Log** tab, download the integration diagnostics (**Devices & services** > Codex > three-dot menu),
remove anything private, and [open an issue](https://github.com/fatherdoctor/ha_codex/issues/new/choose). Steps:
[Reporting a bug](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md#reporting-a-bug).
For every option and feature, see [the app reference](https://github.com/fatherdoctor/ha_codex/blob/main/docs/app-reference.md).
