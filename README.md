# Codex for Home Assistant (fatherdoctor fork)

A Home Assistant app and integration that let one household member ask OpenAI's Codex, in plain words and from the
Home Assistant app, to explain the house or make a configuration change, with one confirmation per change.

[![License][license-badge]][license-url]

This is a fork of [moryoav/home-assistant-codex](https://github.com/moryoav/home-assistant-codex) (MIT), built on its
release **0.1.81**. **Status: pre-release.** Nothing from this fork is installed anywhere yet; the permission,
sign-in and recovery changes listed under [Versions](#versions) are still being built and reviewed.

## Table of contents

- [Features](#features)
- [Preview](#preview)
- [Installation](#installation)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Usage](#usage)
- [Versions](#versions)
- [Making contributions](#making-contributions)
- [License](#license)

## Features

- A chat page inside Home Assistant (Ingress), with saved conversations and a task queue.
- Codex works on the Home Assistant configuration folder inside its sandbox, saves a copy of each file before
  changing it, and asks Home Assistant to check the configuration after YAML edits.
- Answers Codex offers are shown as buttons, in the chat and on mobile app notifications.
- Sign-in with a ChatGPT account by device code, shown as a QR code in a Home Assistant notification.
- Dashboards inspected in a real browser, at desktop and mobile sizes.
- A local copy of the Home Assistant documentation, so lookups need no web search.
- **In this fork:** the Codex CLI is pinned (the packaged 0.166.0 build, checked by SHA-256) and cannot update
  itself; the base image is pinned by digest. A CLI or base image move is a release of this fork.

## Preview

Screenshots of this fork are not taken yet (see [screenshots/NEEDED.md](screenshots/NEEDED.md)). Upstream's chat
screens are in [examples/chat-ui](examples/chat-ui).

## Installation

You need Home Assistant OS or Supervised (the worker is an app), Home Assistant 2026.5 or later, and a ChatGPT plan
with Codex. Before signing in, turn on **Enable device code authorization for Codex** in ChatGPT
(**Settings** > **Security**).

1. **Add the app repository.** **Settings** > **Apps** > **Install app** > menu (top right) > **Repositories**, then
   add `https://github.com/fatherdoctor/ha_codex`.
2. **Install and start the app.** Install **Codex CLI Worker**, review its options, start it.
3. **Install the integration.** In HACS, add `https://github.com/fatherdoctor/ha_codex` as a custom repository
   (type **Integration**), download **Codex**, and restart Home Assistant. Manual: copy `custom_components/codex_cli`
   to `/config/custom_components/codex_cli` and restart.
4. **Add the integration.** **Settings** > **Devices & services** > **Add integration** > **Codex**. It finds the
   app by itself; there is no address or token to enter.
5. **Sign in.** In the app's page, **Settings** > **Sign in**; scan the QR code in the notification, or open the
   link, and enter the code.

Every app option is described in [codex-cli-worker/DOCS.md](codex-cli-worker/DOCS.md).

## Updating

The app and the integration carry the same version. Update the app from **Settings** > **Apps**, and the
integration from HACS, then restart Home Assistant. Home Assistant can take a backup of the app before an app update.

## Uninstalling

1. **Settings** > **Devices & services** > **Codex** > delete the integration entry.
2. Remove **Codex** in HACS (or delete `/config/custom_components/codex_cli`) and restart Home Assistant.
3. Uninstall **Codex CLI Worker** under **Settings** > **Apps**. Task logs stay in `/config/codex_tasks` until you
   delete that folder.

## Usage

Open **Codex** in the sidebar and type. The integration's entities, its actions (`codex_cli.*`) with YAML examples,
the task output format and the Assist example are documented upstream and still apply to this release: see
[docs/upstream-README.md](docs/upstream-README.md) and the
[integration README](custom_components/codex_cli/README.md).

## Versions

A fork version is upstream's plus one number: `0.1.81.1` is the first build of this fork on upstream 0.1.81. The
daily upstream watch ([ci/upstream-watch.yml](ci/upstream-watch.yml), moved to `.github/workflows/` on the default
branch) opens one issue when upstream releases something newer.

| Version | Changes |
|---|---|
| 0.1.81.2 (unreleased) | No full-access mode; a protected permission policy and house rules in the image; permission answers and the integration's actions only from Home Assistant administrators; a permission-prompt sensor. |
| 0.1.81.1 (unreleased) | Packaged Codex CLI 0.166.0 pinned by checksum, no self-update, base image by digest, links and maintainer for the fork. Details in the [changelog](codex-cli-worker/CHANGELOG.md). |

Planned for the fork's v1, not in this release: category denials (locks, alarm, garage, cameras, messages to people) enforced outside the model, a page for sign-in
and recovery without a terminal, and remote control from the ChatGPT app managed by the app.

## Making contributions

Issues and pull requests are welcome on this fork. Changes that are not specific to the fork belong upstream first;
see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, as upstream: [LICENSE](LICENSE) (copyright Yoav Mor). Fork changes by David Allison, under the same license.

[license-badge]: https://img.shields.io/github/license/fatherdoctor/ha_codex
[license-url]: LICENSE
