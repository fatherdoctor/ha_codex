# Codex for Home Assistant

Use Codex CLI to inspect and edit your configuration through a chat in Home Assistant.

The assistant can act on any device the owner confirms. Put limits specific to your installation in the app's `AGENTS.md` (Settings > Workspace instructions). Codex uses its defaults and your workspace instructions; the app bundles no house rules. A new install that has no `AGENTS.md` gets a short starter file there once, which is yours to change or delete. Its permission policy, approval checks and other enforced safeguards remain in place.

The app provides a responsive chat UI with saved conversations, a resizable desktop sidebar, and a mobile chat drawer. It also provides a token-protected local API for the Codex integration, device-code sign-in, backup tracking, task logs, and notifications when work needs input or fails.

Codex runs in a sandbox: `workspace-write` (the default) or `read-only`. A chat runs without one only in the Full access mode, which an administrator turns on in Settings. Commands and file changes that need extra access ask a Home Assistant administrator to **Allow once** or **Deny** in the chat. See [sandbox modes, safeguards, and permission requests](https://github.com/fatherdoctor/ha_codex/blob/main/codex-cli-worker/DOCS.md#sandbox).

## Checks and dashboard changes

**Codex checks its own work during a task:** fresh entity states, the configuration check, Core logs and a dashboard's configuration read back from Home Assistant. To change a dashboard, it makes the change, reads it back and reports what changed. No tool renders or screenshots a dashboard. See [verification tools and limits](https://github.com/fatherdoctor/ha_codex/blob/main/codex-cli-worker/VERIFICATION.md).

For setup, security notes, and action examples, see the [repository README](https://github.com/fatherdoctor/ha_codex/blob/main/README.md) and [app documentation](https://github.com/fatherdoctor/ha_codex/blob/main/codex-cli-worker/DOCS.md).

## History and statistics

Codex can analyze Home Assistant state history and recorder statistics through separate read-only MCP tools. They use the worker's automatic authentication and work in sandboxed modes without shell approval or the `HA_TOKEN` option. Queries support multiple IDs, explicit time windows, and five-minute, hourly, daily, weekly, and monthly statistics. Results are saved in private temporary JSON files so Codex can calculate over the data without large tool outputs being cut off. Query limits and any incomplete series are reported explicitly. See [the tools and limits](https://github.com/fatherdoctor/ha_codex/blob/main/codex-cli-worker/DOCS.md#historical-data-and-statistics).

## Local Home Assistant documentation

The app keeps a local copy of the official Home Assistant documentation, refreshed after each Home Assistant update, so Codex can look things up without a web search. Web search stays available for custom integrations and cards. See [local documentation](https://github.com/fatherdoctor/ha_codex/blob/main/codex-cli-worker/DOCS.md#local-home-assistant-documentation).

## SSH connections

The image includes the OpenSSH client for optional connections to existing SSH servers. Ask Codex to create a dedicated key and connection configuration under `/data/.ssh`, then install the public key on the remote server. Use `ssh -F /data/.ssh/config <connection> <command>` so the client selects the persistent configuration explicitly. No Home Assistant token is needed.

Each SSH command goes through the **Allow once / Deny** approval flow. Remote permissions are controlled by the SSH server and account. The `.ssh` directory is excluded from app backups and must be set up again after a restore. See [setup, restricted keys, and limits](DOCS.md#ssh-connections).

## Conversations

Open an existing chat to continue with its saved Codex context, or choose **New chat** for a separate conversation. Account controls and workspace instructions are under **Settings**.

The chat formats Markdown in your messages and in Codex's answers. Wrap code in triple backticks to send it as a code block; inline code, headings, lists, tables, and links are formatted too.

While one chat is working, messages you send in other chats wait in a queue and start in order when it finishes. You can edit or remove a waiting message until it starts.

When Codex needs a decision, it can offer up to three answers as buttons under its question, and on the notification if you use a mobile app notify service. Pick one, or type a different answer.

When Codex generates an image, the chat shows it with a full-size link and a download button. Image generation depends on your account's available tools and allowance. Images stay with their conversation across restarts.

Attach screenshots, photos, or design mockups to a message with the paperclip button, by pasting, or by dropping files onto the composer. Codex sees them with your message, and they stay with the conversation.

Hover over a chat and use its **⋯** button, right-click the row, or long-press it on a phone to rename, pin, share, archive or delete the conversation. **Share** downloads the chat, or its last lines, as a Markdown file with the steps Codex printed. Pinned chats stay at the top of the list, and deleting asks for confirmation first.

Use the model and reasoning chip below the message box to customize each conversation: it opens a reasoning slider, and the model name in that panel opens the model list. The `model_pin` option is empty by default; when you set it, it limits the picker and runs to that model, and clearing it brings back the other available models. Choices are saved across restarts and apply to the next message. A chat that has no model of its own shows the model set in the app options, or the one Codex picks when that option is `default`. Choosing that model in the menu restores the app's model setting; the reasoning picker's reset button separately restores the app's reasoning setting. The slider only offers reasoning levels supported by the selected model.

![Saved conversations in the desktop chat UI](https://raw.githubusercontent.com/fatherdoctor/ha_codex/main/screenshots/chat-light-desktop.png)
