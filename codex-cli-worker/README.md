# Codex for Home Assistant

Ask OpenAI's Codex, in a chat inside Home Assistant, to explain your setup or change its configuration.

## Quick Start

1. Install and start this app.
2. Restart Home Assistant when the notification asks, then add **Discovered: Codex** under **Settings** >
   **Devices & services**.
3. Choose **Open Web UI** and sign in with your ChatGPT account.

## Requirements

- Home Assistant OS or Supervised, version 2026.5 or later.
- A ChatGPT account with Codex access.

## Features

- A chat with saved conversations, image attachments and four permission modes.
- Codex edits your configuration folder and asks before a step that needs more access. Only an administrator can answer.
- Codex checks changed YAML and saves the earlier copy of a file. Keep your own Home Assistant backups too.
- The app keeps a local copy of the Home Assistant documentation, and can read state history and statistics.

## Setup

Put rules for your house in **Settings** > **Workspace instructions** (the app's `AGENTS.md`). Codex uses its defaults and your workspace instructions; the app bundles no house rules. A new install that has no `AGENTS.md` gets a short starter file there once, which is yours to change or delete.
The other options are on the app's **Configuration** tab. More: [Documentation](DOCS.md).

## Using ChatGPT for Home Assistant

Describe what you want in plain words, such as "Turn the porch light on at sunset." Codex shows what it changed. A
permission card appears when it needs more access: read it, then choose **Allow once** or **Deny**.
