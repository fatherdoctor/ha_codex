# Codex for Home Assistant: app reference

The short guide is [DOCS.md](../codex-cli-worker/DOCS.md). This page holds the detail behind it. The app runs Codex CLI tasks against the Home Assistant config folder mounted at `/config`.

## Checks and dashboard changes

**Codex checks its own work during a task.** It can find an entity by name, read fresh entity states, read Core
logs, run the configuration check and read a dashboard's configuration back, without a Home Assistant password or
a token you create. To change a dashboard, Codex makes the change, reads the configuration back and reports what
changed; the dashboard backup and the undo line stay as they are. No tool renders or screenshots a dashboard. See
[verification tools, authentication and limits](../codex-cli-worker/VERIFICATION.md).

Versions before 0.4.0 opened dashboards in a built-in browser and kept screenshots in the chat. Version 0.4.0
removed that browser, the **Dashboard verification** Home Assistant user it signed in as, and the **Enable built-in
browser** and **Browser memory limit (MiB)** options. The integration removes the old user on its first start;
see [Upgrading from an earlier version](../codex-cli-worker/VERIFICATION.md#upgrading-from-an-earlier-version). The integration
changed, so restart Home Assistant after the update.

## Historical data and statistics

The separate **Home Assistant data** MCP server lets Codex read state history and recorder statistics. It is available automatically in `workspace-write` and `read-only`. It does not ask for shell network approval or require the `HA_TOKEN` option. The worker makes these fixed, read-only API requests with its Supervisor credential, which is never sent to Codex. No extra MCP setup is needed.

Ask, for example: “Compare my kitchen temperature, humidity and extractor fan history yesterday and suggest activation thresholds. Only analyze the measurements.” Codex finds the relevant entity IDs in the configuration, queries the data, and explains its analysis. These tools do not change configuration, call services, or run automations. Verification remains a separate tool.

Five tools are available under `home_assistant_data`. **`exchange_info`** takes no arguments and changes nothing: it gives Codex the details of the current message (see [What a chat thread holds](#what-a-chat-thread-holds)). **`ask_owner`** and **`report_failure`** change nothing either: they tell the page that Codex needs an answer or that the task failed (see [Human Input](#human-input)). The two data tools are:

- **`history`** reads `/api/history/period/<start_time>` for explicit `entity_ids`, `start_time`, and `end_time`. It returns timestamped state strings, including `unknown` and `unavailable`, and the initial state at the window boundary. Attributes are omitted; the existing entity verification tool can read units separately. Example: `{"entity_ids":["sensor.kitchen_temperature","fan.extractor"],"start_time":"2026-10-01T00:00:00+03:00","end_time":"2026-10-02T00:00:00+03:00"}`.
- **`statistics`** reads `recorder/get_statistics_metadata` and `recorder/statistics_during_period` through Home Assistant's WebSocket API, for explicit `statistic_ids`, `start_time`, and `end_time`. `period` is `5minute`, `hour` (default), `day`, `week`, or `month`. Optional `types` selects `mean`, `min`, `max`, `sum`, `state`, `change`, or `last_reset`, where supported. It returns display units and bucket start/end timestamps in UTC. Example: `{"statistic_ids":["sensor.kitchen_temperature"],"start_time":"2026-10-01T00:00:00+03:00","end_time":"2026-10-08T00:00:00+03:00","period":"hour","types":["mean","min","max"]}`.

Use ISO timestamps with seconds and a timezone offset or `Z`. Results normalize timestamps to UTC; aggregated day/week/month buckets still follow Home Assistant's configured timezone and can extend beyond the exact requested boundaries. A statistics `sum` is cumulative, not consumption during a bucket; request `change` for that. Unsupported numeric fields are absent or null, not zero.

Each tool saves its result in a private temporary JSON file under `/data/ha-data`, outside `/config`. The tool returns a small receipt with `file.path`, the file's byte count and SHA-256, total available/saved point counts, series status counts, and `truncated`. The file contains the query window, per-ID metadata and all saved points under `series[ID].points`. Codex can read these files in every sandbox mode without approval. They are excluded from app backups and removed when the exchange ends, including cancellation and failure. Files left by a stopped worker are removed on startup. A follow-up message must query again rather than reuse a previous exchange's file path.

Codex loads the file with Python and calculates over the saved points, printing only the relevant summary or a small slice. This keeps large datasets out of the model's context without silently losing their middle. Printing the whole file with `cat`, or reading it all into a tool response, hits the CLI's output limit again. For example, a compact calculation over a statistics file is:

```python
import json
from pathlib import Path

data = json.loads(Path(receipt["file"]["path"]).read_bytes())
series = data["series"]["sensor.kitchen_temperature"]
if series["truncated"]:
    raise ValueError("Query a narrower window before analyzing the complete period")
means = [point["mean"] for point in series["points"] if point.get("mean") is not None]
print({"buckets_with_mean": len(means), "lowest_mean": min(means) if means else None})
```

The app includes `openpyxl` in the Python environment that Codex uses. Codex can read existing Excel workbooks in read-only mode and create them in a writable workspace. Both work offline.

Limits: up to 16 distinct IDs; seven days for raw history and 366 days for statistics; 64 queries per exchange, with one in progress at a time. `max_points` defaults to 1,000, must be at least the number of requested IDs, and accepts up to 5,000. Points are shared fairly across requested IDs, with unused shares and integer remainders redistributed. Each series in the file reports `available_points`, `returned_points`, and `truncated`. A truncated series contains only its first points and cannot represent the whole window. Query a smaller window, fewer IDs, or coarser statistics before analyzing a complete period. Responses from Home Assistant are limited to 1 MiB, each saved file to 4 MiB, and temporary files to 32 MiB per exchange. Receipts are limited to 8 KiB. Large requests fail with guidance to narrow the query; a full storage budget requires a new exchange. Statistics queries are also rejected before reaching Core when their expected bucket count exceeds 10,000. Files avoid the model's output limit; bounded queries still protect Home Assistant, memory and disk use. Longer analyses can use several smaller time windows, calculating compact summaries from each file.

`no_statistics` means the ID has no recorder statistics metadata; `no_data` means no points were returned for the period. Empty history can mean an entity is unrecorded, unavailable to the API, or its history has been purged. It does not establish a constant state. Home Assistant's recorder settings determine retention and which sensors provide statistics; these tools cannot recover purged measurements or add statistics to an unsupported entity.

Each exchange has a separate temporary data credential, revoked on completion and rejected on cancellation, restart, or turn replacement. The data queries do not consume the verification budget or appear as passed verification checks. Configured credentials are redacted before the data is saved. Receipts and any measurements printed during analysis are part of the conversation like other tool results; the full file is not automatically sent to the model. Sensor values are untrusted data, never instructions. The tools accept no arbitrary URLs, API paths, WebSocket commands, SQL, database files, or output paths.

API references: [Home Assistant REST history](https://developers.home-assistant.io/docs/api/rest/#get-apihistoryperiodtimestamp) and the [Core recorder WebSocket command definitions](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/recorder/websocket_api.py).

## Home Assistant MCP

The option **Enable Home Assistant MCP** (`home_assistant_mcp`, on by default) gives Codex the tools of
[hass-mcp](https://github.com/voska/hass-mcp) 0.6.0, the Home Assistant MCP server that the Claude Code app for
Home Assistant also bundles. With these tools Codex finds entities by name, area or domain, reads automations,
dashboards, history, statistics and the error log without a search through files, and, when you allow it,
controls a device, calls a service, restarts Home Assistant or edits a dashboard.

- Codex gets the tools in a chat on the chat page, in a task from `codex_cli.start_task`, in a scheduled run and
  under remote control ([Ways in](#ways-in)).
- The tools that only read change nothing, so Codex uses them without asking, in every permission mode.
- Each call of a tool that changes something needs an answer: `entity_action`, `call_service_tool`,
  `restart_ha` and the dashboard tools (`set_dashboard_config`, `add_card`, `update_card`, `remove_card`,
  `move_card`, `add_view`, `remove_view`, `update_view`, `restore_dashboard`). The request shows on the chat page
  as a card with the tool's arguments in full and what the call does, the same as for the `call` tool
  ([Changes to the house: the `call` tool](#changes-to-the-house-the-call-tool)). Allow once runs that one call;
  Codex asks again before the next one. Your permission mode decides who answers, as for the `call` tool: in
  Default and Read only, Codex asks you; in Auto-review, Codex's reviewer can allow the call without asking you;
  in Full access, the call runs without a question ([Permission modes](#permission-modes)).
- A card can show **Allow once** only when it shows the whole call. A write longer than 32,000 characters
  shows Allow turned off. A whole dashboard of ordinary size can be that long: about 70 small cards reach
  32,000 characters. Codex can still change the dashboard with the card and view tools.
- In a scheduled run nobody watches the chat, so a request waits on the chat page until you answer it.
- The tools show what Home Assistant returns. That includes attributes such as a camera's picture link, which
  holds a short-lived access token. Your `AGENTS.md` can tell Codex how to treat such values.
- The server uses the app's Supervisor token. Codex starts it outside the sandbox, and no command that Codex
  runs in the sandbox can read the token: on the chat page the server reads it from a folder the sandbox hides,
  and under remote control the relay process holds it for its whole run and passes it to the server in a variable
  that commands do not inherit. A command that runs outside
  the sandbox (Full access, or an approved request for more access) can read the token.
- If Home Assistant does not answer, the server fails to start and the chat starts without it.
- The image installs hass-mcp and each package it needs at a fixed version, checked by hash
  (`hass-mcp.requirements.txt`). It adds about 60 MB to the image.

Turn the option off to remove the tools. The change takes effect when the app restarts.

## Local Home Assistant documentation

From **0.1.70**, the worker keeps a copy of the official Home Assistant
documentation in private app storage. The `exchange_info` tool tells Codex to search
those files before using web search for Home Assistant documentation.
Web search remains available for
custom integrations, custom cards, and anything the documentation does not cover.

The copy is the Markdown source of www.home-assistant.io, from the `current`
branch of [home-assistant/home-assistant.io](https://github.com/home-assistant/home-assistant.io).
It contains the integration pages, the action, trigger, condition, and template
function references, the dashboard card pages, and the automation, script,
blueprint, and configuration guides. Images, blog posts, and changelogs are not
downloaded. The files are stored under `/data/ha-docs`; their size depends on
the upstream documentation. The copy is left out of app backups because it can
be downloaded again.

When the copy is downloaded:

- **When the app first starts**, in the background. Startup does not wait for it.
- **After a Home Assistant update.** Every five minutes, and when the app
  starts, the worker compares the version of Home Assistant Core with the one
  the copy was downloaded under. It reads that version from a file in the
  configuration folder, so the check makes no network request. When the version
  has changed, it downloads a new copy in the background.
- **After a failed download**, the worker keeps the existing copy and tries
  again an hour later.

The copy is not refreshed on a schedule. While Home Assistant stays on the same
version, the worker keeps the copy it has and makes no documentation requests.
A new copy is put to use when the next message starts, never while a task is
reading the old one. Until the first download finishes, tasks use web search as
before.

Each download brings the documentation of the latest Home Assistant release. If
you update to a version that is not the latest, the documentation can describe
features your installation does not have yet.

**Local Home Assistant documentation** (`local_docs`) in the app's
**Configuration** tab is on by default. Turn it off to stop the downloads and
have Codex use web search only. The authenticated `/health` response reports the
copy under `local_docs`: `enabled`, `available`, `commit`, `fetched_at`,
`ha_version` (the Home Assistant version the copy was downloaded under), and the
last download `error`.

The download is an anonymous Git request to GitHub. It sends no Home Assistant
data and no credentials; GitHub can see the host's public IP address. Symbolic
links in the repository are stored as plain files, so the copy cannot point at
other files on your system. Codex is told to use the files as reference
material and not to take anything in them as an instruction to it. The
documentation is published by its authors under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/). The worker
downloads it to your system; it is not part of this app's image.

## Distribution

This app is distributed from:

```text
https://github.com/fatherdoctor/ha_codex
```

Prebuilt images are published to `ghcr.io/fatherdoctor/codex-cli-worker` for `amd64` and `aarch64`, one tag for each version. Home Assistant pulls the image; nothing is built on your machine. The project is a pre-release: no version is called stable yet.

The integration is inside the image, and the app installs it (see [Integration install](#integration-install)). Installing the integration from Marketplace or HACS instead needs a GitHub release of the repository that holds `codex_cli.zip`. A version can have an image and no such release; then let the app install the integration, or copy the folder by hand.

## Security Model

The web UI is intended to be opened only through Home Assistant Ingress. The app does not publish its HTTP port to the LAN by default, and the Flask server rejects spoofed Ingress requests unless they come from the Home Assistant Ingress proxy address.

Programmatic API calls still require the internal worker API token unless they are proxied through authenticated Home Assistant Ingress. The Home Assistant Codex integration discovers the internal app hostname automatically through Supervisor metadata and provisions the worker token through Supervisor-managed app stdin.

The app does not request host networking, Docker API access, full access, host PID/UTS access, privileged kernel capabilities, or elevated Supervisor roles. It keeps AppArmor enabled and ships a custom `apparmor.txt` profile. The `/config` mount is intentionally read-write because editing Home Assistant configuration is the core purpose of the app.

For sandboxed modes, Codex uses Bubblewrap user, mount, PID, and network namespaces. Only the dedicated Bubblewrap setup binary enters a nested AppArmor profile that permits namespace setup. A wrapper forces Bubblewrap to drop all Linux capabilities before the generated command starts. On hosts that deny mounting a fresh `/proc`, the bundled wrapper normalizes a narrowly recognized probe error so Codex can select its own no-proc fallback. The worker then checks that a real sandboxed command succeeds before allowing tasks to start. The fallback retains the other sandbox restrictions but can expose process metadata from the app container; it does not grant host PID access. See [the workaround and host verification steps](../codex-cli-worker/SANDBOX_TESTING.md).

## API Token

The API token protects the worker HTTP API. The app stores it in private app storage. The Home Assistant `Codex` integration provisions and rotates it automatically through Supervisor-managed app stdin. You do not need to view, copy, or configure this token.

The token is not your OpenAI or ChatGPT credential. Codex authentication is still handled separately with `codex login`.

## Integration install

From **0.3.0** the app installs and updates the Codex integration itself, so one install and one restart of
Home Assistant set up both parts. The app image carries the integration of the same release. At each start,
before the worker starts, the app compares it with `/config/custom_components/codex_cli`:

| The folder | What the app does |
|---|---|
| Missing | Writes the integration, then shows a notification: restart Home Assistant to finish setup. |
| Written by the app, from another copy, and not changed since | Keeps the old folder in the app's storage (`/data/integration_backup`), writes the new copy and swaps the two by renaming, then asks for a restart. |
| Written by the app, from this copy, and not changed since | Nothing. |
| Installed another way (Marketplace, HACS, by hand, or a symbolic link), or changed after the app wrote it | Nothing. A notification says so once for each app version; the app removes it once the folder is its own again. |

The app knows its own folder by the marker file `.installed_by_app.json` in it, which names the app version and
a digest of the files it wrote. If any file in the folder differs from that digest (another installer copied
over it, or someone edited or removed a file), the folder is no longer the app's and the app leaves it alone.
To move to the Marketplace or HACS, turn the option below off first. If a copy fails, for example on a full
disk, the old folder stays in place and a notification says what failed. If Home Assistant cannot show the
restart notification (for example while it is starting), the app posts it again at its next start. The step
never stops the app from starting. The app log has one line for each start, beginning `Integration install:`.

The app writes nothing else in `/config` except its work folder, `/config/.codex_cli_install`. The new copy is
written there and the old folder is moved there during the swap. It is outside `custom_components`, so Home
Assistant never loads from it, and the app empties it at every start while the option below is on. If it
cannot be emptied, the app log says so.

**Going back to the previous version:** the copy the app replaced is kept in the app's own storage
(`/data/integration_backup/codex_cli`), which every backup of the app includes. To go back, restore the app's
previous version from the backup Home Assistant offers before an app update. If that version is 0.3.0 or
later, at its next start the app writes the integration of that version over its own folder and asks for a
restart. An earlier version does not install the integration, so the newer integration stays: turn the option
below off, copy `/data/integration_backup/codex_cli` back to `/config/custom_components/codex_cli` by hand, and
restart Home Assistant.

After the restart, the app's Supervisor discovery shows **Discovered: Codex** under **Settings** > **Devices &
services**. The app announces itself only once Home Assistant has loaded the integration, so a fresh install
logs no `Cannot find integration codex_cli` error before the restart. Choose **Add**, then **Submit**; the integration is added only after you confirm.

**Update banners on the chat page.** Two banners above the chat say what still needs you. The app installs and
restarts nothing itself.

| Banner | When it shows |
|---|---|
| **Version X is installed. Restart Home Assistant to finish.** | The integration in `/config/custom_components/codex_cli` has a version that Home Assistant has not loaded. Home Assistant keeps the integration it loaded at its last start, so this shows after the app writes a new version, and after the first install. The app asks Home Assistant which version it loaded (the WebSocket command `manifest/get`), so it never guesses. With the option below off, this banner never shows. |
| **Update available: version X** | Supervisor offers a newer version of the app. The banner links to the app's Info page, where you choose **Update**. |

**Dismiss** hides a banner for that version, in that browser; a newer version shows it again. If Home Assistant or
Supervisor cannot answer, the page shows no banner.

**Option:** **Install the Codex integration** (`install_integration`, default on). Turn it off to install and update
the integration yourself; the app then reads and writes nothing in `custom_components`. Discovery is announced
either way, once Home Assistant has loaded the integration.

**Uninstalling:** uninstalling the app leaves the integration in place, so that removing the app does not change
Home Assistant behind your back. Delete the integration entry, uninstall the app, then delete
`/config/custom_components/codex_cli` and restart Home Assistant.

## Integration check

The app's web UI works without the **Codex** integration, but some features need it. From **0.1.71**, the web UI shows a line at the top of the chat, above the messages, in three cases:

- **Not installed:** there is no `/config/custom_components/codex_cli/manifest.json`. See [Installation](https://github.com/fatherdoctor/ha_codex#installation).
- **Out of date:** the version in that file is below the app's general minimum integration version. The line names the version to update to. Restart Home Assistant after updating.
- **Installed but not connected:** the files are there, but the integration has not called the app for two minutes. It normally calls every 30 seconds. Restart Home Assistant if you have not done so since installing the integration, then add **Codex** under **Settings** > **Devices & services**.

With integration **0.1.71** or newer, status polling keeps running even if all its entities are disabled. If you turn off polling in the integration's system options, the app can show "not connected" while the integration is still loaded. Turn polling back on for this check.

The line cannot be dismissed. It goes away by itself, within a minute, once the integration is installed, up to date, and connected. During the first two minutes after the app starts, a missing call is not reported, because the integration may still be reconnecting.

The version is read from the installed files. After an update of the integration, Home Assistant keeps running the previous version until it restarts, and the app cannot see that.

This is a general connection check, not a feature-by-feature compatibility check. Its minimum is currently **0.1.69**; newer features need newer integration versions. Update the app and integration together even if no warning is shown.

API: `GET /status` has `integration` with `state` (`ok`, `starting`, `not_installed`, `outdated`, or `not_connected`), `version` (the installed version, empty when the integration is not installed or its version cannot be read), and `minimum_version`.

## Model

On the app's **Configuration** page, choose **Model** for new chats. **Account default** lets Codex choose the model for your signed-in account. Select a listed model to use it by default, or select `custom` and enter the exact name in **Custom model (when Model is Custom)**. The list is refreshed with releases. Use Custom for a newly available model between releases. Availability depends on your account. The bundled Codex CLI is **0.166.0**, pinned by version and checksum.

After an upgrade, the app keeps your saved `codex_model`, even if it is missing from the list. While **Model** is **Account default**, that saved name still applies and the startup log explains how to switch. To follow your account again, clear **Custom model** and select **Account default**.

**Model pin** overrides the configuration choice and limits every run to one model. A chat that explicitly requests a different model or resumes a turn on one gets an error before Codex runs. Clear the pin or set it to `none` to use **Model** again. Versions before 0.4 shipped `gpt-6.1-sol` as the default pin. The first start of 0.4 clears that value once and keeps any value you set afterward.

The worker treats the legacy `gpt-5.3-codex` value as `default`. The retired `gpt-5.5` value still starts for existing installations and runs `gpt-5.6-sol`; it is not offered in the chat's model picker.

`model_reasoning_effort` controls how much reasoning Codex asks supported models to use for each task. The app passes it for each exchange, through the app-server turn. It is free text, not a dropdown, so a level that a new model adds works with no app update. Type the level name in lowercase. A model that does not support the level runs on `medium`. At the shipped value, `medium`, a chat that follows the model Codex picks for the account (Account default, with no saved custom model or pin) runs on the level Codex gives that model, as the ChatGPT apps do; any other value applies to every model that offers it. Common values are:

- `minimal`: legacy value; no current model supports it, so it runs as `medium`.
- `low`: lighter reasoning.
- `medium`: balanced; the shipped value, which follows Codex's own level for the account's default model (see above).
- `high`: more reasoning, usually slower and more quota-intensive.
- `xhigh`: extra reasoning where the selected model supports it.

The bundled models also support `max`, and most support `ultra`. The chat page shows the levels each model supports; see [Per-conversation model and reasoning](#per-conversation-model-and-reasoning). The worker maps `minimal` to `medium`, including when the model is `default`. See the [Codex models guide](https://learn.chatgpt.com/docs/models) for current availability.

`reasoning_summary` controls the reasoning summaries shown in the activity list under your latest message. It is passed with each app-server turn. Summaries must be requested for them to appear, so the default is `concise`. This setting changes the reported summaries, not the selected reasoning effort.

- `concise`: short headlines such as "Checking the automation". This is the default.
- `detailed`: asks for fuller summaries; current models still mostly return headlines.
- `none`: no reasoning in the activity list. Commands, file edits, searches, progress notes, and tool calls are still shown.

There is no chat UI control for this setting; change it in the app configuration.

## Task time limits

Two options stop a task that runs too long. Both are in seconds, on the app's Configuration tab.

- **Task timeout** (`task_timeout_seconds`, default `3600`, one hour) is the longest one message may run, from the moment Codex starts on it. The time a permission request waits for your answer counts. When the limit is reached, the worker stops Codex and the task fails with "Codex timed out after 3600 seconds." You can send another message in the same chat to continue. If `0` is saved, the worker uses 3600.
- **Stall limit** (`task_stall_seconds`, default `600`, ten minutes) stops a task when Codex sends nothing for that long while no permission request waits. `0` turns it off. See [Permission requests](#permission-requests).

Example: a task that rebuilds a large dashboard and waits 50 minutes for your answer to a permission card has 10 minutes left. For work like that, set `task_timeout_seconds: 7200`.

## Keep Codex running

**Keep Codex running** (`warm_start`, default on) keeps one Codex process running between messages. Each chat that
you used recently stays loaded in that process, with its tools. A follow-up message then starts its turn at once.
Without the option, the app starts Codex again for each message and loads the chat from its saved history.

- **Loaded chats.** At most 3 chats stay loaded. When a fourth chat needs room, the app unloads the chat that was used
  longest ago. An unloaded chat keeps its history and loads again at its next message. A chat that works is never
  unloaded.
- **Memory.** While it waits, the app holds one Codex process and, for each loaded chat, the small Python programs
  of its tools. The app adds no memory limit of its own. The Info tab of the app shows its memory use; turn the option
  off if the box is short of memory.
- **When the process starts again.** The app compares what Codex was started with before each message: the
  options, the ChatGPT sign-in, the Home Assistant address and token, `AGENTS.md`, and the app and Codex versions.
  When one changed, the process starts again before the message, so an edit to `AGENTS.md` reaches the next message.
  A process that works on a message is never stopped for this; the restart waits until the message ends.
- **Failures.** When the process cannot take a message, the app starts Codex for that message as it does with the
  option off, and writes one line with the reason in the task log. When the process stops during a message, that
  message fails and a new process starts. After 3 such stops in 10 minutes, the app uses the option-off way for one
  hour. The task log of a message that fails, times out or stalls holds what Codex wrote to its error output during
  that message.
- **Stop and limits.** **Cancel**, the task timeout and the stall limit stop only that chat's turn. The process and
  the other loaded chats stay.
- **Remote control** keeps its own Codex process, as before.

A change to the option takes effect when the app restarts.

## Validation

After every task the worker compares the `/config` tree with the list of files it recorded before the run, then validates what changed in two passes. This comparison also includes files written by Home Assistant or another editor during the task.

1. **Syntax.** Added and changed YAML files are parsed with Home Assistant's YAML loader, and JSON and `.storage` files are parsed as JSON. Cookie jars that integrations keep under `.storage` in the LWP text format are checked as cookie jars instead, without rewriting them: Home Assistant's iCloud jars, which are the files under `.storage/icloud/` ending in `.cookiejar`, and any other `.storage` file that starts with the `#LWP-Cookies-` header, as the jars of custom integrations such as iCloud3 do. Malformed cookie jars are reported without including their contents in errors. Any parse error fails the task.
2. **Home Assistant's configuration check.** From **0.1.56**, when YAML files outside `.storage` were added, changed, or deleted and the syntax pass succeeded, the worker calls `POST /api/config/core/check_config`, the same check as Tools. An `invalid` result fails the task with Home Assistant's error text. The check needs the Supervisor's Core API access the app already has, and can take some seconds on large configurations; the worker waits up to three minutes. If the check cannot run, because Core is restarting or the API is unreachable, the task still completes and its details say the change is applied but unverified. Set `config_check` to `false` to skip this pass.

When validation fails, the task details list the saved previous version of each affected file under `/data/codex_tasks/<task_id>/turns/<turn_id>/backups/`, so a broken edit can be restored by copying the file back. Files the task created have no previous version and are listed separately, and so are affected files that have no saved copy. See [Backups](#backups) for where the copies come from and how long they are kept.

Dashboard auto-save skips storage files that failed validation and reports the skip in `lovelace_results`. A YAML failure does not block saving an unrelated, valid dashboard file.

The chat shows the outcome under the answer: a passing check as a short confirmation, a failing check with Home Assistant's error, and an unavailable check as a note. `GET /tasks/<task_id>`, `codex_cli.get_task`, and the `codex_cli_task_result` event carry `config_check` with `result` (`valid`, `invalid`, `unavailable`, `skipped` when no YAML changed or syntax already failed, or `disabled`), `errors`, and `warnings`, and `recovery_files` with `path` and `copy` per affected file. An entry without a copy has a `reason` of `no_backup` or `excluded_credentials`, unless the task created the file. A configuration check does not prove that an automation behaves as intended; it only confirms Home Assistant would load the configuration.

## What a chat thread holds

A conversation from the chat page is a Codex thread, and the ChatGPT app shows the same thread when remote control is on. From 0.3.3 the page sends its fixed rules as Codex developer instructions when it starts a thread. The thread's first message is your own text, so the app shows your words as the message and as the title, not the page's rules.

Details that change from one message to the next are not part of the rules: the backup folder, the chat's files folder (see [Files Codex makes for you](#files-codex-makes-for-you)), the documents and images you attached, and the notes on direct API calls (the `HA_TOKEN` option), the local documentation and GitHub. Codex reads them from the `exchange_info` tool before its first command, file change or tool call in a message, and when your message can refer to a file, a document or something it cannot see in the chat, such as "Summarize this" with a document attached. A message that Codex can answer from what it knows and from the chat, such as a greeting or a one-number reply, needs no tool call, so it gets its answer in one step. Codex keeps the developer instructions a thread started with, so an update of this app changes the rules of a new thread; an older thread keeps its rules until Codex compacts it.

A thread that this page did not start has no page rules: a thread started in the ChatGPT app, or one started by 0.3.2 or earlier. A message from the page on such a thread starts with a one-line note that tells Codex to call `exchange_info`, which then also returns the rules. A thread from 0.3.2 or earlier also keeps that version's instructions in its history, so it can still answer in the app with the page's JSON format. The page still reads such a JSON answer.

From 0.3.3 the page asks for no output format, so the thread stores Codex's answer as plain text, as the app shows it. The page's status, question and choices come from the `ask_owner` and `report_failure` tool calls (see [Human Input](#human-input)).

In the ChatGPT app the `exchange_info` tool is not available. The rules tell Codex to make no backup copy there and to say so in its answer. The worker's protections do not depend on these rules: approvals, the sandbox and the policy stay the same.

## Backups

From **0.1.63**, the worker no longer archives the whole configuration folder before every message. On a large configuration that archive took longer than Codex needed to start answering, and one archive was kept per message forever.

**Per-file copies (default).** The `exchange_info` tool tells Codex to copy each existing file to `/data/codex_tasks/<task_id>/turns/<turn_id>/backups/<path under /config>` before it changes, moves, or deletes it. Only the files Codex is about to change are copied. The copies are a safety net, not a check: the answer does not list them and gives no verdict on them, because other writers (an automation, another app, a person in the file editor) can change files under `/config` while a turn runs, and the app cannot tell their changes from Codex's.

- When a change fails validation (below), the task details point to the saved copy of each affected file, or say that it has none.
- Secret files are never copied: every `secrets.yaml` and every file under `.storage`, where Home Assistant keeps sign-ins, tokens and integration credentials beside dashboards. The full snapshot leaves them out, Codex is told not to copy them, and the worker deletes such a copy when the message ends, in every permission mode. A message that you stop loses the copies at once; a message that a restart cuts off loses them at the next backup cleanup, when the app starts or within the hour. A permission card for a change to one of these files shows the change while it waits for your answer; after the answer, or when the request expires, the chat's record keeps the file's path but not the changed lines. To undo a change to one of them, use a Home Assistant backup.
- In a sandbox (Default, Auto-review and Read only), `/config/.storage` is read-only and `/config/secrets.yaml` is hidden. A `secrets.yaml` in another folder, such as `esphome/secrets.yaml`, gets no copy (above) but is not hidden: a sandboxed command can read and change it without a request. Codex can change these files only through a permission request. In Default, you see that request as a card and decide. In Auto-review, Codex's reviewer decides, and in Full access nobody is asked: no app-side check can stop a change there, because Codex runs without the sandbox.

Codex needs to be able to write to the backup folder. In `workspace-write` mode the app adds that one exchange's backup folder to the sandbox's writable places, beside `/config` and `/share` and the chat's files folder (see [Files Codex makes for you](#files-codex-makes-for-you)); the rest of `/data` stays read-only to Codex, and the private files the sandbox hides stay hidden. In `read-only` mode Codex is given no backup folder; any explicitly approved writes still need a separate recovery plan. A per-file copy depends on Codex following the instruction; if a file was changed without one, restore it from a Home Assistant backup.

**Full snapshot (optional).** Turn on **Full snapshot before every message** (`full_snapshot`) to archive tracked files, excluding secret files (every `secrets.yaml` and every file under `.storage`), to `snapshot-before.tar.gz` in the exchange's folder before Codex starts. When a change fails validation, the worker can then take a missing per-file copy from that archive when a matching file is available. The exclusions below still apply, so this is not a complete Home Assistant backup. Creating the archive adds a wait before every message that grows with the amount of tracked data.

**Cleanup.** **Keep backups for (days)** (`backup_retention_days`, default 7, range 1 to 365) sets how long backups are kept after an exchange ends. This covers per-file copies, full snapshots, and recovery copies made by earlier versions. Cleanup runs when the app starts, after every exchange, and once an hour; it skips the exchange that is running and leaves messages, logs, and generated images in place. Files you attach to a message follow their own 7-day rule (see Attachments). Archives left by earlier versions are removed the same way once they are older than the setting.

**Old chats.** A chat with no message for **Delete chats after (days without a message)** (`chat_retention_days`, default 14, range 1 to 365) is deleted with all its files, its prompts, logs, backups and attachments. Pinned chats are never deleted. A scheduled run counts as a message: a chat that an approved, enabled schedule was saved from is kept while the schedule is on, and the time of its last run counts as its last message. A chat that is running or waiting is never deleted, and a message waiting in the queue counts as a message. In its last 3 days the chat shows a line with the day it will be deleted; sending a message or pinning the chat clears it. The check runs when the app starts and once an hour. A deleted chat cannot be restored.

**Change detection.** The list of tracked files recorded before a run holds each file's size, modification time, and SHA-256 hash. Hashes can be reused when the size and timestamps are unchanged and the file was already at rest before the previous scan. Recently changed files are read again. The exchange's baseline file is deleted when the exchange ends; detected changes stay recorded in `changes.json`.

**Coverage and exclusions.** Change detection and snapshots skip symbolic links, files larger than **8 MiB**, the worker's task folder, recorder databases, and excluded folders such as `www`, `media`, `downloads`, `deps`, and caches. Selected file types, including logs, common images, audio/video files, fonts, pickle files, and `.old` files, are also excluded. See `EXCLUDED_PARTS`, `EXCLUDED_SUFFIXES`, and `should_include_file` in `server.py` for the exact list. These skipped files do not receive the worker's automatic change validation. Secret files (every `secrets.yaml` and every file under `.storage`, dashboards kept in storage included) remain in change detection but are excluded from backups and snapshots. Keep Home Assistant backups for recovery beyond this coverage.

## Sandbox

- `read-only`: Codex can inspect files. Writes and shell networking require permission for the specific action.
- `workspace-write`: Codex can edit the mounted `/config` workspace and the shared `/share` folder, the same `/share` access the Claude add-on has, and can save files for you in the chat's own files folder under `/data`. Shell networking and writes elsewhere require permission. This remains the default setting.

The app's `codex_sandbox` option takes only these two values. An older setting that names `danger-full-access` stops the app at startup with a log line saying so. A chat can still run without the sandbox in the Full access permission mode, which the owner turns on in Settings (see "Permission modes" below).

**Protected policy.** The sandbox modes the `codex_sandbox` option may name come from `/usr/share/ha_codex/policy.json` inside the app image, outside `/config` and sandbox writable roots. A value it does not list stops startup and each task. The app adds no command check of its own: Codex's approval mode and the sandbox, with its private-file masks, are the protection (0.3.6). No house rules are bundled or copied into Codex's home. Codex uses its defaults and the owner's `AGENTS.md` (in the app's own folder, see [AGENTS.md and HA_TOKEN](#agentsmd-and-ha_token)), whose existing editor and reading path remain available. Owners who want house practices can use the Whole Home Agent starter kit (published separately) and choose their own instructions. Native approval remains specific to the displayed request; read each action before allowing it.

**Who can answer.** Only a signed-in Home Assistant administrator can answer **Allow once** or **Deny**, on the Codex page. This holds in Default and Read only. In Auto-review, Codex's reviewer answers, and in Full access nobody is asked, whoever sent the message (see "Permission modes"). The worker checks the user Home Assistant names for the page against its user list and records who answered on the request. The integration's API token cannot answer permission requests, and the integration's actions refuse callers who are not administrators. An automation or script that runs on its own may call the actions; one that a user who is not an administrator starts carries that user, and its call is refused too.

A command allowed once runs outside the sandbox. The card does not refuse any command or path by name, so read each command before you allow it.

**Prompt counts.** Each conversation keeps a count of the permission requests it raised, by kind (`command`, `command_input`, `file_change`, `other`). The integration's **Permission prompts** sensor shows the latest conversation's total, with the counts by kind as attributes.

The app still provides safeguards: instructions to save per-file backups, optional full snapshots, checks of changed YAML and JSON, Home Assistant configuration checks, and built-in verification of states and dashboards. These help catch mistakes and recover changes; they do not guarantee that every action is safe or reversible. Keep normal Home Assistant backups too.

The worker exposes the Codex version and a structured sandbox preflight in its authenticated `/health` response. Sandboxed tasks fail with an actionable error before launch if namespace isolation is unavailable.

### Permission requests

In `workspace-write` and `read-only`, the worker uses Codex's live approval protocol with the approval policy `on-request`. When Codex requests access for a command or file change, the chat shows **Permission needed**, the reason, the command and working folder or the file paths, rename destinations, and diff, and **Allow once** and **Deny** buttons. Review the action, then choose a button. When Codex asks before it runs the Home Assistant tool (`call` of the `home_assistant_call` server), the card shows the tool name and every argument it will send, such as the action, its target and the full text of a message, in plain words and as JSON. The plain words name each entity, area, device, floor and label of the target. A request that the card cannot show in full, because it is too long or a known credential in it is hidden, has **Allow once** turned off: deny it. **Allow once** on a tool request allows that one call; Codex asks again before the next one. Typing a reply or answering a follow-up question does not grant sandbox access. Open the app chat to answer permission requests, including tasks started by the integration or a notification.

ChatGPT apps are services, such as GitHub or a calendar, that you connect to your ChatGPT account. Codex can use their tools through that account. These tools act in the app's own service, outside this Home Assistant and outside the sandbox. In Default, Codex asks before each such call, and the chat shows the same card: the app, the tool, the question Codex asks and every argument. In Read only, the page declines each app tool request and says so in the chat. In Auto-review, Codex's reviewer answers instead of you; in Full access, Codex does not ask. The app log records each tool request that you allow or deny, and each question that the page declines, by name only, without its arguments.

When a request starts waiting, the worker sends a **Codex needs permission** notification through `notify_service`, or a persistent notification when none is set. It carries the reason Codex gave and the task, not the command or the diff, and has no buttons: the decision is made in the chat, where the complete action is shown. A notification that Home Assistant has not received by the time the request is answered or expires is not sent, so it cannot replace a later notification about the task's question.

**Allow once** answers only that pending request. It does not change the configured sandbox, create a permanent command rule, or grant access to later requests. An approved command can run outside the sandbox with the app's available file and network access; permission is broader than enabling networking alone. An approved file change applies the displayed edits. Review paths outside `/config` carefully, since the worker's configuration backups and checks do not cover them. In `read-only`, the normal per-file backup instructions are disabled, so consider changing modes for editing work.

**Deny** refuses the action and lets Codex continue, for example by finding another way or explaining the limitation. **Cancel** stops the exchange. Requests expire when the exchange ends, times out, is canceled, loses its CLI connection, or the worker restarts. If a file diff changes while its request is waiting, the exchange stops and the old request expires. The task timeout includes time spent waiting for permission. The stall limit (`task_stall_seconds`, 10 minutes by default) does not count that wait: it stops a task only when Codex sends nothing while no request is waiting, as after a network drop. A command that runs quietly counts as sending nothing: a long build or check that prints no output for the whole limit is stopped too, so raise the limit, or set it to 0, if your tasks run such commands. An unanswered request keeps the worker busy and later messages in the queue. Decisions from another tab refresh automatically; an old or repeated button cannot grant access.

The request and decision remain in the exchange's `approvals` history, with sensitive values redacted. An incomplete, oversized, or redacted action can be denied but cannot be allowed, since hidden text could contain executable instructions. Ask for an action whose command, working folder, paths, and diff can be shown in full. API calls should refer to credentials through environment variables instead of embedding their values in a command. This first version supports individual command and file-change requests; broad turn or session permission grants and permanent policy changes are refused. Input that Codex wants to type into a command already running outside the sandbox is shown and can only be denied; Codex can run the whole action as one command instead. API credentials are separate: an approval does not create a Home Assistant token or expose the Supervisor token to Codex. The built-in verification tool continues to work through the worker without shell network approval.

A request is shown only when it belongs to the exchange that is running now: a request that arrives before Codex started the exchange, or one for another exchange, stops the exchange without a card. A request that repeats the id of a request that is still waiting also stops the exchange, also when the exchange has reached its limit of 50 requests. After that limit, each new request is denied with a line in the chat.

Authenticated API clients can answer a live request using `POST /tasks/<task_id>/approvals/<approval_id>` with `{"turn_id":"<current_turn_id>","decision":"allow"}` or `"deny"`. Read the redacted `approvals` records from `GET /tasks/<task_id>` first. A stale or already answered request returns HTTP 409. Task summaries include `permission_required` while an exchange is waiting. Permission decisions are made directly on the live exchange and are never queued as chat messages.

### Changes to the house: the `call` tool

From **0.3.6**, a chat on the page changes the house through one tool, `home_assistant_call` `call`. It sends one
Home Assistant action (a service call), for example `light.turn_off` with a target, `automation.reload` or
`homeassistant.restart`. The app sets the tool's approval mode to `prompt`, so Codex asks you each time, with no
remembered answer: the question has no "Allow for this session" and no "don't ask again". It shows the service,
the target and the data.
A scheduled run has the same tool. Its question waits in the run's chat until an administrator answers it
([Scheduled runs](#scheduled-runs)).

The ChatGPT app has the same tool while remote control is on. There, Codex asks in the app before each call; the
worker does not see that answer. In the app the tool uses its own socket, which answers only while the
remote-control process runs. The checks below hold in the app too.

The tool gives Codex no token. Its bridge passes the request to the worker over a private socket, with the thread id
that Codex adds to the call. The worker sends the call to Home Assistant once, with its own access, and
checks these things first, in every mode:

- On a chat's socket, a request that does not name a chat with a running message sends nothing. On the app's
  socket, a request while remote control is off sends nothing.
- The service must be `domain.service`. `entity_id`, `device_id`, `area_id`, `floor_id` and `label_id` go in
  `target`, the other fields in `data`. A request in any other form sends nothing.

The tool refuses no service by name. Your answer to Codex's question is the only gate, so read the service before
you allow it. A call can reach the Supervisor: a `hassio` service, `update.install` on an update entity (an app,
Home Assistant OS or Core, this app too) or `backup.create`. A call can also run code that you wrote, for example a
`shell_command`, `python_script`, `pyscript` or `rest_command` service. The app keeps no log of its own of the
calls; Home Assistant's logbook and history show what changed.

For a device you name, Codex can give your words in place of a target, for example `light.turn_on` with the name
`kitchen ceiling`. Codex still asks you first, and the question shows the name. The worker then reads the entity
list and sends the call only when exactly one entity of the service's domain fits the name:

- Each word must be a whole word of the entity's name or entity id: "lamp" does not fit "Lamppost". A vowel or
  tone mark (as in Thai or Hindi) is part of its word, so two names that differ only in a mark do not fit.
- An entity whose name is exactly the words given wins over longer names: "office overhead" picks "Office
  Overhead", not "Office Overhead Fan". Two entities with the same exact name are two candidates.
- The service picks the domain: `light.turn_on` with a name never changes a switch. `homeassistant` services
  act on many domains, so they need the entity id.
- Credential-like and password-mode entities never match.

When no entity fits, or more than one, nothing is sent: the answer lists up to 10 candidates and the full count,
and Codex asks you which one. After a call whose target is entity ids, the answer also holds the state right after
the call of up to 5 of them, so Codex does not need a second step to read it back. A light that fades or a cover
that moves can still show its old state there. The answer names the entity that changed. If Home Assistant does
not answer these reads, the answer can come up to 40 seconds late; the call itself is sent once.

A chat holds no key for the app's tools (0.4). Each tool request carries the chat's thread id, which Codex adds
to every tool call, and the worker answers it only while that chat's exchange runs. In Read only, Default and
Auto-review a command cannot connect to the tool sockets at all: the sandbox has no network, and Codex denies the
connection. A command that runs outside the sandbox - one you approve when Codex asks to run it with more access,
one that Codex's reviewer approves in Auto-review, or any command in Full access - can reach the sockets, and can
then do what the tools can do. That approval is the protection, as it is for the command itself.

Remote control holds no key either (0.4). Its call tool and its read tool each have a socket of their own, so
the socket tells the worker that a request comes from the app, and both answer only while the remote-control
process runs. The same sandbox keeps the commands of the app's Codex from those sockets, and the same approval
covers a command that runs outside it. With the option **Enable Home Assistant MCP** on, the relay's Codex holds
the Supervisor token as `HA_MCP_CAPABILITY` for the bundled hass-mcp server; Codex's `shell_environment_policy`
setting keeps every `HA_*_CAPABILITY` name out of the commands it runs. The remote-control launch sets it, and
`/etc/codex/managed_config.toml` in the image sets it for every client. A thread that the ChatGPT app starts cannot
change it, because the managed file outranks a thread's own settings.

These checks do not cover a command that uses the `HA_TOKEN` option (see "AGENTS.md and HA_TOKEN").

### Owner instructions and planted commands

Codex follows its defaults and the owner's `AGENTS.md`. The integration does not bundle a house-practice file prescribing device categories, API-deletion practices or a writing style. Owners can choose such practices through their own workspace instructions or the starter kit. The starter `AGENTS.md` of a new install ([AGENTS.md and HA_TOKEN](#agentsmd-and-ha_token)) is an ordinary owner file, in plain view, that the owner can change or delete. Permission cards and the sandbox operate independently of prose instructions.

The app does not scan `/config` for planted commands. From 0.3.6 there is no command check (hook), no check after each exchange and no config watch: an edit that adds a `shell_command`, `rest_command`, `python_script` or a command-line platform stays as Codex wrote it. In `workspace-write` Codex can write such an edit without a request, and Home Assistant runs it on the next reload or call. Review what a chat changed (its steps show each file Codex edited) before a reload or restart. The Supervisor token stays out of Codex's environment, and the sandbox still hides `secrets.yaml`, Home Assistant's sign-in store and the app's own data (AUDITING.md, layer 1).


## Ways in

Codex can be reached three ways: the chat page, the ChatGPT app paired through remote control, and the
`codex_cli.start_task` action. Each way in gets its own set of Home Assistant tools. The table lists them.

| Request | Chat page | ChatGPT app | codex_cli.start_task |
|---|---|---|---|
| Read an entity's state or find an entity | `home_assistant.verify`, `home_assistant_mcp.get_entity`, `home_assistant_mcp.list_entities`, `home_assistant_mcp.get_entities_by_area`, `home_assistant_mcp.search_entities_tool`, `home_assistant_mcp.domain_summary_tool`, `home_assistant_mcp.system_overview`, `home_assistant_mcp.get_version` | `home_assistant_relay.read`, `home_assistant_mcp.get_entity`, `home_assistant_mcp.list_entities`, `home_assistant_mcp.get_entities_by_area`, `home_assistant_mcp.search_entities_tool`, `home_assistant_mcp.domain_summary_tool`, `home_assistant_mcp.system_overview`, `home_assistant_mcp.get_version` | `home_assistant.verify`, `home_assistant_mcp.get_entity`, `home_assistant_mcp.list_entities`, `home_assistant_mcp.get_entities_by_area`, `home_assistant_mcp.search_entities_tool`, `home_assistant_mcp.domain_summary_tool`, `home_assistant_mcp.system_overview`, `home_assistant_mcp.get_version` |
| Read history or long-term statistics | `home_assistant_data.history`, `home_assistant_data.statistics`, `home_assistant_mcp.get_history`, `home_assistant_mcp.get_history_range`, `home_assistant_mcp.get_statistics`, `home_assistant_mcp.get_statistics_range` | `home_assistant_mcp.get_history`, `home_assistant_mcp.get_history_range`, `home_assistant_mcp.get_statistics`, `home_assistant_mcp.get_statistics_range` | `home_assistant_data.history`, `home_assistant_data.statistics`, `home_assistant_mcp.get_history`, `home_assistant_mcp.get_history_range`, `home_assistant_mcp.get_statistics`, `home_assistant_mcp.get_statistics_range` |
| Read automations, dashboards or the error log | `home_assistant_mcp.list_automations`, `home_assistant_mcp.list_dashboards`, `home_assistant_mcp.get_dashboard_config`, `home_assistant_mcp.list_view_sections`, `home_assistant_mcp.list_dashboard_backups`, `home_assistant_mcp.get_error_log` | `home_assistant_mcp.list_automations`, `home_assistant_mcp.list_dashboards`, `home_assistant_mcp.get_dashboard_config`, `home_assistant_mcp.list_view_sections`, `home_assistant_mcp.list_dashboard_backups`, `home_assistant_mcp.get_error_log` | `home_assistant_mcp.list_automations`, `home_assistant_mcp.list_dashboards`, `home_assistant_mcp.get_dashboard_config`, `home_assistant_mcp.list_view_sections`, `home_assistant_mcp.list_dashboard_backups`, `home_assistant_mcp.get_error_log` |
| Control a device or call a service | `home_assistant_call.call`, `home_assistant_mcp.entity_action`, `home_assistant_mcp.call_service_tool`, `home_assistant_mcp.restart_ha` | `home_assistant_call.call`, `home_assistant_mcp.entity_action`, `home_assistant_mcp.call_service_tool`, `home_assistant_mcp.restart_ha` | `home_assistant_call.call`, `home_assistant_mcp.entity_action`, `home_assistant_mcp.call_service_tool`, `home_assistant_mcp.restart_ha` |
| Change a dashboard | `home_assistant_mcp.set_dashboard_config`, `home_assistant_mcp.add_card`, `home_assistant_mcp.update_card`, `home_assistant_mcp.remove_card`, `home_assistant_mcp.move_card`, `home_assistant_mcp.add_view`, `home_assistant_mcp.remove_view`, `home_assistant_mcp.update_view`, `home_assistant_mcp.restore_dashboard` | `home_assistant_mcp.set_dashboard_config`, `home_assistant_mcp.add_card`, `home_assistant_mcp.update_card`, `home_assistant_mcp.remove_card`, `home_assistant_mcp.move_card`, `home_assistant_mcp.add_view`, `home_assistant_mcp.remove_view`, `home_assistant_mcp.update_view`, `home_assistant_mcp.restore_dashboard` | `home_assistant_mcp.set_dashboard_config`, `home_assistant_mcp.add_card`, `home_assistant_mcp.update_card`, `home_assistant_mcp.remove_card`, `home_assistant_mcp.move_card`, `home_assistant_mcp.add_view`, `home_assistant_mcp.remove_view`, `home_assistant_mcp.update_view`, `home_assistant_mcp.restore_dashboard` |
| Send a notification | `home_assistant_call.call` | `home_assistant_call.call` | `home_assistant_call.call` |
| Ask the owner a question or report the turn's state | `home_assistant_data.ask_owner`, `home_assistant_data.report_failure`, `home_assistant_data.exchange_info` | not available | `home_assistant_data.ask_owner`, `home_assistant_data.report_failure`, `home_assistant_data.exchange_info` |
| Work with a GitHub repository | `codex_github.github_git` | not available | `codex_github.github_git` |
| Propose a schedule | `codex_scheduling.propose_schedule`, `codex_scheduling.list_schedules` | not available | `codex_scheduling.propose_schedule`, `codex_scheduling.list_schedules` |

- `codex_cli.start_task` starts the same kind of task as the chat page, so it gets the same tools.
- Each `call` and each `home_assistant_mcp` tool that changes something needs an answer, on the chat page or in
  the ChatGPT app ([Changes to the house: the `call` tool](#changes-to-the-house-the-call-tool)). In Default and
  Read only, Codex asks you; in Auto-review, Codex's reviewer can allow it without asking you; in Full access, it
  runs without a question ([Permission modes](#permission-modes)). The
  `home_assistant_mcp` tools are there only while [Enable Home Assistant MCP](#home-assistant-mcp) is on.
- File edits and shell commands use Codex's own tools on every way in, and the [sandbox](#sandbox) applies on every
  way in. On the chat page and for `codex_cli.start_task`, a request for more access shows on the chat page as a
  [permission request](#permission-requests). The ChatGPT app shows Codex's own requests in the app instead, as
  [Remote control from the ChatGPT app](#remote-control-from-the-chatgpt-app) describes.
- A scheduled run also starts a task. It uses the same launcher as the chat page and gets the page's tools, the
  `call` tool included; Codex asks before each `call`, and the question waits in the run's chat. A Read only
  schedule narrows the sandbox, not the tools ([Scheduled runs](#scheduled-runs)).
- "Not available" means the way in has no tool for that request.

`tests/test_surface_matrix.py` starts each way in's tool servers and compares their tools with this table. A change
that adds, removes or moves a tool updates this table and the test in the same pull request.

## Remote control from the ChatGPT app

OpenAI's remote control (`codex remote-control`) lets the ChatGPT app on a phone or tablet work on this Home Assistant through OpenAI's relay. The app runs it for you; there is no terminal step. The ChatGPT desktop app does not list paired machines, so the phone app and the chat page are the two ways in.

**Startup on Home Assistant OS.** An immutable launcher enters a dedicated relay AppArmor label.
Its separate Bubblewrap binary sets a private UTS host name and maps container root to UID/GID
1000 inside the outer user namespace, then drops all capabilities before Codex starts. The root worker profile gains no
capabilities. A namespace-only probe selects this route; if setup fails, the page and log show
**Machine-name namespace unavailable**. The fallback omits the private hostname but still creates
the non-zero user mapping and enters the relay label. It clears and verifies all capability sets
and NoNewPrivs before starting Codex, failing closed if the safe mapping cannot be made. Launcher errors
identify a fixed label, stack, capability or exec stage without printing arguments or raw exceptions.

The inner UID represents the same kernel file owner as container root, so existing `/config`
and `/data` ownership and access are preserved; files are not chowned. This avoids Linux's
CAP_SETFCAP requirement for a new mapping of parent UID 0 after privileges were dropped.
Codex's nested sandbox instead maps parent UID 1000 to itself. Neither route retains a capability
or relaxes the tool sandbox to make that operation succeed.

The worker creates `/data/remote_control_control/` at startup, including with remote control off.
The sandbox wrapper masks that fixed parent, so an earlier chat sandbox cannot see a later control
socket through its ordinary path. Relay tool commands retain the relay label and add a separate
restrictive tool component while `NoNewPrivs=1`. An immutable guard verifies the actual stack
before starting native Bubblewrap; the chat tool label is unchanged.
This prevents the base abstraction's same-label ptrace-read rule from granting a sandbox access to
the relay's `/proc/<pid>/root`, `cwd` or `environ` in the no-proc fallback. Loaded-label and proc-view
isolation still require the maintainer's HAOS acceptance check described in
[SANDBOX_TESTING.md](../codex-cli-worker/SANDBOX_TESTING.md#remote-control-profile-regression).

**Relay execution.** The reproduced unified-exec failure was a `nodev` root bind, not
a missing executable rule. Under the previous outer `--bind / /`, opening `/dev/null` and
`/dev/ptmx` failed with `EACCES`; pipe commands and PTY commands both failed before exec. The
outer relay now applies `--dev /dev` after the root bind and preserves shared memory with
`--bind-try /dev/shm /dev/shm`, exposing standard devices rather than the whole container device
tree. Removing those device setup flags reproduces the native regression. The former `/opt` rules
were redundant with `file,` and have been removed. Tool sandbox labels retain their narrower
`network netlink raw,` rule; the relay alone keeps its parent transport access. Native approvals
and the managed requirements remain in effect. The relay starts in `/config`.

**First run: no hand-made token for reads.** Pair remote control, then ask for entity ids or current state. The `home_assistant_relay.read` tool uses the worker's existing Supervisor access for bounded, observational entity listings and state readback. The relay receives no key for it; the Supervisor credential stays in the worker, and a command in the sandbox cannot reach its socket. The page shows the discovered Core address and explains the read tool. A new relay launch, relay exit or stopping revokes a pending read; late results are discarded. This tool is separate from a page task's verification/history tools and cannot call services, reload, approve an action or read credential stores. From **0.3.6**, a change goes through the separate `home_assistant_call` `call` tool, which Codex asks you for in the app before each call (see "Changes to the house: the `call` tool").

Use `{"operation":"states","domain":"light","limit":10}` to list a page, then `{"operation":"state","entity_id":"light.sample","attributes":["friendly_name","brightness"]}` for selected observational attributes. Lists are sorted and return `next_offset`; limits are 1–50 and the default is 10. State strings longer than 512 characters are explicitly marked truncated. Credential-like entity ids and undocumented attributes are refused, and recognized credential values are redacted. A state read does not prove automation behavior. Responses are capped at 64 KiB and Core responses at 16 MiB; request a smaller page or fewer attributes when a read is too large. One relay read runs at a time. Downloads above 16 MiB return an explicit error asking for a single entity. Password-mode input_text entities and credential-like id substrings are excluded from both listings and state readback.

For an unsupported read that needs shell networking, relay instructions tell Codex to request native approval for that specific command instead of silently stopping on a sandbox network denial. Direct commands still require an appropriate Core credential: when the owner optionally configures `HA_TOKEN`, relay commands receive it together with the resolved `HA_URL`. Native approval does not create credentials. The read tool's automatic authentication adds no service-write authority and does not bypass any #22/#27 refusal. Never obtain credentials from stores or process environments.

**Updates keep the enrollment.** Startup sets `CODEX_HOME=/data/codex-home` and pins `sqlite_home` to
that same persistent directory. It refreshes configuration/instructions without replacing Codex's state
database or the saved enabled choice. The initialize request retains the client name `ha_codex` across image versions and nonempty display
name changes. Display/UTS names are not enrollment keys; do not request a new code merely because
the image changed. The pinned
[enrollment implementation](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-transport/src/transport/remote_control/enroll.rs#L293)
loads enrollment from the state database, keyed by relay target, account and stable app-server client
name. This is separate from a transient control socket and the replaceable image. An update preserves
data; uninstalling, clearing data, changing account or revoking enrollment is a different operation.
Actual reconnect without a new code remains a maintainer lab acceptance check.

**First pairing (Codex 0.166).** Turn remote control on, wait for the connected state, then choose
**New pairing code** and type the displayed code into the ChatGPT app. The button calls
`remoteControl/pairing/start` with `manualCode: true` on the running process's own Unix WebSocket.
It uses `manualPairingCode`, not the opaque QR pairing token. A missing, unsupported or failed response
shows an error and clears an earlier code. Turning off or restarting clears the page's code. Codes
are kept only in administrator page state; recent logs, non-administrator Ingress reads,
the integration and `/status` omit them. Start, stop and new-code requests all require
an active Home Assistant administrator verified through the same check as task approvals.

This follows the pinned upstream sources at `rust-v0.166.0`:

- [cli/src/remote_control_cmd.rs:187-204](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/cli/src/remote_control_cmd.rs#L187):
  foreground mode creates a private socket; the separate CLI `pair` command selects the daemon socket.
- [cli/src/main.rs:591-606 and 1298-1314](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/cli/src/main.rs#L591):
  `app-server --listen unix://PATH --remote-control` selects ephemeral remote control on this process.
- [app-server-daemon/src/client.rs:63-140](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-daemon/src/client.rs#L63)
  and [remote_control_client.rs:60-140](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-daemon/src/remote_control_client.rs#L60):
  Unix WebSocket, experimental initialization, ephemeral enable, status and manual-code request.
- [app-server-protocol/src/protocol/v2/remote_control.rs:70-83](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-protocol/src/protocol/v2/remote_control.rs#L70):
  manual-code request and response fields. No second daemon or terminal pairing step is needed.

**Managed policy and read-only fallback (Codex 0.166).** The requirements list the values of the four
permission modes: the `read-only` and `workspace-write` sandboxes, `on-request` approvals, the person
(`user`) or Codex's reviewer (`auto_review`), and no hooks, so a hook planted in `/config` never runs. While the owner's Full access switch is
on, the app writes them again with the `danger-full-access` sandbox and the `never` policy added. The app does
not check a thread's settings itself. A setting the requirements do not list can be downgraded to read-only by
Codex with a configuration warning. The pinned
[0.166 configuration loader](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/config/mod.rs#L2358)
uses this permitted fallback; the requirements do not turn it into an RPC error. Settings and the launch log
explain the possible downgrade. When Codex emits the warning on the supervised output, the page and log say
that the request was downgraded. Not every relay thread warning is exposed there: absence of that message
is not evidence that the requested permissions were granted.

- **Off until you turn it on.** In the app chat, open **Settings** > **Accounts and connections** > **ChatGPT app pairing** > **Manage remote connection**, then choose **Turn on**. You can also open the connection view from the **Remote connection** row at the foot of the side bar. The choice is saved and holds across app restarts.
- **One supervised process.** The app runs `codex app-server --listen unix://<private socket> --remote-control` in the foreground and starts it again, with a growing delay, if it exits. **Turn off** stops it.
- **Machine name.** The ChatGPT app lists this machine as **Home Assistant** unless you change the **Remote control machine name** option on the app's Configuration tab, or in **Settings** > **Remote connection** while remote control is off (the field there saves the same option; the page showed the name only in 0.3.5 and 0.3.6). The name from the Configuration tab takes effect when the app restarts; the name saved on the page takes effect the next time you turn remote control on. Pairing is kept with the Codex sign-in, so the app should show the new name the next time remote control connects; if it still shows the old name, choose **New pairing code**. Codex has no name setting, so the app starts it with that name as its host name inside a private namespace. If the private name namespace is unavailable, the page explains that Codex is using its existing host name.
- **Pairing.** The manual code is shown as large text. A QR for the app link appears only when an app-link code is available; otherwise type the manual code or request a new code. Pairing is kept in the app's private storage with the Codex sign-in, so a restart does not ask for it again. **New pairing code** asks Codex for a fresh one when the ChatGPT app has lost this machine.
- **Automatic Core address and first-run reads.** Relay commands receive the discovered or explicitly overridden `HA_URL`; an owner-configured `HA_TOKEN` remains optional. The separate observational read tool needs no hand-made token. While **Enable Home Assistant MCP** is on (the default), the relay process holds the app's Supervisor token for its whole run, to start the Home Assistant MCP server; with the option off, the relay gets no Supervisor token. The commands Codex runs in the sandbox do not see that token, but a command that runs outside the sandbox can read it ([Home Assistant MCP](#home-assistant-mcp)). Relay processes never receive a chat turn's private MCP capabilities; from **0.3.6** they get the `call` tool with a key of their own, kept out of the commands Codex runs. Never print credentials; approval does not create them.
- **No command check of its own (0.3.6).** Commands from the ChatGPT app do not pass through this app's Allow and Deny buttons. They are held by Codex's own approval flow on the phone, the sandbox mode of the thread, and Codex's managed requirements (`/etc/codex/requirements.toml`, read-only): the values of the four permission modes and no hooks (the full-access sandbox and the `never` policy only while the Full access switch is on), so a hook planted in `/config` or Codex's home never runs. The managed config (`/etc/codex/managed_config.toml`, read-only) holds one setting: the commands Codex runs never see the tools' keys (`HA_*_CAPABILITY`), whatever a client sends with a thread. The app ships no hook, no command-prefix rules, no rule-load self-test and no config watch, so **Turn on** is always available.
  - The phone uses Codex's own approval card, not this page's buttons: review what will change, where and the effect, then allow or deny that request. An allowed command runs outside the sandbox.
  - A valid owner `allow` rule in a Codex `.rules` file can let a matching command run outside the sandbox without a request. The app does not read or block owner rules.
- Remote control is experimental upstream. If it breaks, the chat page is not affected.

## Codex Sign-In

Choose **Settings > Sign in** in the app web UI or call the Home Assistant action `codex_cli.start_login` to start `codex login --device-auth`.

Before starting sign-in, enable **Enable device code authorization for Codex** in ChatGPT: open the ChatGPT website, click your profile, open **Settings** -> **Security**, and turn on the toggle near the bottom of the page. This setting is not in the Codex website.

For a managed workspace, an administrator may need to enable device-code login in the workspace permissions. See [Codex authentication](https://learn.chatgpt.com/docs/auth#preferred-device-code-authentication-beta).

The app never starts a sign-in by itself. When it starts and Codex is not signed in, it posts a Home Assistant notification that says to choose **Sign in** when you are ready. A one-time code works for 15 minutes only, so you get it at the moment you use it.

After you choose **Sign in**, the app posts a Home Assistant persistent notification containing the sign-in link and the one-time device code. Open the link and type the code from the same notification into that page. The QR code of the sign-in page is on the app's own page only (below): the notification has no image, because an image there would have to be a file under `/config/www`, which Home Assistant serves to anyone who can reach it, without a sign-in. The app refreshes the notification when the code appears and detects completion automatically. You do not need to run `docker exec`.

While the app is not signed in, a banner above the chat offers **Sign in**. During a sign-in, the banner and **Settings** show the link first, as a button, then the one-time code in large letters, and a QR code of the sign-in page served by the app itself. While a sign-in waits, **Settings** hides its other text and buttons, and shows only this card and **Cancel**. On a phone-sized window the QR code starts folded under **Show QR code**, because a phone cannot scan its own screen; a tall banner scrolls inside itself. The code stays on screen, unchanged, until the sign-in completes or you press **Cancel**; pressing **Sign in** again keeps the running sign-in and its code. Under the code, a countdown shows how long it still works (Codex 0.166.0 gives codes 15 minutes); each status reply from the app sets it again, so it stays right after the phone sleeps. When it runs out, the page says so and offers **Get a new code**. If a sign-in stops without signing in, **Settings** says why when it can, and **Sign in** becomes **Get a new code**. The GitHub code in **Settings > Accounts and connections > GitHub** has the same countdown and button.

Every section of **Settings** starts collapsed, and one top-level section is open at a time: opening one closes the others. The parts inside a section, such as **GitHub**, keep their own state. The window keeps one size; an open section scrolls inside it. Its header shows a one-line status, such as **Signed in**, **Connected as owner, 1 repository** or **Keeps 25**. A section that needs attention shows its status in red: a sign-in that waits or failed, a schedule that waits for your approval, or schedules paused because the Home Assistant time zone is unknown. Tap a header to open the section. When a saved sign-in expires or is revoked, the banner returns and the integration raises a Home Assistant repair; the integration's **Signed in** binary sensor turns off. **Accounts and connections** holds the three sign-ins and pairings: **ChatGPT account**, **GitHub** and **ChatGPT app pairing**, each under its own collapsed header. Its header joins their states, for example "ChatGPT signed in · GitHub connected · app off"; when one of them needs you, the header names only that one. The app pairing part shows the remote connection state, **Get a pairing code** while remote control is on, **Manage remote connection** and **Turn off**. Turn off stops the connection; it does not remove the pairing in the ChatGPT app. A pairing code shows no countdown, because the worker does not know how long Codex keeps it.

The page reaches the app through a Home Assistant ingress session, which the Home Assistant tab renews. If a request finds that session expired (for example after a phone slept), the page asks Home Assistant for a new session once and sends the request again. If that fails, a banner says the session expired and offers **Reload**; the page never shows the bare HTTP error.

When opened through Home Assistant Ingress, the app web UI can start the login flow without entering the worker API token because Home Assistant already authenticated the session. Direct HTTP/API calls still require the worker API token.

Use **Settings > Sign out** in the app web UI or the Home Assistant action `codex_cli.logout` to run `codex logout` and remove saved Codex CLI credentials from the worker. Logout is blocked while a Codex task is actively running.

The app uses the built-in Supervisor token for Home Assistant notifications and dashboard saves through the Home Assistant Core API proxy. No Home Assistant long-lived access token is required.

Sign-in uses your ChatGPT account and its Codex allowance. Availability and usage limits depend on your plan and workspace permissions. The worker does not provide an OpenAI API-key option for Codex tasks.

## GitHub sign-in

Release 0.3 lets the assistant fetch, pull, push, clone and link the GitHub repositories you choose. You do every step with a button in the app page: **Settings > Accounts and connections > GitHub**, just below **ChatGPT account**. No step needs a shell, and nothing about GitHub is typed into chat.

The app keeps the sign-in in its own private folder, `/data/github`. That folder is hidden in every Codex sandbox, so a task cannot read it. Codex never holds the token. It calls one tool, `github_git`, and the app runs git for it. Every push goes through the kit's push guard first.

### Connect

1. Install the **Codex for Home Assistant** GitHub App on the repositories of your choice. Use GitHub's own page for this. You can change the set there later.
2. In **Settings > Accounts and connections > GitHub**, press **Connect GitHub**. The card shows a code in large letters and a link to `github.com/login/device`.
3. Open the link, type the code and approve the App. The card checks every 5 seconds and shows your account, the repositories it reaches and their access as soon as GitHub confirms. Press **Cancel** to stop a sign-in that is waiting. The code works for 15 minutes; after that, press **Connect GitHub** again for a new code.

The App asks for Contents read and write only (GitHub adds Metadata read). The sign-in reaches only what you can reach and what the App was installed on. An App sign-in does not expire. It lasts until you revoke it on GitHub or uninstall the App.

The code appears only on the page. The app does not write it to a log, and the page does not keep it in browser storage.

### Use a token instead

If you cannot install the App (for example, an organization forbids third-party Apps), open **Use a token instead**. Make a fine-grained token on github.com: **Settings > Developer settings > Fine-grained tokens**. Choose **Only select repositories** with one repository, **Contents: Read and write**, and an expiry of 90 days or less. Paste the token and the `owner/repository` name, then press **Use this token**. The app accepts a token only when it reaches that one repository and nothing more. The page clears the field at once and never shows the token again.

### The card

- **Read test** reads each connected repository once and shows the result.
- **Check now** runs the access check at once (see below).
- **Accept this access** appears when the repositories changed on GitHub but the access is still exactly Contents read and write. It makes the current set the one the check expects. If the access itself changed, change it back on GitHub instead.
- **Disconnect** forgets the sign-in on this box. It cannot revoke it on GitHub, because that needs a secret the app does not carry. So the card then shows the link to revoke it: `https://github.com/settings/apps/authorizations` for the App, `https://github.com/settings/tokens` for a token. Disconnect waits while a task runs.
- **Repositories on this box**: **Clone** copies a connected repository into a new folder directly under `/share`; the folder name starts as the repository's name, so a clone goes to `/share/<repository>`. **Link** points an existing git folder under `/share` or `/config` at a connected repository (it sets `origin`). Release 0.3 maps Home Assistant's `/share` folder read-write into the app for this. Neither writes a credential into the repository, so git on this box reaches GitHub with the sign-in only through the app. A list of the git folders on this box, with menus, comes in 0.3.1.

You can also ask the assistant in chat to clone or link a connected repository. It uses the same `github_git` tool.

### The `github_git` tool

Codex can call `repos`, `fetch`, `pull`, `push`, `clone` and `link`, for connected repositories only and only during a chat or scheduled run. Every operation is available in every sandbox mode (0.3.6). A pull fetches only; the assistant then fast-forwards the work tree in its own shell (`git merge --ff-only`), so the app never checks files out into a folder a task can change. A push never forces, deletes or pushes tags.

Limits of a push:

- **Workflow files.** GitHub refuses a push that changes `.github/workflows/*` with Contents-only access. Change workflow files on GitHub or from another computer.
- **Allow marks.** When the push guard refuses a push, a line can be marked `guard: allow <reason>` to pass it. That mark needs the owner. The app refuses a push that adds such a mark, so a task cannot undo a refusal by itself. Add the mark yourself, outside the assistant. A page button for it is planned for 0.3.1.
- **Names check.** The guard's household names check is off in 0.3.

### Monitoring

The app checks the sign-in at start, every hour, and when you press **Check now**. The states are: OK, expires within 7 days, expired, revoked, access changed (repositories missing or added, or access other than Contents read and write), and cannot be checked (GitHub unreachable three times in a row).

When the state changes, and only then:

- The app fires the Home Assistant event `codex_cli_github_access` with the state, the previous state, the reason, the account login, the missing and added repositories, the permission problem and the expiry. It never sends a token or a code. The integration fires the same event, with `source: integration`, when it sees the change, so an automation still works if the app's own call failed. An automation can route it to your notify service.
- The app sends one notification through its `notify_service` option, or a persistent notification (`codex_cli_github`) without one.
- The integration raises one repair (`github_access_lost`). It is an error for expired, revoked and cannot be checked, and a warning for expires soon and access changed. Its text fits the state and the kind of sign-in: for expires soon it says that GitHub still works and gives the date, and for a token it names **Use a token instead**, because **Connect again** starts an App sign-in. It clears when the check is OK or you disconnect. If you never connected, there is no repair.
- A banner above the chat offers **Open GitHub settings**.

### When GitHub stops working

Open **Settings > Accounts and connections > GitHub** in the app page. The card shows the state and, when available, the last check's reason.

- **Not connected to GitHub:** No sign-in is saved, or the app was restored from a backup. Press **Connect GitHub** to connect the App. If you use a fine-grained token, open **Use a token instead**.
- **Connecting:** The App is waiting for you to finish on GitHub. Follow the link on the card. If the code expires, press **Get a new code**. To start over while it is waiting, press **Cancel**, then **Connect GitHub**.
- **Connected to GitHub:** The access check is OK. If a repository operation still fails, press **Read test** to check the connected repositories.
- **GitHub still works, but this sign-in (or token) expires within 7 days:** The assistant can still use GitHub. Before the date, press **Connect again** for the App, or create a new fine-grained token and open **Use a token instead** for a token.
- **The GitHub sign-in expired** or **This GitHub token expired:** The saved sign-in is past its expiry. For the App, press **Connect again**. For a token, create a new one on GitHub, then open **Use a token instead**.
- **GitHub no longer accepts this sign-in (or token):** GitHub refused the saved sign-in (revoked). For the App, press **Connect again**; if GitHub still refuses it, restore the App's repository access on GitHub. For a token, create a new one, then open **Use a token instead**.
- **The new GitHub sign-in failed:** A **Connect again** did not finish. The card gives GitHub's reason, and the previous sign-in stays as it was.
- **The GitHub sign-in reaches different repositories or access than you connected:** GitHub reports a changed repository list or permissions. Check the list and access shown on the card. If the repository change is intended and **Accept this access** is available, press it. If you did not intend the repository change, restore the repository selection for the App on GitHub. Then press **Check now**. If permissions changed, restore Contents read and write on GitHub, then press **Check now**.
- **The GitHub sign-in cannot be checked:** GitHub could not be reached repeatedly, or the check found another error. Check the last-check reason and your connection, then press **Check now**.

### Backups

`/data/github` is not in the app backup. After a restore, the card shows **Not connected**; press **Connect GitHub** again.

## AGENTS.md and HA_TOKEN

The app web UI includes an editor for the app's `AGENTS.md` (in its own `addon_config` folder, not in `/config`) under **Settings** > **Workspace instructions** > **Workspace rules**. This file remains the source of truth for shared Codex project instructions. When the file does not exist, the task prompt does not mention it, and the page shows a setup hint above the message box until you write the file or choose **Dismiss** (the choice is kept in that browser).

**Starter instructions.** On its first start, the app writes a short starter `AGENTS.md` in the app's own folder when the home has
none. The text is `starter_agents.md` in the app's source. It tells Codex what `/config` is and gives seven general
rules. Four of them matter most: change the home only on the owner's word, keep secrets secret, keep the old version
of a changed file, and check the configuration before a restart. A short section after the rules lists three safeguard
defaults (secrets, protected places, backups) and says that they are defaults the owner can change, not locks in
the app. The app never replaces a file that is already there,
a link included. It writes the starter once: when you delete the file, it stays deleted while the app keeps
its data, and an update of an app that has started before writes nothing. The rules are instructions to Codex and not a safeguard; the sandbox, the
command check and the approval cards work the same with or without them. To get the starter on an older install,
copy the text from `starter_agents.md` into the editor.

**Where the file lives (0.4).** The file is `AGENTS.md` in the app's own `addon_config` folder: `/addon_configs/<app folder>/AGENTS.md` on the host and `/addon_config/AGENTS.md` inside the app. It is not in the top level of `/config`. Codex loads it as its global instructions through a link the app keeps at `$CODEX_HOME/AGENTS.md` (`/data/codex-home/AGENTS.md`), where Codex looks for global instructions ([Codex: AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md)). On the first start of this version, an `AGENTS.md` that an older version left in `/config` is **moved** into the app's folder, after a backup copy at `/data/agents_before_move.md`. It is moved and not copied because Codex works in `/config` and would read both. If both places already hold a file, the app moves nothing, logs a line, and leaves the one in `/config` for you to merge or delete. The folder is part of the app's backup like its other data.

The editor under **Settings** writes the file without a question. The folder is not one of the places Codex may write on its own, so when you ask a chat to change the file, Codex asks for your approval first.

From **0.3.4**, Codex no longer looks for the file in every message. Codex loads the file when a chat
starts, and the page's rules tell it never to look for the file with a command. When Codex calls `exchange_info`
in a message, the worker tells it whether the file exists and when it last changed, and Codex reads it again only
after a change. A change you make to the file still reaches a chat that is already open, on its next message that
runs a command, changes a file or calls a tool; a message that Codex answers directly uses the copy it has.

The optional `HA_TOKEN` app option is passed to Codex subprocesses as the `HA_TOKEN` environment variable. Configure it only if you want Codex tasks to call Home Assistant APIs directly. A Home Assistant long-lived access token inherits its user's permissions; use a dedicated user with permissions appropriate for the intended work.

From **0.3.6**, the instructions tell Codex to change the house with the `call` tool, reloads and restarts
included, and to use `HA_TOKEN` only for an API call that is not a service. A command that uses the token is still
a command: in the sandboxed modes it needs network access, so Codex asks first. The `call` tool's checks do not
cover such a command; read it before you allow it.

From **0.1.68**, a task that has `HA_TOKEN` also gets `HA_URL`, the address those calls go to, and the worker's instructions tell Codex to use the two together. The app's own calls to Home Assistant keep using the Supervisor proxy with the app's own token and do not use this option.

From **0.1.81.2**, the worker discovers Core's port and SSL flag from Supervisor `/core/info` at startup and each task or relay launch. `ha_url` is now an empty-by-default **Core API URL override**. Leave it empty for discovery; an explicit HTTP(S) override wins. Saved values equal to the former direct default or former Supervisor proxy defaults are treated as unset. Overrides cannot contain credentials, a query or a fragment. Discovery requests use the worker's Supervisor credential only at the fixed internal endpoint, without proxy inheritance or redirects, with bounded timeouts and response size.

The manifest grants `hassio_api: true` with the default Supervisor role and no `hassio_role` override. The default role admits read-only `/info` endpoints such as `/core/info`; the grant exists only to discover Core's port and SSL setting. The Supervisor credential stays in the worker. Each relay and task launch retries a failed metadata read once, then probes the two fixed internal Core API ports in order without any credential. Only a verified API response supplies a fallback address; the log records which source and fallback port were used. Failed probes leave discovery unavailable.

With SSL, set the Home Assistant API URL override to the configured certificate hostname and port. Never disable TLS verification or use insecure client flags to bypass a hostname or certificate error. Discovery does not turn verification off.

`HA_URL` is passed to Codex even without `HA_TOKEN`. If discovery is unavailable or malformed and there is no valid override, the value stays unavailable and is omitted from the environment; neither the worker nor the instructions invent a port. Settings reports that state. A later launch refreshes discovery. The app's own authenticated API calls continue through its Supervisor Core proxy, which does not accept an owner Core token. None of these changes grants shell networking: an unsupported direct API read needs native approval for that command and a separately configured credential.

These calls come from Codex's shell. In `workspace-write` and `read-only`, Codex must request permission for the specific command that needs networking. What Codex can do with the token is whatever the token's user may do, including reloading configuration and restarting Home Assistant.

## SSH connections

The image includes the OpenSSH client so Codex can use an existing SSH server, such as a Home Assistant SSH app, a NAS, or another Linux machine. There are no SSH app options or connection-management screens. Set up each connection through the existing chat and standard SSH files. No `HA_TOKEN` is needed for SSH authentication, and SSH does not mount remote folders inside the worker.

### Set up a connection through chat

Ask Codex, for example:

> Set up a connection named `ha-terminal` to my SSH server at `ssh-host.example`, port `22`, username `hassio`. Generate a dedicated Ed25519 connection key under `/data/.ssh`. Keep the private key out of chat and logs, show me only its public key, and preserve any existing connections. Use `/data/.ssh/config` and `/data/.ssh/known_hosts` explicitly. Wait for me to verify the server fingerprint before trusting it. Save the connection name and invocation in the app's `AGENTS.md` (`/addon_config/AGENTS.md`), without credentials.

Replace the example host, port, and username with those of the remote SSH server. For a Home Assistant SSH app, enable its SSH network port and use the Home Assistant host's address and that port. The web terminal's Ingress address is not an SSH endpoint.

Ask Codex to create the directory with mode `700`, private keys and configuration with mode `600`, and a connection-specific key without overwriting an existing key. Check the permissions after setup; the app does not enforce these file modes. Setup writes outside `/config` and network checks require approval in sandboxed modes. Public-key authentication is intended for these non-interactive tasks; this setup does not save passwords or provide an interactive passphrase agent. A dedicated unattended key can be generated without a passphrase. Anyone able to read that private key can use the remote permissions granted to it.

Add the generated **public** key to the remote server's authorized keys. In Advanced SSH & Web Terminal, use its `ssh.authorized_keys` option. Keep the remote server's existing credentials and other authorized keys intact. Do not copy the server's Git key into the worker.

Verify the server's host-key fingerprint against a trusted source, such as its local console or administrator, before adding the matching public host key to `/data/.ssh/known_hosts`. `ssh-keyscan` can collect a host key but does not prove the server's identity. For a non-default port, the known-hosts entry uses `[host]:port`. Keep strict host-key checking enabled; investigate a changed key rather than disabling the check.

An example `/data/.ssh/config` connection is:

```sshconfig
Host ha-terminal
    HostName ssh-host.example
    Port 22
    User hassio
    IdentityFile /data/.ssh/ha-terminal_ed25519
    IdentitiesOnly yes
    IdentityAgent none
    UserKnownHostsFile /data/.ssh/known_hosts
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 10
    ConnectionAttempts 1
    ControlMaster no
    ControlPath none
    ControlPersist no
    ClearAllForwardings yes
    ForwardAgent no
    RequestTTY no
```

Use `ssh -F /data/.ssh/config -n ha-terminal '<remote-command>'`, replacing the quoted command with the intended operation. The explicit `-F` is important: OpenSSH can use the account's home directory for its default configuration even though Codex receives `HOME=/data`. Absolute identity and known-hosts paths also avoid that ambiguity. Add another `Host` entry and dedicated key for each additional server.

### Permissions and approvals

The remote SSH server controls what the account and key can do. A normal login may expose everything that account can read or execute, including credentials. To permit only fixed operations, configure a dedicated public key on the **server** with OpenSSH's `restrict` and `command="..."` authorized-key options. The forced command can dispatch a small set of exact operation names to existing scripts and reject everything else. Do not evaluate the requested string as shell code or accept arbitrary executable paths. Keep that dispatcher, the scripts it runs, and their executable configuration outside folders Codex can modify.

Advanced SSH & Web Terminal 24.2.0 was tested with a restricted key for both `root` and the default `hassio` username: fixed dummy operations worked, while shell access, command injection, credential-file reads, SCP/SFTP, terminal allocation, and local/remote port forwarding were denied. Root-protected scripts also worked with a fixed `sudo -n` command for `hassio`. These checks used dummy files, not a real backup repository. Other versions and custom dispatchers need their own checks. See [OpenSSH key restrictions](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT) and the [SSH app documentation](https://github.com/hassio-addons/app-ssh/blob/main/ssh/DOCS.md).

In `workspace-write` and `read-only`, shell networking is disabled until a specific command receives **Allow once** through the existing chat approval flow. Run each remote operation as a separate, short-lived SSH command. The configuration above disables connection sharing and forwarding so later operations do not reuse a connection opened by an earlier approved command. Review the full command: approval permits that command outside the sandbox with the app's available access. Preview and audit commands need network approval too. **Deny** must leave the remote operation unexecuted. See [permission requests](#permission-requests).

This feature does not add a per-operation policy engine or a preview transaction: the remote scripts remain responsible for ensuring a later operation uses exactly the contents the user reviewed. A separate unrestricted SSH login or administrator API token retains its own broader access.

### Storage, backups, and removal

`/data/.ssh` is private to this worker app and persists across restarts and updates, but Codex tasks can read it. Never print private keys or paste them into chat, task prompts, logs, or `/config`. The app excludes the entire `.ssh` directory from Home Assistant app backups, including keys, connection configuration, and trusted host keys. The GitHub sign-in folder, `/data/github`, is excluded too (see [GitHub sign-in](#github-sign-in)). After restoring a backup, set up connections again and revoke replaced public keys on the remote servers. These exclusions do not remove credentials already copied elsewhere or included in older backups.

To remove a connection, revoke its dedicated public key on the remote server, then remove its `Host` entry, local key files, and unused known-hosts entry through chat. Connections use only an SSH client; this feature does not start an SSH server or open an inbound worker port.

## Usage Status

The worker reads usage from Codex's own account read: it starts `codex app-server`, asks `account/rateLimits/read`, and places each window by its length (up to a day is the 5-hour window, a week or more the weekly one), showing the percentage left and the reset time. Some accounts report both windows, while others currently report only a weekly window. If that read fails or names no window, the worker falls back to starting a pseudo-terminal session and reading Codex's `/status` screen; the figures are then marked estimated: the usage sensors' `source` attribute is `status_screen` and their `note` attribute says so (`source` is `rate_limits` otherwise).

The worker disables Codex update checks at startup. Codex CLI is installed and verified as part of the app image, so it must be updated by installing a newer app image rather than from an interactive Codex update prompt.

A missing window has an `unknown` sensor state and a `reported: false` attribute. The existing 5-hour entities and worker fields remain in place so upgrades do not break automations on accounts that still report or reference them. Diagnostic excerpts remove account, email, and session identifiers before being exposed.

## Reporting a bug

A report with logs is much easier to fix. On Home Assistant 2026.11:

1. Note the date and time of the failed interaction, with your time zone.
2. Open the worker app log: Settings, Apps, Codex for Home Assistant, Log tab. Copy the lines around that time.
3. Download the integration diagnostics: Settings, Devices & services, Codex, three-dot menu, Download diagnostics. Attach the file to the issue.
4. For problems that may involve Home Assistant Core, open Settings, System, Logs, type `codex` in the filter box, and copy the matching lines.
5. Remove secrets, tokens, device codes, private URLs, and personal configuration before posting, then [open an issue](https://github.com/fatherdoctor/ha_codex/issues/new/choose).

## Notifications

A notification about a chat goes out only while the chat waits for you: a question to answer, or a permission request to decide. A completed answer and a failure send nothing; read both in the chat. The one exception is a block by OpenAI's safety filter: it sends one notification, with the id `codex_safety_block_<task_id>`, and that notification stays until you dismiss it (see [When OpenAI's safety filter blocks a task](#when-openais-safety-filter-blocks-a-task)). A chat has one notification at a time, with the id `codex_cli_wait_<task_id>`. The app dismisses the persistent notification as soon as the chat no longer waits: you answer, you stop the chat, you delete it, the request expires, or the app restarts. With a companion app service (`notify.mobile_app_...`), the push carries the same id as its `tag`, and the app clears it from your phone at the same time. Any other notify service cannot clear a push, so that push stays until you clear it; a tap on it after the wait ended does nothing. An answer that waits in the queue behind another chat keeps the notification until the answer starts. A dismissal that Home Assistant does not take is not tried again.

Some notifications are not about a chat. **Codex CLI is not signed in** and the sign-in code (id `codex_cli_login`) go away when you sign in or stop the sign-in. **Codex sign-in complete**, **Codex sign-in failed** and **Codex signed out** (id `codex_cli_worker`) and the GitHub notice (id `codex_cli_github`) stay until you dismiss them. Leave `notify_service` unset or empty to use Home Assistant persistent notifications. If you want push notifications, set it to a Home Assistant notify service such as `notify.mobile_app_your_phone`.

From **0.1.69**, a question sent to a mobile app notify service has a button for each answer Codex offers. See [Human Input](#human-input).

Home Assistant app configuration schemas do not currently provide a Home Assistant service autocomplete selector, so this option remains a plain text field.

## Human Input

Tasks run without an interactive terminal. If Codex needs a decision, it calls the `ask_owner` tool with a question, ends its turn and writes the same question as its answer. The task is marked `waiting_for_input`, and your answer continues it as the next message. If the task cannot be done, Codex calls `report_failure` with the reason, and the task is marked failed with that reason. Up to 0.3.2, Codex returned the status `needs_input` or `failed` in a JSON answer instead; the page still reads that answer from an older thread. Sandbox permission requests use the separate [approval flow](#permission-requests) during the running exchange.

### When OpenAI's safety filter blocks a task

The worker stops the exchange when Codex reports its typed cybersecurity safety block.
The task fails, and you get one notice with the task id: through the notify service when one is set, or as a
persistent notification otherwise. Its id is `codex_safety_block_<task_id>`, and it stays until you dismiss it.
Rephrase the request or retry it yourself. Normal use needs no special access.
The worker does not retry this block automatically or change any permission.
Any file changes made before the block still receive the usual backup and configuration checks.

The worker also fires `codex_cli_safety_blocked`, with `task_id` and `turn_id` only.
These are the worker's ids, as in `codex_cli_task_result`.
The notification and event do not copy the prompt, chat title, or provider error text.
The notification stays until you dismiss it; another block in the same task replaces it.

Detection uses `codexErrorInfo: "cyberPolicy"` from the pinned Codex 0.166.0 app-server.
It accepts an `error` notification or a failed `turn/completed` notification for the current thread and turn.
It does not guess from an assistant's answer, a screenshot, a generic error or an exit code.
It covers tasks the worker launches, including scheduled tasks.
The paired ChatGPT app's own remote threads are not observed through this transport.
Other filter errors without this typed signal keep the normal error path.

The source contract is Codex's [TurnError](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs#L439),
[ErrorNotification](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-protocol/src/protocol/v2/notification.rs#L62),
and [CyberPolicy translation](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-protocol/src/protocol/v2/shared.rs#L86).
The worker reads this type from the app server notification. Earlier versions could also start `codex exec`, whose JSONL output drops the type and has only a message.
No test sends a request to a model to cause a block. Tests simulate the source-defined notifications.

The worker reads these calls from the exchange's events and decides the status in this order:

1. A failure that the worker finds itself: no answer, an exit with an error, a timeout, a stall, or a configuration that fails its check. A question is not asked over a broken configuration.
2. A `report_failure` call.
3. An `ask_owner` call.
4. Completed.

Of several calls to one tool, the last counts. Only a call that finished counts, and only one within the limits: a question or reason of 1 to 2,000 characters, and at most three choices of up to 200 characters each. Neither tool asks you for permission. With no output format, a question that Codex writes in its answer without calling `ask_owner` gives a completed exchange with no choice buttons; you can still answer it in the chat.

From **0.1.69**, Codex can offer up to three short answers with its question, for example **Go ahead** and **Don't change anything**. When it asks whether to go ahead with a change, it is told to say in its answer exactly what it would change.

- **In the chat**, the choices are buttons under the question. Picking one sends it as your next message. You can still type a different answer in the message box, and a draft there stays as it is. Once the question is answered the buttons are gone, and the question stays in the history.
- **In a notification**, a mobile app notify service (`notify.mobile_app_...`) shows a button for each choice, and tapping one sends that answer. The **Codex** integration receives the tap, so it has to be installed and at **0.1.69** or later. Any other notify service, and the persistent notification, lists the choices in the text instead.
- **In automations**, the `codex_cli_task_result` event carries `choices` (a list, empty when Codex offered none) and `turn_id`, and the **Last task** sensor has the same two attributes. Answer with `codex_cli.reply_task`. Its reply is free text, so to pick a choice, send that choice's text.

An answer goes to the question it was given for. `codex_cli.reply_task` accepts an optional `turn_id`. When it is set, the worker takes the reply only while that turn's question is the one waiting, and refuses it otherwise. The buttons in the chat and in notifications always send it, so a button on an old notification cannot answer a newer question in the same chat. A tap the worker refuses makes no notification; the integration writes the reason to the Home Assistant log. A reply without `turn_id` works as before.

If another chat is working when you pick a choice, the answer waits in the [queue](#queueing-messages-while-another-chat-works) and is sent when that chat finishes. Until then the chat takes no other message, and the answer is sent only if its question is still the one waiting.

The choices are a convenience and not a safeguard. Codex decides when to ask, and nothing stops it from changing files without asking. Use the app's `AGENTS.md` to tell it when you want to be asked first.

API: `POST /tasks/<task_id>/reply` and `POST /tasks/<task_id>/continue` accept `turn_id`, and `choice` in place of the text: the position of one of that turn's choices, counted from 0. `choice` needs `turn_id`, because a position means nothing without its question. A `turn_id` that is not the waiting turn returns HTTP 409. A `choice` without `turn_id`, or one the question does not offer, returns HTTP 400. Each turn in `GET /tasks/<task_id>` has `choices`, and `latest_task` in `GET /status` has `choices` and `current_turn_id`. The action of a notification button is `CODEX_CLI_CHOICE_<position>_<turn_id>_<task_id>`, which Home Assistant delivers in the `mobile_app_notification_action` event.

## Saved conversations

The web UI lists recent chats in a resizable sidebar and opens their messages on the right. On mobile, the sidebar becomes a drawer. A tap on a chat keeps the drawer open, so you can look at several chats; tap the page beside the drawer, or the close button, to shut it. Select a saved chat and send a message to continue, or choose **New chat** for a fresh session. Settings contains account controls and workspace instructions.

To make the text easier to read, open **Settings** > **Text size** and choose **Normal**, **Larger** or **Largest**. The choice applies to the whole page and is saved in that browser only, so a phone and a tablet can each keep their own size.

In 0.5, the message box sends text starting with `/` to Codex as an ordinary message. For example, sending `/new` sends those four characters. Use **New chat** to start a conversation, the model chip to choose a model or reasoning level, and the chat menu for actions on a saved chat.

Below **Home Assistant workspace**, the sidebar shows how much of the account's 5-hour and 7-day quota is left. From **0.1.67**, each has a bar next to the percentage. The filled part is the quota left: green, and red when less than 5% is left. Hover over a bar to see when the quota resets. The worker checks the quota every few minutes, and not while a task is running. Until it has a value, the bar is an empty outline.

From **0.1.54**, the web UI reopens the chat you left open the next time it loads, for example after the Home Assistant app on a phone reloads the panel. Choosing **New chat** is remembered as well. The chat id is kept in the browser's local storage under the Home Assistant origin, so it applies to that browser only. Opening the chat requests it from the worker as usual, but the worker does not record which chat you had open. If the remembered chat was deleted from another tab or device, the UI starts with a new chat instead.

### Formatted messages

From **0.1.66**, the chat formats Markdown in the messages you send and in Codex's answers. Wrap code in triple backticks to get a code block, with an optional language name after the opening backticks:

````text
Why does this trigger never fire?

```yaml
trigger:
  - platform: sun
    event: sunset
```
````

Single backticks mark inline code, such as an entity id. Headings, bold and italic text, strikethrough, bulleted and numbered lists, task lists, quotes, tables, horizontal rules, and links are formatted as well. Code blocks keep their indentation and scroll sideways when a line is too long, and so do wide tables. A single Enter stays a line break, and `<br>` breaks a line inside a table cell. The preview of each chat in the sidebar shows the answer without the symbols.

Your own messages follow the same rules, so a line that starts with `#`, `-`, or `1.` becomes a heading or a list item, and text between asterisks or underscores is emphasized. Put configuration, templates, and logs in a code block to show them exactly as written.

Only the display changes. Codex receives your message exactly as you typed it, the saved conversation keeps the original text, and Home Assistant actions and the `codex_cli_task_result` event return `summary`, `question`, and `details` unchanged. The steps in the activity list are not formatted.

Messages are treated as untrusted text:

- HTML is shown as typed and is never interpreted. The one exception is `<br>` inside a table cell, which breaks the line.
- Links open in a new tab and are limited to `http`, `https`, and `mailto` addresses. Any other target, such as a file path or a script address, shows its label as plain text.
- Images are never loaded from a message, because a remote image would tell its server that the message was read. They appear as links. Images that Codex generates or that you attach are shown as before.
- A message longer than 50,000 characters, or one that takes unusually long to format, is shown as plain text.

The Markdown parser, [marked](https://github.com/markedjs/marked) 18.0.14, is bundled with the app under `web/assets/vendor/` with its MIT license, so the chat does not load scripts from another server. The arrows are two Material Design Icons paths (`mdiChevronRight` and `mdiChevronDown` from [@mdi/js](https://github.com/Templarian/MaterialDesign-JS) 7.4.47, Apache-2.0, the icon set Home Assistant uses), inlined in `web/assets/chat.css` and `web/index.html`; `web/NOTICE` keeps the license note and goes into the image with the page. Codex decides how it writes its answers; to ask for Markdown, add an instruction to the app's `AGENTS.md` (**Settings** > **Workspace instructions**) as described under [Task output](../custom_components/codex_cli/README.md#task-output).

### Queueing messages while another chat works

From **0.1.69**, you can send the next request without waiting for a working chat to finish. The worker still runs one task at a time, because two runs could edit the same files, reload automations, or restart Home Assistant under each other. While a chat is working, a message sent in a new chat or in another saved chat is placed in a queue instead of being refused. The note above the message box says so before you send, and the send button has a dashed outline.

Queued messages are listed under **In queue** at the top of the sidebar, in the order they will start, with their place in line. Opening one shows the message with a dashed outline and an **In queue · not sent yet** label. Until it starts, choose **Edit** to change its text (Enter saves, Escape cancels) or **Remove** to take it out of the queue. Removing the message of a chat that has not started removes that chat. Attached images wait with the message and are removed with it.

Up to 50 messages can wait at once. Each chat can have one waiting message, and the chat that is working cannot queue a follow-up to itself. The model and reasoning pickers are locked for a chat while its message waits. The queue keeps the chat's selected overrides; inherited app defaults are resolved when the message starts.

When the running task ends, whether it completed, failed, was stopped, or needs your reply, the oldest queued message starts on its own. This also happens when the web UI is closed. The queue is stored in `/data/message_queue.json` and continues after a worker restart. A message leaves the stored queue only after its chat or exchange is saved, so if the worker stops at that moment the message is either still waiting or recorded as an interrupted exchange, never lost. If a queued follow-up can no longer start, for example because its saved session was removed, it is recorded as a failed exchange in its chat and the next message starts. Deleting a chat removes its waiting message.

API: `POST /tasks`, `POST /tasks/<task_id>/continue`, and `POST /tasks/<task_id>/reply` accept `"queue": true`. With it, a request made while another task is active returns HTTP 202 with `status: "in_queue"`, `task_id`, `queue_id`, `position`, and `active_task_id` instead of HTTP 409. A request made while messages are still waiting joins the queue behind them in the same way. When nothing is running and the queue is empty, the task starts as usual. Without it the endpoints behave as before, so automations and the Home Assistant actions are unchanged. `GET /queue` and the `queue` list in `GET /tasks` return the waiting messages in order, each with `queue_id`, `task_id`, `new_chat`, `title`, `message`, `chat_settings`, `prompt_attachments`, `position`, `created_at`, and `updated_at`. `POST /queue/<queue_id>` with `{"message": "..."}` replaces the text, and `DELETE /queue/<queue_id>` removes the message. Both return HTTP 404 once the message has started. `GET /tasks/<task_id>` includes `queued_message` for a chat with a waiting message. For a chat that has not started, it returns a placeholder with `status: "in_queue"` and no turns; that chat joins the `tasks` list under the same `task_id` when its message starts. `GET /status` reports `queued_message_count`. A second message for a chat that has one waiting, whether it asks for the queue or not, a full queue, and a settings change for a chat with a waiting message return HTTP 409.

### Per-conversation model and reasoning

From **0.1.48**, the pill below the message box opens a model menu and a reasoning slider. From **0.4.0** it works as in the ChatGPT desktop app: one chip shows the model name and then the reasoning level in grey. Select it, or press Ctrl+Shift+M, to open a small panel above it; the chip reads **Select effort** while the panel is open. The panel shows the reasoning level in bold, the model name with a **>** under it, and a slider with one stop per level that model offers. Select the model name to see the model list (**Select model**, the current model checked); choosing a model returns to the panel. The reset button returns the chat to the default model and its default reasoning level, whatever model the chat had selected, as in the ChatGPT apps. Esc or a press outside the panel closes it. The fast mode switch of the desktop app is not offered, because the Codex CLI has no setting for it. Selecting a value in a saved chat saves it immediately; selections for a new chat are saved with its first message. Both persist across worker restarts. Changes apply to the next message and preserve the saved session. Controls are disabled while that conversation is running.

A chat without a model of its own follows the app's model setting. From **0.1.67**, the pill names that model instead of showing **Default**: the model set in the app options or, when that option is `default`, the model Codex picks for the signed-in account. The worker takes the latter from the model Codex's model list (`model/list`) marks as the account's default; before that list is read, from Codex's own status report when it checks the quota; then from the newest Sol model in the account's list; and last from the first model of the built-in list (GPT-6.2 Sol). The menu marks that model, and choosing it makes the chat follow the app setting again. The reasoning reset button sets both the model and the level back to these defaults. New and older chats without saved overrides inherit both defaults. When an exchange starts, it records its resolved `execution_settings`; changing defaults later does not change that running exchange. Queued messages resolve inherited defaults when they start.

With a pin set, the picker offers only the pinned model; clear `model_pin` in the app configuration to use the other choices. With no pin (the default), the picker shows the models Codex lists for the signed-in account, with the levels each one supports. The worker asks Codex for this list (`model/list`) when the page first needs it and again after an hour, so a new model appears with no app update. Until the first answer, or when Codex cannot answer, the picker shows the built-in list below. The retired `gpt-5.5` is never offered. The built-in list follows the bundled CLI 0.166.0 catalog, with GPT-6.2 Sol first, which accounts offer although the CLI's offline catalog stops at GPT-6.1 Sol: Low, Medium, High, Extra High, Max and Ultra for GPT-6.2 Sol, GPT-6 Astra, GPT-6.1 Sol, GPT-6.1 Terra, GPT-6 Sol, GPT-5.6 Sol and GPT-5.6 Terra; through Max for GPT-6 Luna and GPT-5.6 Luna. Ultra enables automatic task delegation. With the app model on `default`, the levels are those of the model Codex picks; a model the worker has no entry for gets the common Low through Extra High choices. If switching models makes a saved reasoning choice incompatible, the UI resets reasoning to the app default. An inherited reasoning level that the model does not support falls back to Medium. So does a level saved in a chat that follows the app model, when that model changes to one without the level; the saved level applies again once the model has it. A chat that had GPT-5.5 selected continues on GPT-5.6 Sol, the next model up, and keeps its reasoning level. Model availability still depends on the account; unavailable-model errors are reported by the CLI.

The authenticated worker API exposes `GET /chat-options` for choices and defaults, with `default_model` naming the model a chat without its own selection runs on and `models_source` saying where the list came from (`codex` or `built-in`), and `POST /tasks/<task_id>/settings` with `{"chat_settings": {"model": "gpt-6-astra", "reasoning_effort": "xhigh"}}` to save selections for an idle chat. Each value can be `null` to inherit the add-on default. `POST /tasks`, `/tasks/<task_id>/continue`, and `/tasks/<task_id>/reply` also accept the optional `chat_settings` object. Omitting the object preserves existing selections. Invalid models and reasoning levels, and a level the selected model lacks, return HTTP 400; changes to active chats return HTTP 409. `GET /tasks/<task_id>` includes the saved `chat_settings`. Home Assistant actions continue using their existing parameters and honor settings already saved for the conversation.

### Live activity

From **0.1.53**, the chat shows what Codex is doing while it works on your latest message. Steps appear under the message as Codex reports them: reasoning headlines, progress notes, the commands it runs, the files it edits, web searches, and tool calls. A failed command or an error is highlighted. When a run is stopped or fails, the list ends with a **Stopped** or **Failed** row. The list is expanded while the run lasts. When it finishes, it closes to one gray line above the answer, **Worked for** and the exchange's total time, which opens the steps and the answer's checks. The text Codex writes along the way ("I'll look for …") shows above the list while Codex works; when the exchange ends, it moves into the list, between the groups of steps, so the answer starts right under the closed line. An earlier exchange keeps the same closed line: it opens that exchange's saved steps, then its checks. Its steps load from the worker the first time you open the line; a chat from before **0.4.0** that saved no steps for an exchange says so. When a check failed, a short **1 check failed** line stays under the answer and opens the checks. A permission request you have answered shrinks to one line, such as **Allowed once: ls /config**, that opens to its details. Codex's plan shows as one to-do step that is updated in place as Codex works through it; a plan update that does not have the expected form, or that has more than 50 entries, is ignored.

From **0.1.63**, the list starts as soon as a message is accepted. The worker reports its own steps alongside those of Codex: recording the state of the configuration files, saving a full snapshot when that option is on, starting Codex, waiting for its first response, and checking the changes afterwards. The heading shows how long the exchange has been running, and its dot pulses for as long as the run lasts.

Command output is not shown by default because it can contain configuration secrets. Each command offers **Show output**, which reveals up to 2,048 characters after the same redaction as the task log and marks output that was cut. File edits list paths only. Reasoning summaries and other activity text are limited to 4,000 characters per field, and at most 500 steps are kept per exchange. The task's `codex.log` retains the underlying redacted event stream.

The worker keeps the steps of the running exchange in memory and writes them to `/data/codex_tasks/<task_id>/turns/<turn_id>/activity.json` once when the run ends, so each exchange's steps survive a page reload and a worker restart. They stay in the chat's folder and are removed with the chat; each exchange keeps at most 500 steps, with long text and command output cut short. The authenticated worker API exposes `GET /tasks/<task_id>/activity?after=<seq>`, which returns `turn_id`, `running`, `seq`, `total`, the `steps` whose sequence number is above `after`, and `elapsed_ms` while the exchange runs. Add `turn=<turn_id>` to read an earlier exchange of the same chat instead of the latest one; a turn id that is not one of that chat's exchanges gets 404. A step that changes, such as a command that finishes, is returned again with a new sequence number and the same `index`. The web UI polls this endpoint once a second while a chat is working and pauses when the tab is hidden. The Home Assistant integration does not use it.

### Generated images

From **0.1.49**, images that Codex generates during a conversation appear in the chat under the response that produced them. Click an image to open it at full size, or use **Download** to save it. Images stay with their exchange across reloads, worker restarts, and continued conversations.

Image generation uses Codex's built-in `image_gen` tool with the signed-in ChatGPT account. It needs no separate OpenAI API key. Tool availability and usage limits depend on the account. The app currently bundles Codex CLI 0.166.0.

After each run, the worker reads the session's rollout file under `/data/codex-home/sessions` and looks for completed `image_gen.generation` items that were not already present before the exchange. This works for app-server turns, including images absent from the live event stream. Codex saves each result under `/data/codex-home/generated_images/<session_id>/`. The worker copies the file into `/data/codex_tasks/<task_id>/turns/<turn_id>/attachments/` and records metadata on that exchange. If the saved file is missing, the worker decodes the inline result from the rollout instead. The original stays where Codex saved it so follow-up edits in the same conversation keep working.

The page's rules tell Codex that generated images are attached automatically and that it should leave them at the default save location unless the user asks for a file at a specific path, for example under `/config/www` for use on a dashboard.

Limits and retention: only PNG, JPEG, GIF, and WebP content is accepted, checked by inspecting the file rather than trusting its name. Files larger than 25 MiB and images beyond the first 12 per exchange are skipped and noted in the task log. Generated images are retained with the task history and are removed when the task directory is deleted; there is no automatic expiry for them. Files and images that you attach to a message are kept in `/data/chat_uploads`, outside the backup, and removed after 7 days (the chat then shows "Attachment removed after 7 days"; the attachment API returns HTTP 410). The worker serves them through its authenticated attachment API and does not publish them under `/config/www`; Codex can copy an image there if you request it.

API: `GET /tasks/<task_id>/attachments/<attachment_id>` returns the image with its recorded content type and `X-Content-Type-Options: nosniff`. Add `?download=1` for a download disposition. The endpoint requires the worker API token or an authenticated Home Assistant Ingress session. Before sending, it re-checks the stored file's size, SHA-256, and image signature against the recorded metadata, and returns HTTP 404 for unknown, missing, oversized, or altered files. `GET /tasks/<task_id>` and `codex_cli.get_task` include an `attachments` list on each turn and on the task-level result for the latest exchange. Each entry has `attachment_id`, `kind` (`image`), `name`, `mime_type`, `size`, `sha256`, `created_at`, `revised_prompt`, `generation_id`, `path` (relative to the task directory, or to `/data/chat_uploads/<task_id>` when `store` is `data`), `store` (`data` for an upload kept in `/data/chat_uploads`; absent for files kept with the task), and `url` (relative to the worker). The `codex_cli_task_result` event includes the same `attachments` list; it is empty for cancelled and failed launches.

If an image does not appear, open the task log and look for `Skipped image generation` or `Could not attach image generation` lines, which explain why the worker did not attach the file.

### Attaching images

From **0.1.51**, you can attach images to a message: a screenshot of a dashboard problem, a photo of a device, or a design mockup. Use the paperclip button next to the model pill, paste an image from the clipboard into the message box, or drop image files onto the composer. Pending images appear above the message box with a remove button and go out with the next message, in a new chat or when continuing a saved one. They then appear with your message in the conversation and stay with it across reloads and worker restarts.

Codex receives each image with the message as a `localImage` input in sandboxed app-server turns. The `exchange_info` tool also names the attached files. Images become part of the saved session and can contribute to the context of later turns, so the UI resizes images whose longest edge exceeds 2048 pixels before uploading.

In the Home Assistant Android app, the paperclip picks one image at a time; use it again to add more. The app's file chooser returns nothing when a multi-select picker is used, so the web UI asks Android WebViews for a single selection. Paste and drag-and-drop still accept several images at once.

Limits: PNG, JPEG, GIF, and WebP only, checked by inspecting the bytes rather than the file name; at most 6 images per message and 10 MiB per image after resizing. The worker stores attached images and files under `/data/chat_uploads/<task_id>/<turn_id>/`, which the app's backup leaves out, serves them through its authenticated API, and removes them after 7 days or with the chat, whichever comes first. It does not publish uploads under `/config/www`.

API: `POST /tasks`, `POST /tasks/<task_id>/continue`, and `POST /tasks/<task_id>/reply` accept an optional `attachments` list of `{"name": "shot.png", "data": "<base64>"}` objects; a `data:` URL prefix is tolerated. Invalid entries return HTTP 400 and no task or turn is created, and requests over roughly 81 MiB return HTTP 413. Each turn in `GET /tasks/<task_id>` and `codex_cli.get_task` lists its uploads under `prompt_attachments` with the same fields as generated images plus `origin: "user"`; generated images carry `origin: "codex"`. `GET /tasks/<task_id>/attachments/<attachment_id>` serves both kinds with the same checks. The `attachments` list on the task result and in the `codex_cli_task_result` event contains only images generated by Codex.

### Files Codex makes for you

When you ask for a file, such as a spreadsheet, a report or an export, Codex saves it in the chat's own files
folder, `/data/codex_tasks/<task_id>/files/`, and links to it in its answer. The chat page shows that link as a card
with the file's name and size; select it to download the file. The page and the worker offer only files in that
folder: a link to any other path, to a hidden file (a name that starts with a dot) or to a symbolic link stays plain
text. Codex can write in this folder in the **Default**, **Auto-review** and **Full access** modes; in **Read only**
it asks you first, and the file is saved only after your **Allow once**. The folder is not under `/config`, so its files are not part of the change review, the
configuration check or the backups of `/config`. It is deleted with the chat. Up to 200 files are listed for the page.

The remote connection in the ChatGPT app runs in OpenAI's app and cannot reach files on your Home Assistant system,
so a file link in an answer read there does not download. Open the chat on the app's page to download it.

API: `GET /tasks/<task_id>` includes `chat_files`: `{"folder": "/data/codex_tasks/<task_id>/files", "files":
[{"path": "2025/visits.xlsx", "size": 11}]}`, or `null` when the chat has no folder yet.
`GET /tasks/<task_id>/files/<path>` returns one of those files as a download (`application/octet-stream`,
`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`), never shown inside the page. It requires the
worker API token or an authenticated Home Assistant Ingress session, opens the file without following a symbolic
link, and returns HTTP 404 for a path outside the folder, a hidden name, a folder, a missing file or an unknown chat.

### Managing chats

From **0.1.50**, each chat in the sidebar has an actions menu. On desktop, hover over a chat and choose the **⋯** button, or right-click the row. On phones, the selected chat shows a **⋯** button where its date was, and a long-press on any chat opens the same bottom sheet. With a keyboard, focus a chat and press Shift+F10; arrow keys move between items and Escape closes the menu.

The menu lists, in this order: **Rename**, **Pin chat**, **Share**, **Archive** and **Delete**, as the ChatGPT app
orders them. The conversation header has no menu of its own; open a chat's menu from the chat list.

**Share** downloads the chat as a Markdown file: the whole chat, or only its last lines (a number from 1 to
100,000). The file has each of your messages, the steps Codex printed for it (thinking, commands, file changes and
tool calls, with the marks the page shows) and Codex's answer. The file masks what the task log masks, and also a
JSON Web Token (the form of a Home Assistant long-lived access token), an `sk-` API key, a value after a key such
as `password`, `passwd`, `secret` or `pin`, and the password in a URL; each one becomes `[redacted]`. A quoted value
is masked inside its quotes, also when it runs on to the next line; a value with no quotes is masked to the end of
its line, so the rest of that line is hidden too. The mask cannot know names, addresses, or a secret that has no label and no known form. Nothing is sent anywhere; read the
file before you give it to anyone. API: `GET /tasks/<task_id>/share` returns the file, and `?lines=<n>` keeps only the last `n` lines.

**Pin chat** keeps the conversation at the top of the list under a **Pinned** heading, ahead of the recently active chats; **Unpin chat** returns it to the recent list. **Rename** opens a dialog for a new title, which appears in the sidebar and the conversation header. Neither action changes the chat's `updated_at` time or its saved messages. **Delete** asks for confirmation, then removes the conversation and its saved history. The delete action is unavailable while that chat is running; stop the task first.

Deleting a chat removes `/data/codex_tasks/<task_id>/`, including prompts, output, backups, and generated images, and `/data/chat_uploads/<task_id>/` with the files you attached, and the entry from the task index. The worker also attempts to remove the Codex session rollout file under `/data/codex-home/sessions` and generated images under `/data/codex-home/generated_images/<session_id>/`. Failures to remove those session files are logged. Deletion cannot be undone in the chat UI and does not erase copies in separate Home Assistant backups.

### Where the app keeps its working files

Every chat's files are in the app's private folder, `/data/codex_tasks/<task_id>/`: the prompts, `codex.log`, `task.json`, the per-turn backups and the generated images. The folder `auth/` in `/data/codex_tasks` holds the log of the ChatGPT sign-in: the `codex login` output, which includes the sign-in link and the one-time code (a code works for 15 minutes only), with token-like values redacted. The files you attach to a message are in `/data/chat_uploads/<task_id>/` for 7 days. None of this is in Home Assistant's configuration folder, so the file editor, Samba and a backup of that folder do not hold it. The app's own backup holds all of it except the files you attached: it leaves out `/data/chat_uploads`. The app writes nothing under `/config/www`. In `/config` it writes only the files Codex changes for you, the integration's folder `custom_components/codex_cli` with its work folder `.codex_cli_install` (which must be on the same file system for the swap to be atomic, and which Home Assistant never loads from), and `AGENTS.md` when you edit it on the page.

Versions before 0.4 kept the chats in `/config/codex_tasks` (or the folder the removed `task_root` option named) and wrote the sign-in QR images to `/config/www/codex_cli_auth`. On the first start of 0.4 the app moves each chat from `/config/codex_tasks` to `/data/codex_tasks` and then removes the old folder. If the app still finds a saved **Task root** value in its options, it takes from that folder only the chat folders (each holds a `task.json`) and the sign-in log folder `auth`; every other file stays, and the folder is removed only when it is then empty. A folder the app cannot read is left as it is, with a line in the app log, and the app starts. It moves, never copies: a chat is renamed, or copied whole to a staging folder in `/data` and renamed into place, and only then deleted from the old place. A chat that cannot be moved stays where it was, with a line in the app log, and is tried again at the next start. The same start deletes the `codex_login_*.svg` images and their folder; a file in that folder that the app did not write stays.

API: `POST /tasks/<task_id>/pin` with `{"pinned": true}` or `{"pinned": false}` returns the saved state. `POST /tasks/<task_id>/title` with `{"title": "Porch lights"}` trims and collapses whitespace, rejects empty titles and titles longer than 200 characters with HTTP 400, and returns the stored title. Both work on running chats and return HTTP 404 for unknown tasks and HTTP 500 if the metadata could not be written, in which case the previous values are kept. `DELETE /tasks/<task_id>` returns HTTP 409 for queued or running tasks, HTTP 404 for unknown tasks, and HTTP 500 if the task files could not be removed, in which case the chat stays listed. Summary entries from `GET /tasks?summary=true` include a `pinned` flag, and `order=pinned_first` lists pinned chats before the others while keeping the most recently updated first within each group. The `codex_cli.list_tasks` action keeps its existing `created_asc` and `updated_desc` orders.

### Continuing and browsing chats

`codex_cli.continue_task` accepts `task_id` and `message` and resumes completed, waiting, failed, or cancelled tasks. The old `reply_task` action remains restricted to tasks waiting for input. Both use the same resume implementation. Continuation requires a saved session under `/data/codex-home/sessions`; the worker does not fall back to a fresh chat if it is missing.

`GET /tasks` and `codex_cli.list_tasks` accept optional `limit` (1–500), `offset`, `status`, `order` (`created_asc` or `updated_desc`; the worker API also accepts `pinned_first`), and `summary` parameters. `summary: true` returns compact entries for the sidebar. Without filters, all tasks are returned in their original creation order. `GET /tasks/<task_id>` includes `turns`, `history_incomplete`, and `can_continue`. The latter indicates an eligible task with a recorded session ID; availability of its session file is checked when continuing. `POST /tasks/<task_id>/continue` accepts a JSON `message`.

Each new exchange stores its user message, timestamps, status, and response in the task's `turns` list. Prompts, final output, backups, and the list of changed files are stored separately under `/data/codex_tasks/<task_id>/turns/<turn_id>/`. The task-level result and existing result events continue to describe the latest exchange. Old tasks are adapted from their available prompt, replies, and last result, with a notice that earlier responses may be missing. When a chat's `task.json` is lost and the chat is rebuilt from the task index, its latest exchange keeps its turn id, so the steps saved for that exchange stay linked to it after you continue the chat. History is retained until its files are removed; there is no automatic expiry, except for [backups](#backups).

For local browser checks, run `python codex-cli-worker/tests/web_fixture.py` from the repository root, then `node codex-cli-worker/tests/browser_smoke.cjs` with Playwright available. The fixture uses temporary storage and simulated responses, and never launches Codex or contacts Home Assistant. Set `CODEX_CHAT_BROWSER=msedge` to use installed Edge, and optionally `CODEX_CHAT_QA_DIR` to choose a screenshot directory outside the repository.


### ABI and relay fallback acceptance (N8/N9)

The AppArmor ABI declaration enables domain/stack and network_v8 mediation on HAOS; ptrace was already mediated. Both tool labels keep their existing Unix/raw-netlink socket permissions, so sandboxed AF_INET/AF_INET6/packet socket creation is denied. Approved escalations keep the existing parent/relay policy. The maintainer must run an ordinary chat-page shell read and the history MCP tool after loading the updated HAOS profile; CI's Ubuntu kernel is not that acceptance.

The plain relay route still creates a user namespace and maps to the fixed non-root identity. It omits the private hostname, not the namespace requirement. If namespace creation is refused, it fails closed and retries with backoff. No working fallback is promised in that case. See [sandbox checks](../codex-cli-worker/SANDBOX_TESTING.md#abi-socket-family-change-and-namespace-fallback). The scratch-folder proposal is in [CX-HA-URL's design note](../docs/design/CX-HA-URL.md#scratch-folder-proposal-do-not-build); this change creates no notes folder or sandbox bind.

## Scheduled runs

To schedule a chat, open its menu in the chat list and choose **Schedule**. The schedule editor opens with the chat's title as the name and its first message as the prompt. Choose a **Model**, or leave **Add-on default**, and a **Trigger**: **Hourly**, **Daily** at a time, **Weekly** on a day and time, or **Custom**. Custom holds the other timings: every N minutes or hours, a cron line, or one date and time. **Advanced settings** holds the permissions, **Default** or **Read only**. Default follows the app's configured sandbox; Read only narrows that mode. **Create schedule** authorizes the exact prompt and timing you submitted. If the `model_pin` option later names a different model than the schedule, its runs fail and each one says so in the run list; edit the schedule and choose the pinned model or **Add-on default**.

Each run starts a new chat; the chat you scheduled does not grow. **Schedule** in the menu of the chat you scheduled, or of any chat a run started, opens that schedule again. There, **Advanced settings** also shows its state, its recent runs, **Pause** or **Resume**, and **Delete schedule**. A chat's menu opens one schedule: the newest one made from that chat.

**Settings → Scheduled runs** lists every schedule, also one whose chat was deleted and one saved by an earlier version, which belongs to no chat. **Edit** opens the same editor. Codex can propose a schedule; the proposal stays off until an administrator reviews and approves it there, with **Approve this proposal** or by saving it in the editor. Codex can edit or delete only its own unapproved proposals; an approved schedule is owner-only. If a proposal changes while you review it, refresh before approving it. Proposals whose prompt contains masked credentials cannot be approved or prefilled for editing; create a replacement with a complete prompt.

Intervals accept whole minutes or hours, with a minimum of one minute. Hourly is an interval of one hour, counted from the time you save. Daily and Weekly are cron lines at the time you chose. One-off entry and displayed dates use Home Assistant’s configured IANA time zone, named under the trigger. Cron follows that local wall time across daylight-saving changes: missing minutes are skipped and repeated minutes run once, at the first occurrence. One-off nonexistent local times are refused; repeated times use their first occurrence. Entry and scheduled dispatch pause if the Home Assistant time zone is unavailable. Explicit API timestamps with an offset remain absolute instants. Cron uses five numeric local fields: minute, hour, day of month, month and weekday. Lists, ranges and steps are supported. Sunday is 0 or 7. When neither day field starts with a wildcard, either can match. A leading wildcard, including a step such as `*/2`, keeps the other day field restrictive. The next occurrence must be within 366 days. A cron schedule disables after its last occurrence if no next occurrence lies within that horizon; edit it to enable it again. One-off schedules disable after their occurrence is reserved. Pause, resume, edit or delete schedules from the editor or the list; deleting a schedule stops future runs, not a task already in progress. Cancel a running task in its chat.

The worker checks due schedules every five seconds. It uses the ordinary task launcher, backups, validation and permission checks, and starts at most one scheduled task per check. Due work waits while the assistant is busy. A missed interval coalesces into one run, without a burst of catch-up tasks. Limits are 100 approved schedules plus 20 unapproved proposals, 24 runs per schedule per UTC day, and 64 scheduled runs total per UTC day. A run that reaches a limit waits until a later UTC day. Completed run history retains the latest 500 records; active records are kept separately. Schedules, occurrence reservations and run history live in `/data/schedules.sqlite3` and survive app restarts and updates. Reservation and launch share the normal chat-slot lock; a busy launch restores the due occurrence without overwriting a concurrent owner edit. A crash after reservation but before saving the ordinary task records an interrupted run and does not retry it automatically.

**Permission requests remain human decisions.** A scheduled run has the tools of a chat on the page, the `call` tool that changes the house included, and Codex asks before each `call` as it does in a chat. The run log shows **waiting for approval** while a request is pending, and the chat page shows its yellow warning. Open the run's chat to review the command, diff or Home Assistant action and choose **Allow once** or **Deny** as an administrator. Nothing that a chat on the page would ask about runs without that answer. The scheduler cannot answer it. The parked exchange keeps the worker's normal execution slot; other schedules do not bypass it. Existing task timeouts, cancellation and restart expiration still apply. A question instead shows **waiting for input**. Scheduled tasks report completion, failure, cancellation and questions in the run log and chat without pushes. Only their existing permission-wait notification uses `notify_service` (or persistent Home Assistant notifications).

Codex on the worker receives the `codex_scheduling` tools `propose_schedule` and `list_schedules`. They use a running-turn capability that expires when the exchange ends. Proposal tools cannot approve schedules or grant native permissions. Schedule summaries are paginated in groups of 10 by default (`limit` can select 1–20; a larger number returns 20 with `next_offset` to read the rest, not an error); prompt excerpts are limited to 512 characters. A refused call says why in plain words, for example that `offset` is out of range or that the exchange has ended. Read the complete proposed prompt on the page before approving. They run without a permission card because these limits already bound them. If any other tool asks a question, the page cannot show it: the request is declined and the chat says so. Only the Home Assistant tool's own permission request gets a card (see [permission requests](#permission-requests)). These tools are for page tasks; native remote threads do not receive page-task capabilities.

A build-time switch, `SCHEDULED_RUNS` in `scheduling.py`, can hide all of this again; saved schedules stay in the database while it is off.

## Interaction logs for a bug report

Home Assistant's integration **Download diagnostics** button (Settings, Devices
& services, Codex, three-dot menu) downloads the anonymized interaction logs
inside Home Assistant's own diagnostics envelope, with system information,
including the time zone, component information and the integration manifest.
Review the entire downloaded file before sharing it. The button does not send a
report to another service. The add-on page no longer has its own Export logs
button (removed in 0.3.6); `GET /diagnostics/export` still returns the same data
to an administrator.

The worker's data excludes connection settings, accounts, task titles, prompts,
answers and raw tool output. The former integration entry/coordinator snapshot
was removed for this privacy boundary. Error descriptions and tool/model
categories come from fixed allowlists. Numeric release strings are retained;
ambiguous address-shaped four-part values are rejected. Unknown values remain
unknown. A test ties the bundled CLI version to its Dockerfile pin.

The **Interactions to keep** option (`interactions_to_keep`, 10–100) on the app's
Configuration tab sets how many interactions are kept. Left empty, the app keeps the
number it has: **25** on a new install, or the number saved on the page before 0.3.5,
when Settings had the field. The byte cap scales from 128 KiB at ten to 1,280 KiB at 100. Completed
records are removed before still-running interactions; reducing retention also
preserves running work. Per record, retain at most 48 events, 64 entity
references, 32 file categories and 64 operation/domain counters. Raw paths under
`/config` become only an allowlisted folder category and extension. Entity IDs
become HMAC-SHA256 references using a random installation key in private add-on
data, mode 0600. The key is never included in either export. Each download adds
a fresh, unexported salt, keeping an entity's public reference consistent within
one file while changing it between downloads. Legacy unkeyed references are
dropped during migration because their original IDs cannot safely be recovered.
Add-on backups carry the private key and the diagnostic store together; keyed references protect a
store copied on its own, not a backup containing both files. The key, retention settings and set-aside
stores are outside the sandbox's writable folders; a command or file change you allow once runs outside the sandbox and can change them.
If the key is unsafe or unreadable, the worker logs a recovery instruction and continues collecting
other metadata without new entity references. Delete `/data/interaction-diagnostics.key` to let the
worker create a fresh private key; old and new entity references will no longer link.

Diagnostic writes are coalesced every eight metadata changes and flushed at
begin, finish and export. Abrupt termination can lose the buffered tail; an
unfinished record is closed at the next worker startup as **unknown (restart)**. Its end time is the
observed restart time, not a claim about when the interrupted action ended; its result remains unknown.
Only this process's still-running records receive priority over completed tasks. A corrupt,
too-deep or wrong-shaped store is set aside privately with a content-free note,
and a fresh store is started. Only the newest set-aside corrupt store is kept. Non-object rows are
dropped with a count. Refused approval-policy and policy-parse problems have distinct fixed labels. Failures
of diagnostic collection never decide whether an action runs or a queue advances.

**Remote ChatGPT thread events are not recorded in this first delivery.** Every
export includes `remote ChatGPT threads: not recorded`. No diagnostic connection,
subscription, resume, attachment or remote history read is added. Add-on page
and scheduled tasks are recorded by their worker lifecycle. Relay
read RPCs are grouped separately within one capability lifetime, with counts per
operation/domain; they do not describe the remote conversation or its approvals.
Only worker-visible launch-policy refusals and approval events are included. The export
requires an administrator for Ingress requests; the integration's worker token
remains able to fetch it. Retention changes only on the Configuration tab.
An unavailable worker returns an unavailable data section without its exception
message or connection address.


### Chat page and attachments

The page follows Home Assistant colors in light and dark when its host exposes them; otherwise it follows
the device appearance. Recent conversations stay alongside the writing canvas on tablets and desktop.
Above the messages is one thin row with the chat's title and its menu. The foot of the side bar holds, top to
bottom: **Codex** (the ChatGPT sign-in), **GitHub** and **Remote** (the ChatGPT app connection), each with a word
or two such as Ready, Off or Not signed in; the 5-hour and weekly quota with a line for the plan type and the reset
times; and **Settings**. Select Codex, GitHub or Remote to open Settings at that part of Accounts and connections;
**Manage remote connection** there opens the window that pairs the ChatGPT app. A row that needs you shows an amber
dot, and a row whose state could not be read shows a red dot and Unavailable. The ChatGPT account part says whether
a turn has confirmed Codex access with the current sign-in.

While something needs you, the Settings cog becomes a warning symbol, and on a phone the conversations button
shows the same symbol. It is yellow while an approval or a schedule proposal waits. It is red when ChatGPT is not
signed in or its status cannot be read, when the GitHub sign-in expired or was revoked, or when Remote is on and
lost its connection. Red wins over yellow. A GitHub account that was never connected, or that you disconnected, and
Remote turned off show nothing. **Dismiss the warning**, under Settings, hides the warnings shown at that moment
until something new happens, such as another approval or a new sign-out; the waiting item itself stays. A
reload of the page brings a dismissed warning back. The
ChatGPT warning cannot be dismissed, because no chat runs without that sign-in. A screen reader reads the warning
as the description of the Settings and conversations buttons. The page notices a ChatGPT or GitHub sign-out at its
next status check, within five minutes, and a schedule proposal within a minute.

The integration's state shows only when it needs you, as
a line at the top of the chat. While a task
runs, the send button is the stop button, even with a draft or a file in the message box. On a phone, a slim row holds the
conversations button, the title, New chat and the thread menu; the foot rows are at the bottom of the
conversations drawer. The composer stays at the bottom, with no hint line under it. Its bottom row holds the plus
menu, the Permissions button, the model and reasoning controls and the send button; the Permissions button's chevron
and the send button's arrow are icons centered on their buttons. The model and reasoning controls show the saved chat
choice or configured default, rather than a sample selection.

The plus menu uploads photos or files. A message accepts at most six attachments, up to 10 MB each.
Photos retain their existing resizing and image checks. File uploads support UTF-8 text, Markdown, YAML,
JSON, CSV, logs, Python/JavaScript source, HTML/CSS/XML source, and PDF headers. Binary text and unsupported
extensions are refused. Documents remain passive files: they are never executed or rendered in the page,
and downloads use attachment disposition, no-sniff and a sandboxed content policy. They are stored privately
with the message; Codex receives their validated paths and may read relevant contents as part of that task.
PDF acceptance does not promise a particular extraction tool. Draft attachments stay in the browser until
Send. Archiving a chat hides it from recent conversations without deleting its session or attachments;
Archived chats can restore it. Delete remains a separate action.

### Permission modes

The Permissions button offers the four modes of the ChatGPT app, with the same names. Each mode is only
Codex's own settings, passed when the app starts Codex for the chat's next turn. The app adds no rule of its
own to any of them.

| Mode | Codex settings | A change that needs permission |
|---|---|---|
| Default permissions | sandbox `workspace-write`, `approval_policy = "on-request"`, `approvals_reviewer = "user"` | Codex asks you |
| Auto-review | as Default, with `approvals_reviewer = "auto_review"` | Codex's reviewer allows or denies it; it does not ask you |
| Read only | sandbox `read-only`, `on-request`, `user` | Codex asks you |
| Full access | sandbox `danger-full-access`, `approval_policy = "never"` | runs without a question |

- The app's `codex_sandbox` option stays the limit: when it is `read-only`, Default and Auto-review chats run in
  the read-only sandbox too. Full access does not follow that option: an administrator confirmed its warning, so
  it runs without the sandbox. With the option at `read-only`, a Full access chat also saves no per-file backups.
- Read only limits files and commands. A Home Assistant tool call is not a file or a command, so a device change
  in a Read only chat still happens only after you answer yes on Codex's request.
- In Auto-review the chat shows each review: "Auto-review is checking a command", then whether it allowed it, the
  risk level and the reviewer's reason. The reviewer also answers for locks and messages; that is Codex's model.
- **Full access** is off until an administrator turns it on in **Settings > Full access** and confirms a warning.
  While it is on, **Full access** shows in bold in a chat's Permissions menu. Turning it off returns every chat set to Full access to Default. While Full
  access is on, a ChatGPT app thread can also ask for it; the app never sees those threads, so such a thread can
  mix the settings (for example "never ask" with `workspace-write`). No mix reaches further than Full access itself.
  A turn that is running when the switch goes off finishes in Full access; the chat's next turn runs in Default.
- Scheduled runs use Default or Read only only.
- A queued message keeps the choice submitted with it. Camera and Plan mode are not offered in this build.

A chat's menu holds Rename, Pin, Share, Archive and Delete ([Managing chats](#managing-chats)). The Status
window opens from the Home Assistant row at the foot of the chat list. Status shows the workspace,
selected model and permissions; context and usage/reset remain unavailable in this view until verified
window data is wired. No quota is inferred from activity.

Remote connection opens a separate view for Off, Starting, Ready, Reconnecting and Error. Pairing uses two
different protocol fields: the manual code for typing, and the opaque code for the Continue in ChatGPT URL
and its QR. The manual code is never substituted into the URL. If only a manual code is available, request
a new code or type it; no app link is invented. The QR has a four-module quiet zone at every width. Codes,
URLs and QR images remain limited to verified Ingress administrators; API tokens alone cannot read them
or start pairing. No live account enrollment or device handoff is implied by local rendering tests.

Settings → Workspace instructions → Workspace rules opens a separate editor. The file is read only when that editor
opens, and Save remains an explicit action. Existing account, diagnostic and scheduling controls remain in
Settings. Version 0.4.0 removed **Advanced** with its **Browser permissions** part, with the dashboard browser. The
**Add-on configuration** row at the foot of Settings opens the app's Configuration tab in Home Assistant
(`/config/app/<slug>/config`), where the plain options are set. When Supervisor cannot name the app, the row
is not a link and says to open the Configuration tab from the Home Assistant settings.


A chat's **Read only** choice also disables worker-mediated dashboard saves: both
verification with `save_pending` and the end-of-turn automatic save use the current
turn's effective options. A dashboard file changed by another actor during that turn
does not grant the chat permission to save it. The worker rechecks the mode immediately
before saving; ordinary dashboard inspection remains available without `save_pending`.
The direct **Status** button is available on tablet and desktop. On the phone, open
Status from the thread menu.


### Checking Codex account access

You need a ChatGPT account with Codex access. The app cannot confirm access from a passive check; it warns
when the sign-in is missing or explicitly expired, and a successful task confirms access for that sign-in.

A ChatGPT account with Codex access is required. OpenAI's [availability page](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan),
read November 13, 2026, includes Codex across ChatGPT plans, including Free and Go; usage limits and workspace
settings still apply. Codex Cloud eligibility is separate from the local CLI used by this app.

The pinned CLI does not expose a documented boolean for account entitlement in `account/read`.
Its rate-limit reader reports ordinary usage eligibility, which may also reflect exhausted allowance,
and converts backend failures into a generic RPC error. A denied usage read cannot establish that the
account lacks Codex access. The worker checks sign-in state on every status read and passively refreshes it at startup and at least
daily while idle. Busy checks retry on the next minute; signing in also forces the existing passive usage
read, whose plan enum supplies informational Status text. No task is submitted to check an account.

The existing Home Assistant sign-in Repair appears only for signed-out or known expired/rejected sign-in
state, and clears on a good sign-in or a successful task using the same credentials. Unknown state raises
nothing. The passive readers recognize explicit revoked-token markers and the pinned refresh-token expired, reused and invalidated codes and their exact user-facing messages; generic HTTP errors, quota exhaustion and plan names do not imply missing access. No distinct
task-level entitlement error was verified, so no entitlement mapping is added. Task sign-in failures now use
the same explicit rejection signals as passive checks, plus the exact observed revocation phrase
`workspace routing discovery unauthorized (401)`; generic task HTTP failures do not raise the Repair. Status explicitly says when passive reads cannot confirm Codex access.

Passive usage failures treat generic HTTP 401/403, rate exhaustion and network errors as unknown, not expired sign-in. Only explicit Codex sign-in rejection signals can raise the sign-in Repair. Status exposes fixed usage-error messages with `rate_limits_error_category` (`unavailable`, `sign_in_rejected` or `missing_window`); raw upstream errors and failed-read excerpts are omitted, including older cached failures.
A real successful turn can confirm access for its current sign-in; changing credentials invalidates that
confirmation and cached plan information. The new metadata cache retains no account email, token or raw error.

Source: [account type at rust-v0.166.0](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L18-L38),
[rate-limit errors](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/app-server/src/request_processors/account_processor.rs#L1171-L1218),
and [usage eligibility](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/backend-client/src/client/rate_limit_resets.rs#L34-L66).

The daily reader's refresh-code mapping follows
[login/src/auth/manager.rs:1760](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/manager.rs#L1760-L1772).
Plan labels follow [protocol/src/account.rs](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/protocol/src/account.rs#L10-L50).
The [task error enum](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/protocol/src/protocol.rs#L2052-L2090)
separates authentication and limits but supplies no verified, distinct missing-Codex-entitlement signal.

The exact user-facing refresh messages are pinned at
[login/src/auth/manager.rs:224-226](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/manager.rs#L224-L226).
The new daily reader never clears a remembered rejection; a new sign-in or successful matching turn does.

#### Sign-in expiry notices

The pinned Codex CLI does not provide a reliable end time for the ChatGPT sign-in. An access token's
`exp` claim is the expiry of a refreshable access token, not the end of the sign-in. At
[rust-v0.166.0, `token_data.rs:157-161`](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/token_data.rs#L157-L161),
Codex parses that claim; its
[proactive refresh check](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/manager.rs#L3333-L3351)
requests renewal near that expiry. If no usable expiry is present, `last_refresh` instead schedules
a refresh after eight days, using the
[refresh interval constants](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/manager.rs#L221-L222).
Neither value tells us when the owner must sign in again.

The stored [token record](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/token_data.rs#L10-L34),
[auth record](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/storage.rs#L48-L61)
and [refresh response](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/manager.rs#L1786-L1791)
expose no refresh-token lifetime or guaranteed sign-in deadline. A successful refresh
[persists the renewed tokens](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/manager.rs#L3417-L3455).
An expired, reused or revoked refresh grant is learned from the
[refresh rejection](https://github.com/openai/codex/blob/6ff1e1ca3b30/codex-rs/login/src/auth/manager.rs#L1735-L1783),
not from an advance end time.

For that reason, Status has no sign-in expiry timestamp, and the integration provides no expiry
sensor or guessed 72-hour, 24-hour or 6-hour warning tiers. It retains the existing signed-out or
explicitly rejected sign-in Repair described above, clearing after renewal is confirmed. The add-on
sends no advance-expiry push. Expiry estimation adds no credential-file reader, token decoder
or model task.
