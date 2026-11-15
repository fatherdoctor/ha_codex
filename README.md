# Codex for Home Assistant

A Home Assistant app and integration that let one household member ask OpenAI's Codex, in plain words and from the
Home Assistant app, to explain the house or make a configuration change. In the default `workspace-write` mode,
Codex can edit the configuration folder without a prompt; shell networking and actions outside that workspace
require an approval request. Review each request before allowing it.

The assistant can act on any device the owner confirms. Put limits specific to your installation in `/config/AGENTS.md`. Codex uses its defaults and your workspace instructions; the app bundles no house rules. Its permission policy, approval checks and other enforced safeguards remain in place.

A ChatGPT account with Codex access is required. As of November 13, 2026, OpenAI lists Codex across ChatGPT plans, including Free and Go; limits and workspace access vary. See [OpenAI’s current availability](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan).

[![License][license-badge]][license-url]

This is a fork of [moryoav/home-assistant-codex](https://github.com/moryoav/home-assistant-codex) (MIT), built on its
release **0.1.81**. **Status: pre-release.** Nothing from this fork is installed anywhere yet; the permission,
sign-in and recovery changes listed under [Versions](#versions) are still being built and reviewed.

## Features

- A chat page inside Home Assistant (Ingress), with saved conversations and a task queue.
- Codex edits the configuration folder in a sandbox, attempts to save previous file copies, and can run a config check after YAML edits. Copies may be missing or unverified, are never made in `read-only` mode, and exclude credentials; keep your own backups. See [backup coverage and limits](codex-cli-worker/DOCS.md#backups).
- Answers Codex offers are shown as buttons, in the chat and on mobile app notifications.
- Sign-in with a ChatGPT account by device code, shown as a QR code in a Home Assistant notification.
- Dashboards inspected in a real browser, at desktop and mobile sizes.
- A local copy of the Home Assistant documentation, so lookups need no web search.
- **In this fork:** the Codex CLI and the base image are pinned and cannot update themselves (see [Versions](#versions)).

## Installation

You need Home Assistant OS or Supervised (the worker is an app), Home Assistant 2026.5 or later, and a ChatGPT plan
with Codex. Before signing in, turn on **Enable device code authorization for Codex** in ChatGPT
(**Settings** > **Security**).

1. **Add the app repository.** **Settings** > **Apps** > **Install app** > menu (top right) > **Repositories**, then
   add `https://github.com/fatherdoctor/ha_codex`.
2. **Install and start the app.** Install **Codex for Home Assistant**, review its options, and start it. At start
   the app installs the Codex integration of the same version in `/config/custom_components/codex_cli`.
3. **Restart Home Assistant once.** A notification asks for it: **Settings** > **System** > restart.
4. **Add the integration.** **Settings** > **Devices & services** shows **Discovered: Codex**; choose **Add**. It
   finds the app by itself; there is no address or token to enter. If nothing is shown, choose **Add integration** >
   **Codex**.
5. **Sign in.** Choose **Open Web UI** on the app page to open the chat (optionally turn on **Show in sidebar**).
   In the chat's own Settings, choose **Sign in**; scan the QR code in the notification, or open the link, and
   enter the code.

**Installing the integration yourself.** Turn off the app option **Install the Codex integration**
(`install_integration`), then install **Codex** from **Settings** > **Marketplace** > **Add from link** with
`https://github.com/fatherdoctor/ha_codex` (HACS custom repository on releases before 2026.11), or copy
`custom_components/codex_cli` to `/config/custom_components/codex_cli`, and restart. The app never changes an
integration folder that it did not install, or that was changed after it installed it; it says so once in a
notification.

Every app option is described in [codex-cli-worker/DOCS.md](codex-cli-worker/DOCS.md).

## Updating

The app and the integration carry the same version. Update the app from **Settings** > **Apps**; at its next start
it updates the integration it installed and asks for a restart of Home Assistant. An integration you installed
yourself is updated the same way you installed it, to the app's version. Home Assistant can take a backup of the
app before an app update.
How releases are tested and how a bad one is rolled back: [docs/updates-and-rollback.md](docs/updates-and-rollback.md).

## Uninstalling

1. **Settings** > **Devices & services** > **Codex** > delete the integration entry.
2. Uninstall **Codex for Home Assistant** under **Settings** > **Apps**. Task logs stay in `/config/codex_tasks` until you
   delete that folder.
3. Uninstalling the app does not remove the integration. Delete `/config/custom_components/codex_cli` (or remove
   **Codex** in **Settings** > **Marketplace** if you installed it there), and restart Home Assistant.

## Usage

Open **Codex** in the sidebar if enabled, or choose **Open Web UI** on the app page, and type. The integration's entities, its actions (`codex_cli.*`) with YAML examples,
the task output format and the Assist example are documented upstream and still apply to this release: see
[docs/upstream-README.md](docs/upstream-README.md) and the
[integration README](custom_components/codex_cli/README.md).

## Versions

A fork version is upstream's plus one number: `0.1.81.1` is the first build of this fork on upstream 0.1.81. A
daily workflow opens one issue when upstream releases something newer.

| Version | Changes |
|---|---|
| 0.1.81.3 (unreleased) | The app installs and updates its own integration (one install and one restart); setup from discovery asks for confirmation. |
| 0.1.81.2 (unreleased) | No full-access mode; a protected permission policy in the image, no bundled house rules and owner workspace instructions; permission answers and the integration's actions only from Home Assistant administrators; a permission-prompt sensor. |
| 0.1.81.1 (unreleased) | Packaged Codex CLI 0.166.0 pinned by checksum, no self-update, base image by digest, links and maintainer for the fork. Details in the [changelog](codex-cli-worker/CHANGELOG.md). |

This pre-release includes sign-in and recovery controls on the app page and optional remote control from the ChatGPT app. The protected policy and other enforced safeguards remain in place; installation-specific limits belong in the owner's workspace instructions. These features are still being reviewed.

## Auditing

[AUDITING.md](AUDITING.md) maps each protection to the code that enforces it and the tests that show it.

## Making contributions

Issues and pull requests are welcome on this fork. Changes that are not specific to the fork belong upstream first;
see [CONTRIBUTING.md](CONTRIBUTING.md). To try new versions before they are released to everyone, open an issue and
ask to join as a beta tester.

## License

MIT, as upstream: [LICENSE](LICENSE) (copyright Yoav Mor). Fork changes copyright The Backshop, under the same license.

[license-badge]: https://img.shields.io/github/license/fatherdoctor/ha_codex
[license-url]: LICENSE
