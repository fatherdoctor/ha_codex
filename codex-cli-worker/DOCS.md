# Codex for Home Assistant

This app runs Codex CLI tasks against the Home Assistant config folder mounted at `/config`.

## Copying responses

Choose **Copy response** below any answer in the chat, including a saved answer.
It copies the summary, details, and any question as original Markdown, preserving
code fences, links, and YAML indentation. Images, activity logs, and separate
configuration, backup, and verification cards are not included.

The browser first tries its Clipboard API, then selection-based copying for
environments such as local HTTP. If both are unavailable or denied, the response
opens in a read-only dialog with all text selected. Use Ctrl+C / Cmd+C or the
browser's Copy action. **Select all** selects it again on touch devices. A
**Copied!** confirmation appears only after automatic copying succeeds.

## Dashboard browser verification

**Codex can open your dashboards in a real browser and inspect desktop and mobile
screenshots while working on a change.** It can review the layout and revise its
work. Screenshots and findings appear with the conversation, including missing
cards, failed resources, and browser errors.

For example, ask: **"Inspect `/lovelace/home` on desktop and mobile, show me
screenshots, and report any layout or card errors."** Replace the path with your
dashboard view. Saved storage dashboards also get automatic captures of the first
views that fit the task's remaining time. Request other views and YAML dashboards
explicitly. Automatic captures after the answer are labeled separately from
screenshots Codex inspected during the task.

Browser sign-in uses a temporary, local, read-only identity, without a Home
Assistant password or manually created token. Tasks can also fetch fresh entity
states, read Core logs, and check configuration. See [verification tools,
authentication, screenshots, and limits](VERIFICATION.md).

Dashboard captures load integrations' registered static assets and supported
public HTTPS scripts, styles, fonts, and images. Update **both** the app and
integration together, then restart Home Assistant, to use the current resource
and permission support. Repeated findings are grouped by cause and viewport.
The browser blocks service calls and writes. See
[resource compatibility and privacy](VERIFICATION.md#dashboard-resources).

With the app and integration at **0.1.79** or newer, inspections also collect
fresh screenshots from matching Fully Kiosk devices. See
[Fully Kiosk screenshots](VERIFICATION.md#fully-kiosk-screenshots) for requirements
and matching rules.

**Enable built-in browser** in the app's **Configuration** tab controls dashboard
inspection and screenshots. The browser uses RAM while it runs. Turn this off
to completely disable browser launches if you do not need them or have limited
RAM; state and configuration checks remain available.

From 0.1.60, **Browser memory limit (MiB)** in the same tab
controls the memory budget for dashboard captures. The default is **1536 MiB
(1.5 GiB)**, with a range of **512 to 8192 MiB**. For larger dashboards, increase
it to values such as 2048 or 3072 if the host has enough free memory alongside
Home Assistant and other apps. This is a capture limit, not reserved RAM or a
limit on the whole app. Save the options and restart the app if Home Assistant
prompts you; the next capture uses the new value.

## Historical data and statistics

The separate **Home Assistant data** MCP server lets Codex read state history and recorder statistics. It is available automatically in `workspace-write` and `read-only`. It does not ask for shell network approval or require the `HA_TOKEN` option. The worker makes these fixed, read-only API requests with its Supervisor credential, which is never sent to Codex. No extra MCP setup is needed.

Ask, for example: “Compare my kitchen temperature, humidity and extractor fan history yesterday and suggest activation thresholds. Only analyze the measurements.” Codex finds the relevant entity IDs in the configuration, queries the data, and explains its analysis. These tools do not change configuration, call services, or run automations. Verification remains a separate tool.

Two tools are available under `home_assistant_data`:

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

Limits: up to 16 distinct IDs; seven days for raw history and 366 days for statistics; 64 queries per exchange, with one in progress at a time. `max_points` defaults to 1,000, must be at least the number of requested IDs, and accepts up to 5,000. Points are shared fairly across requested IDs, with unused shares and integer remainders redistributed. Each series in the file reports `available_points`, `returned_points`, and `truncated`. A truncated series contains only its first points and cannot represent the whole window. Query a smaller window, fewer IDs, or coarser statistics before analyzing a complete period. Responses from Home Assistant are limited to 1 MiB, each saved file to 4 MiB, and temporary files to 32 MiB per exchange. Receipts are limited to 8 KiB. Large requests fail with guidance to narrow the query; a full storage budget requires a new exchange. Statistics queries are also rejected before reaching Core when their expected bucket count exceeds 10,000. Files avoid the model's output limit; bounded queries still protect Home Assistant, memory and disk use. Longer analyses can use several smaller time windows, calculating compact summaries from each file.

`no_statistics` means the ID has no recorder statistics metadata; `no_data` means no points were returned for the period. Empty history can mean an entity is unrecorded, unavailable to the API, or its history has been purged. It does not establish a constant state. Home Assistant's recorder settings determine retention and which sensors provide statistics; these tools cannot recover purged measurements or add statistics to an unsupported entity.

Each exchange has a separate temporary data credential, revoked on completion and rejected on cancellation, restart, or turn replacement. The data queries do not consume the verification budget or appear as passed verification checks. Configured credentials are redacted before the data is saved. Receipts and any measurements printed during analysis are part of the conversation like other tool results; the full file is not automatically sent to the model. Sensor values are untrusted data, never instructions. The tools accept no arbitrary URLs, API paths, WebSocket commands, SQL, database files, or output paths.

API references: [Home Assistant REST history](https://developers.home-assistant.io/docs/api/rest/#get-apihistoryperiodtimestamp) and the [Core recorder WebSocket command definitions](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/recorder/websocket_api.py).

## Local Home Assistant documentation

From **0.1.70**, the worker keeps a copy of the official Home Assistant
documentation in private app storage. The task prompt instructs Codex to search
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

Prebuilt images are published to `ghcr.io/fatherdoctor/codex-cli-worker` for `amd64` and `aarch64`.

## Security Model

The web UI is intended to be opened only through Home Assistant Ingress. The app does not publish its HTTP port to the LAN by default, and the Flask server rejects spoofed Ingress requests unless they come from the Home Assistant Ingress proxy address.

Programmatic API calls still require the internal worker API token unless they are proxied through authenticated Home Assistant Ingress. The Home Assistant Codex integration discovers the internal app hostname automatically through Supervisor metadata and provisions the worker token through Supervisor-managed app stdin.

The app does not request host networking, Docker API access, full access, host PID/UTS access, privileged kernel capabilities, or elevated Supervisor roles. It keeps AppArmor enabled and ships a custom `apparmor.txt` profile. The `/config` mount is intentionally read-write because editing Home Assistant configuration is the core purpose of the app.

For sandboxed modes, Codex uses Bubblewrap user, mount, PID, and network namespaces. Only the dedicated Bubblewrap setup binary enters a nested AppArmor profile that permits namespace setup. A wrapper forces Bubblewrap to drop all Linux capabilities before the generated command starts. On hosts that deny mounting a fresh `/proc`, the bundled wrapper normalizes a narrowly recognized probe error so Codex can select its own no-proc fallback. The worker then checks that a real sandboxed command succeeds before allowing tasks to start. The fallback retains the other sandbox restrictions but can expose process metadata from the app container; it does not grant host PID access. See [the workaround and host verification steps](SANDBOX_TESTING.md).

## API Token

The API token protects the worker HTTP API. The app stores it in private app storage. The Home Assistant `Codex` integration provisions and rotates it automatically through Supervisor-managed app stdin. You do not need to view, copy, or configure this token.

The token is not your OpenAI or ChatGPT credential. Codex authentication is still handled separately with `codex login`.

## Integration check

The app's web UI works without the **Codex** integration, but some features need it. From **0.1.71**, the web UI shows a line at the top of the chat, above the messages, in three cases:

- **Not installed:** there is no `/config/custom_components/codex_cli/manifest.json`. See [Installation](https://github.com/fatherdoctor/ha_codex#installation).
- **Out of date:** the version in that file is below the app's general minimum integration version. The line names the version to update to. Restart Home Assistant after updating.
- **Installed but not connected:** the files are there, but the integration has not called the app for two minutes. It normally calls every 30 seconds. Restart Home Assistant if you have not done so since installing the integration, then add **Codex** under **Settings** > **Devices & services**.

With integration **0.1.71** or newer, status polling keeps running even if all its entities are disabled. If you turn off polling in the integration's system options, the app can show "not connected" while the integration is still loaded. Turn polling back on for this check.

The line cannot be dismissed. It goes away by itself, within a minute, once the integration is installed, up to date, and connected. During the first two minutes after the app starts, a missing call is not reported, because the integration may still be reconnecting.

The version is read from the installed files. After an update of the integration, Home Assistant keeps running the previous version until it restarts, and the app cannot see that.

This is a general connection check, not a feature-by-feature compatibility check. Its minimum is currently **0.1.69**; newer features, including browser permissions and Fully Kiosk screenshots, need newer integration versions. Update the app and integration together even if no warning is shown.

API: `GET /status` has `integration` with `state` (`ok`, `starting`, `not_installed`, `outdated`, or `not_connected`), `version` (the installed version, empty when the integration is not installed or its version cannot be read), and `minimum_version`.

## Model

`codex_model` is a dropdown. The default value, `default`, lets the bundled Codex CLI choose a model for your account. Explicit choices are `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6.1-terra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`. Availability depends on your account. The app bundles the packaged Codex CLI **0.166.0** (pinned by version and checksum; it never updates itself).

`model_pin` (default `gpt-6.1-sol`) is the only model the app runs. With the pin set, a run on `default` names the pinned model to the CLI, the chat's model picker offers only that model, and a chat, saved conversation or `codex_model` choice that asks for another model is stopped with an error before Codex starts, never run on a different model. Set `model_pin` to `none` to turn the pin off and use `codex_model` as described here.

The worker treats the legacy `gpt-5.3-codex` value as `default`. The retired `gpt-5.5` choice stays in the app's option list for existing installations and runs `gpt-5.6-sol`; it is not offered in the chat's model picker.

`model_reasoning_effort` controls how much reasoning Codex asks supported models to use for each task. The app passes it for each exchange, through the app-server turn. Available values are:

- `minimal`: legacy value, normalized to `medium`.
- `low`: lighter reasoning.
- `medium`: balanced default.
- `high`: more reasoning, usually slower and more quota-intensive.
- `xhigh`: extra reasoning where the selected model supports it.

The app configuration offers `low`, `medium`, `high`, and `xhigh` for all listed models. The chat also offers Max and Ultra where supported; see [Per-conversation model and reasoning](#per-conversation-model-and-reasoning). The worker maps `minimal` to `medium`, including when the model is `default`. See the [Codex models guide](https://learn.chatgpt.com/docs/models) for current availability.

`reasoning_summary` controls the reasoning summaries shown in the activity list under your latest message. It is passed with each app-server turn. Summaries must be requested for them to appear, so the default is `concise`. This setting changes the reported summaries, not the selected reasoning effort.

- `concise`: short headlines such as "Checking the automation". This is the default.
- `detailed`: asks for fuller summaries; current models still mostly return headlines.
- `none`: no reasoning in the activity list. Commands, file edits, searches, progress notes, and tool calls are still shown.

There is no chat UI control for this setting; change it in the app configuration.

## Validation

After every task the worker compares the `/config` tree with the list of files it recorded before the run, then validates what changed in two passes. This comparison also includes files written by Home Assistant or another editor during the task.

1. **Syntax.** Added and changed YAML files are parsed with Home Assistant's YAML loader, and JSON and `.storage` files are parsed as JSON. Cookie jars that integrations keep under `.storage` in the LWP text format are checked as cookie jars instead, without rewriting them: Home Assistant's iCloud jars, which are the files under `.storage/icloud/` ending in `.cookiejar`, and any other `.storage` file that starts with the `#LWP-Cookies-` header, as the jars of custom integrations such as iCloud3 do. Malformed cookie jars are reported without including their contents in errors. Any parse error fails the task.
2. **Home Assistant's configuration check.** From **0.1.56**, when YAML files outside `.storage` were added, changed, or deleted and the syntax pass succeeded, the worker calls `POST /api/config/core/check_config`, the same check as Tools. An `invalid` result fails the task with Home Assistant's error text. The check needs the Supervisor's Core API access the app already has, and can take some seconds on large configurations; the worker waits up to three minutes. If the check cannot run, because Core is restarting or the API is unreachable, the task still completes and its details say the change is applied but unverified. Set `config_check` to `false` to skip this pass.

When validation fails, the task details list the saved previous version of each affected file under `<task_root>/<task_id>/turns/<turn_id>/backups/`, so a broken edit can be restored by copying the file back. Files the task created have no previous version and are listed separately, and so are affected files that have no saved copy. See [Backups](#backups) for where the copies come from and how long they are kept.

Dashboard auto-save skips storage files that failed validation and reports the skip in `lovelace_results`. A YAML failure does not block saving an unrelated, valid dashboard file.

The chat shows the outcome under the answer: a passing check as a short confirmation, a failing check with Home Assistant's error, and an unavailable check as a note. `GET /tasks/<task_id>`, `codex_cli.get_task`, and the `codex_cli_task_result` event carry `config_check` with `result` (`valid`, `invalid`, `unavailable`, `skipped` when no YAML changed or syntax already failed, or `disabled`), `errors`, and `warnings`, and `recovery_files` with `path` and `copy` per affected file. An entry without a copy has a `reason` of `no_backup` or `excluded_credentials`, unless the task created the file. A configuration check does not prove that an automation behaves as intended; it only confirms Home Assistant would load the configuration.

## Backups

From **0.1.63**, the worker no longer archives the whole configuration folder before every message. On a large configuration that archive took longer than Codex needed to start answering, and one archive was kept per message forever.

**Per-file copies (default).** The task prompt tells Codex to copy each existing file to `<task_root>/<task_id>/turns/<turn_id>/backups/<path under /config>` before it changes, moves, or deletes it. Only the files Codex is about to change are copied. When the run ends the worker reviews those copies:

- A copy counts only when it is identical to the file as the worker saw it before the run started. A copy made after the first edit is reported as not saved.
- The review covers the files that are Codex's doing: files it reported editing, files it copied, changed YAML files, and changed dashboard storage files. Home Assistant rewrites its own `.storage` files all the time, so other changed files are not reviewed.
- The chat lists the files with a saved previous version, and the task details name any reviewed file without one. `GET /tasks/<task_id>`, `codex_cli.get_task`, and the `codex_cli_task_result` event carry `backups` with `path`, `status` (`saved`, `missing`, `unverified` for a copy that is not the earlier version, or `excluded`), and `copy`.
- Credential files (`secrets.yaml`, the auth stores, and `.storage/core.config_entries`) are never copied. The worker deletes such a copy if Codex makes one.

Codex needs to be able to write to the backup folder. In `workspace-write` mode that requires `task_root` to be inside `/config`, which is the default. In `read-only` mode Codex is given no backup folder; any explicitly approved writes still need a separate recovery plan. A per-file copy depends on Codex following the instruction; if a file was changed without one, restore it from a Home Assistant backup.

**Full snapshot (optional).** Turn on **Full snapshot before every message** (`full_snapshot`) to archive tracked files, excluding known credential files, to `snapshot-before.tar.gz` in the exchange's folder before Codex starts. The worker can then recover missing per-file copies from that archive when a matching file is available. The exclusions below still apply, so this is not a complete Home Assistant backup. Creating the archive adds a wait before every message that grows with the amount of tracked data.

**Cleanup.** **Keep backups for (days)** (`backup_retention_days`, default 7, range 1 to 365) sets how long backups are kept after an exchange ends. This covers per-file copies, full snapshots, and recovery copies made by earlier versions. Cleanup runs when the app starts, after every exchange, and once an hour; it skips the exchange that is running and leaves messages, logs, and attachments in place. Archives left by earlier versions are removed the same way once they are older than the setting. The chat notes when the saved copies of an exchange have been removed.

**Change detection.** The list of tracked files recorded before a run holds each file's size, modification time, and SHA-256 hash. Hashes can be reused when the size and timestamps are unchanged and the file was already at rest before the previous scan. Recently changed files are read again. The exchange's baseline file is deleted when the exchange ends; detected changes stay recorded in `changes.json`.

**Coverage and exclusions.** Change detection and snapshots skip symbolic links, files larger than **8 MiB**, the worker's task folder, recorder databases, and excluded folders such as `www`, `media`, `downloads`, `deps`, and caches. Selected file types, including logs, common images, audio/video files, fonts, pickle files, and `.old` files, are also excluded. See `EXCLUDED_PARTS`, `EXCLUDED_SUFFIXES`, and `should_include_file` in `server.py` for the exact list. These skipped files do not receive the worker's automatic change validation. Known credential files that are otherwise tracked remain in change detection but are excluded from backups and snapshots. Keep Home Assistant backups for recovery beyond this coverage.

## Sandbox

- `read-only`: Codex can inspect files. Writes and shell networking require permission for the specific action.
- `workspace-write`: Codex can edit the mounted `/config` workspace. Shell networking and writes outside the workspace require permission. This remains the default setting.

This fork has no `danger-full-access` mode. The upstream app's full-access mode turned the permission requests off; here the protected policy (below) refuses it, and an older setting that still names it stops the app at startup with a log line saying so.

**Protected policy.** Allowed sandbox modes and approval policy come from `/usr/share/ha_codex/policy.json` inside the app image, outside `/config` and sandbox writable roots. Options and chats cannot loosen that policy: unsupported modes stop startup and each task. The gate hook, refused domains, private-file masks, Supervisor-token protection and self-edit refusals remain enforced. No house rules are bundled or copied into Codex's home. Codex uses its defaults and the owner's `/config/AGENTS.md`, whose existing editor and reading path remain available. Updates remove only a byte-identical legacy bundled home copy; edited owner files and symlinks are preserved. Owners who want house practices can use the Whole Home Agent starter kit (published separately) and choose their own instructions. Native approval remains specific to the displayed request; read each action before allowing it.

**Who can answer.** Only a signed-in Home Assistant administrator can answer **Allow once** or **Deny**, on the Codex page. The worker checks the user Home Assistant names for the page against its user list and records who answered on the request. The integration's API token cannot answer permission requests, and the integration's actions refuse callers who are not administrators. An automation or script that runs on its own may call the actions; one that a user who is not an administrator starts carries that user, and its call is refused too.

Commands that write to or search protected places, such as `/etc/codex`, lose **Allow once**. That check reads only the command text: it cannot see a path built while the command runs, a symlink, or a relative path from an unknown folder. Such a command keeps **Allow once**, so read it before you allow it.

**Prompt counts.** Each conversation keeps a count of the permission requests it raised, by kind (`command`, `command_input`, `file_change`, `other`). The integration's **Permission prompts** sensor shows the latest conversation's total, with the counts by kind as attributes.

The app still provides safeguards: instructions to save per-file backups, optional full snapshots, checks of changed YAML and JSON, Home Assistant configuration checks, and built-in verification of states and dashboards. These help catch mistakes and recover changes; they do not guarantee that every action is safe or reversible. Keep normal Home Assistant backups too.

The worker exposes the Codex version and a structured sandbox preflight in its authenticated `/health` response. Sandboxed tasks fail with an actionable error before launch if namespace isolation is unavailable.

### Permission requests

In `workspace-write` and `read-only`, the worker uses Codex's live approval protocol with the approval policy `on-request`. When Codex requests access for a command or file change, the chat shows **Permission needed**, the reason, the command and working folder or the file paths, rename destinations, and diff, and **Allow once** and **Deny** buttons. Review the action, then choose a button. Typing a reply or answering a follow-up question does not grant sandbox access. Open the app chat to answer permission requests, including tasks started by the integration or a notification.

When a request starts waiting, the worker sends a **Codex needs permission** notification through `notify_service`, or a persistent notification when none is set. It carries the reason Codex gave and the task, not the command or the diff, and has no buttons: the decision is made in the chat, where the complete action is shown. A notification that Home Assistant has not received by the time the request is answered or expires is not sent, so it cannot replace the notification about the task's result.

**Allow once** answers only that pending request. It does not change the configured sandbox, create a permanent command rule, or grant access to later requests. An approved command can run outside the sandbox with the app's available file and network access; permission is broader than enabling networking alone. An approved file change applies the displayed edits. Review paths outside `/config` carefully, since the worker's configuration backups and checks do not cover them. In `read-only`, the normal per-file backup instructions are disabled, so consider changing modes for editing work.

**Deny** refuses the action and lets Codex continue, for example by finding another way or explaining the limitation. **Cancel** stops the exchange. Requests expire when the exchange ends, times out, is canceled, loses its CLI connection, or the worker restarts. If a file diff changes while its request is waiting, the exchange stops and the old request expires. The task timeout includes time spent waiting for permission. The stall limit (`task_stall_seconds`, 10 minutes by default) does not count that wait: it stops a task only when Codex sends nothing while no request is waiting, as after a network drop. A command that runs quietly counts as sending nothing: a long build or check that prints no output for the whole limit is stopped too, so raise the limit, or set it to 0, if your tasks run such commands. An unanswered request keeps the worker busy and later messages in the queue. Decisions from another tab refresh automatically; an old or repeated button cannot grant access.

The request and decision remain in the exchange's `approvals` history, with sensitive values redacted. An incomplete, oversized, or redacted action can be denied but cannot be allowed, since hidden text could contain executable instructions. Ask for an action whose command, working folder, paths, and diff can be shown in full. API calls should refer to credentials through environment variables instead of embedding their values in a command. This first version supports individual command and file-change requests; broad turn or session permission grants and permanent policy changes are refused. Input that Codex wants to type into a command already running outside the sandbox is shown and can only be denied; Codex can run the whole action as one command instead. API credentials are separate: an approval does not create a Home Assistant token or expose the Supervisor token to Codex. The built-in verification tool continues to work through the worker without shell network approval.

Authenticated API clients can answer a live request using `POST /tasks/<task_id>/approvals/<approval_id>` with `{"turn_id":"<current_turn_id>","decision":"allow"}` or `"deny"`. Read the redacted `approvals` records from `GET /tasks/<task_id>` first. A stale or already answered request returns HTTP 409. Task summaries include `permission_required` while an exchange is waiting. Permission decisions are made directly on the live exchange and are never queued as chat messages.

### Confirmed actions and planted commands

Codex follows its defaults and the owner's `/config/AGENTS.md`. The integration does not bundle a house-practice file prescribing device categories, API-deletion practices or a writing style. Owners can choose such practices through their own workspace instructions or the starter kit. Permission cards and the enforced gate continue to operate independently of prose instructions.

Credential and permission-policy protections remain always on, including the Supervisor token, process environments,
private sign-in files, secrets and the app's own settings. Normal remote actions follow Codex's native approval flow; explicit full-access requests remain refused.
Planted shell, REST and Python commands, command/platform definitions and templated action or service names stay
checked in the hook, configuration watch and end-of-exchange check. An unchanged call or templated target follows
normal approval; it is not a reason to hash or undo an otherwise ordinary change.

A new unsafe file is removed; a changed file is restored only from a hash-verified earlier copy. Without one, the
check reports “could not check” without failing the exchange; review those files before any reload or restart.
Per-file copies depend on the assistant's backup instructions; `full_snapshot` saves a complete earlier copy.
Confirmed findings stay in `refused_edits`; `unchecked_edits` lists paths with no verified copy. The chat shows
summary and details. Marking the watch reviewed acknowledges findings; it does not relax the always-on checks.


## Remote control from the ChatGPT app

OpenAI's remote control (`codex remote-control`) lets the ChatGPT app on a phone or tablet work on this Home Assistant through OpenAI's relay. The app runs it for you; there is no terminal step.

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
isolation still require the lead's HAOS acceptance check described in
[SANDBOX_TESTING.md](SANDBOX_TESTING.md#remote-control-profile-regression).

**Relay execution (CX-RC-EXEC.f2).** The reproduced unified-exec failure was a `nodev` root bind, not
a missing executable rule. Under the previous outer `--bind / /`, opening `/dev/null` and
`/dev/ptmx` failed with `EACCES`; pipe commands and PTY commands both failed before exec. The
outer relay now applies `--dev /dev` after the root bind and preserves shared memory with
`--bind-try /dev/shm /dev/shm`, exposing standard devices rather than the whole container device
tree. Removing those device setup flags reproduces the native regression. The former `/opt` rules
were redundant with `file,` and have been removed. Tool sandbox labels retain their narrower
`network netlink raw,` rule; the relay alone keeps its parent transport access. Native approvals,
managed requirements and the command check remain in effect. The relay starts in `/config`.

**First run: no hand-made token for reads.** Pair remote control, then ask for entity ids or current state. The `home_assistant_relay.read` tool uses the worker's existing Supervisor access for bounded, observational entity listings and state readback. The relay receives only a temporary read capability; the Supervisor credential stays in the worker. The page shows the discovered Core address and explains the read tool. Capability rotation, relay exit or stopping revokes a pending read; late results are discarded. This tool is separate from a page task's verification/history tools and cannot call services, reload, approve an action or read credential stores.

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
Actual reconnect without a new code remains a lead lab acceptance check.

**First pairing (Codex 0.166).** Turn remote control on, wait for the connected state, then choose
**New pairing code** and type the displayed code into the ChatGPT app. The button calls
`remoteControl/pairing/start` with `manualCode: true` on the running process's own Unix WebSocket.
It uses `manualPairingCode`, not the opaque QR pairing token. A missing, unsupported or failed response
shows an error and clears an earlier code. Turning off or restarting clears the page's code. Codes
are kept only in administrator page state; recent logs, non-administrator Ingress reads,
the integration and `/status` omit them. Start, stop, review and new-code requests all require
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

**Managed policy and read-only fallback (Codex 0.166).** The requirements allow only `read-only` and
`workspace-write`, only `on-request` approvals (never `never`), and only managed hooks. Integration-owned
thread/resume/turn requests for full access fail with a clear error before dispatch. Relay-created threads
skip that Python adapter: a disallowed full-access request can be downgraded to read-only by Codex with a
configuration warning. The pinned
[0.166 configuration loader](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/config/mod.rs#L2358)
uses this permitted fallback; the requirements do not turn it into an RPC error. Settings and the launch log
explain the possible downgrade. When Codex emits the warning on the supervised output, the page and log say
that the request was downgraded. Not every relay thread warning is exposed there: absence of that message
is not evidence that the requested permissions were granted.

**Hook timeout and undo limits.** A synchronous hook that times out can fail open in Codex 0.166: the tool
call may run after the timeout. The hook checks bounded inputs and refuses an uncertain, oversized or
ambiguous YAML patch. Hook-checked source and script files are capped at 512 KiB; larger files are refused before scanning. While remote control is on it reconstructs the whole proposed YAML file, so edits
beneath an existing `shell_command`, `rest_command` or `command_line` key are also checked. The checked-in
fixtures use the captured Bash/apply_patch command-string inputs. PermissionRequest is checked for named security
restrictions; needing network or other ordinary sandbox permission is not itself a refusal. In the outside executor, 30 isolated
hook-process measurements took at most 0.412 seconds against the configured 10-second timeout, including
a repetitive 450 KiB YAML file (Codex 0.166 shapes, Python 3.12). Reproduce with
`python codex-cli-worker/tests/measure_gate.py` in the worker's test environment; this timing is not an HAOS
latency guarantee.

The config watch runs every 30 seconds over at most 3,000 ordinary YAML files, each at most 2 MiB. While remote control is on, a new or changed always-on command,
platform or template block causes the **whole affected file** to be restored from its hash-verified earlier
bytes; a newly created file can be removed only with a complete earlier absence manifest. Before either change,
the replaced bytes are saved with mode 0600 under the app's private data folder, in
`remote_control_undo/<sha256>.yaml`. Identical bytes reuse one verified copy, including across files and restarts.
A new copy must fit within 128 copies and 64 MiB, counting earlier nested copies too. When it will not fit,
the watch reports that the store is full and leaves the file unchanged; it does not delete older owner copies.
Each finding names its recovery copy. If the copy cannot
be saved safely, no undo takes place. Home Assistant's root UI-owned `automations.yaml`, `scripts.yaml` and `scenes.yaml`
are reported without automatic undo, preventing whole-file rollback of the owner's UI work. Those basenames
in subfolders are checked normally. Earlier ordinary
YAML copies stay in the app's private storage with mode 0600, capped at 16 MiB; credential files and stores,
symlinks and hardlinks are excluded. Missing, old, corrupt or incomplete baselines never justify a guessed
restore: the page reports that the change could not be undone safely. Successful undo remains visible until
reviewed and across app restarts. Device/message-only findings remain reports under the optional category
setting. With remote control off the watch reports remaining findings without changing files.

Whole-file undo can replace other edits made to the same file since Turn on; recover those edits from the copy named in the finding. Review starts a new baseline; it does not
relax the always-on checks. A watch scan or verified restore cannot reverse a command that already ran,
prevent a reload before the scan, or guarantee capture of an obfuscated reference. Fast hooks and delayed
undo reduce exposure; neither removes the upstream timeout risk.

- **Off until you turn it on.** In the app chat, open **Settings** > **Manage remote connection**, then choose **Turn on**. You can also open the connection view from the connection chip. The choice is saved and holds across app restarts.
- **One supervised process.** The app runs `codex app-server --listen unix://<private socket> --remote-control` in the foreground and starts it again, with a growing delay, if it exits. **Turn off** stops it.
- **Machine name.** The ChatGPT app lists this machine as **Home Assistant** unless you change the **Remote control machine name** option. Codex has no name setting, so the app starts it with that name as its host name inside a private namespace. If the private name namespace is unavailable, the page explains that Codex is using its existing host name.
- **Pairing.** The manual code is shown as large text. A QR for the app link appears only when an app-link code is available; otherwise type the manual code or request a new code. Pairing is kept in the app's private storage with the Codex sign-in, so a restart does not ask for it again. **New pairing code** asks Codex for a fresh one when the ChatGPT app has lost this machine.
- **Automatic Core address and first-run reads.** Relay commands receive the discovered or explicitly overridden `HA_URL`; an owner-configured `HA_TOKEN` remains optional. The separate observational read tool needs no hand-made token. Relay processes never receive the Supervisor token or a chat turn's private MCP capabilities. Never print credentials; approval does not create them.
- **The command check.** Commands from the ChatGPT app do not pass through this app's Allow and Deny buttons, so the app ships a check that holds outside the model, for every client:
  - Codex's managed requirements (`/etc/codex/requirements.toml`, read-only) rule out the full-access sandbox and any hook other than the app's own.
  - With remote control on, the managed hook leaves normal light, lock, network and checked-file requests to Codex's native approval flow. It says nothing (exit 0), never auto-approves. Credential access, explicit full-access requests, the app's own settings/policy/rules and planted command or template definitions remain refused with a named reason. There are no device-category refusals.
  - A config watch compares YAML with the baseline from when remote control was turned on. While remote control is on, it undoes new planted commands and templated actions using verified earlier copies, saves replaced bytes privately, and retains undo results until review. Review its findings in Settings before reloading or restarting.
  - **What the scan reads.** The hook, exchange check and config watch share a line-based scan. It can miss flow-style structures such as `{shell_command: {x: ...}}`, quoted keys such as `"shell_command":`, actions inside flow lists, a script named only in a flow-style `data: {entity_id: ...}` (the block form is checked), and edits to definitions in files brought in by `!include` or `!include_dir_named`. An unquoted `shell_command:` at the start of a line is checked even when its value is inline. The sandbox, owner instructions and normal approval flow still apply; the owner must review changes.
  - **Python definitions.** The always-on definition checks include `python_script:` and `pyscript:`. A `python_script:` definition enables scripts that run when called; it does not run them on load. Its finding in Settings keeps the older wording "runs on its own once loaded" so findings saved before an update still match. The YAML scan does not read `.py` files; while remote control is on, a request that names a path under `python_scripts/` or `pyscript/` is refused.
  - The phone uses Codex's own approval card, not this page's buttons: review what will change, where and the effect, then allow or deny that request. A hook no-opinion result leaves the native request waiting; it never grants permission. Actual phone presentation still needs the owner's phone test.
- **Why Turn on can be unavailable.** Missing command-check files or a failed self-test disable **Turn on** with a reason; the chat page remains available.
- **Command checks.** Remote control requires the installed gate and its self-test. Normal remote requests follow Codex's native approval flow, never an automatic approval from the hook. The self-test checks a normal PermissionRequest with no hook decision and a credential request that must be refused. Explicit full-access requests, credential access, changes to the app's settings/policy/rules and planted command definitions remain refused. Templated action names stay checked; installation-specific limits belong in `/config/AGENTS.md`. The configuration watch retains undo results and unreviewed changes before a reload or restart.
- Remote control is experimental upstream. If it breaks, the chat page is not affected.

## Codex Sign-In

Choose **Settings > Sign in** in the app web UI or call the Home Assistant action `codex_cli.start_login` to start `codex login --device-auth`.

Before starting sign-in, enable **Enable device code authorization for Codex** in ChatGPT: open the ChatGPT website, click your profile, open **Settings** -> **Security**, and turn on the toggle near the bottom of the page. This setting is not in the Codex website.

For a managed workspace, an administrator may need to enable device-code login in the workspace permissions. See [Codex authentication](https://learn.chatgpt.com/docs/auth#preferred-device-code-authentication-beta).

The app posts a Home Assistant persistent notification containing a QR code, link, and one-time device code. The QR code opens the OpenAI device page; type the code from the same notification into that page. The app refreshes the notification when the code appears and detects completion automatically. You do not need to run `docker exec`.

While the app is not signed in, a banner above the chat offers **Sign in**. During a sign-in, the banner and **Settings** show the one-time code in large letters, a QR code of the sign-in page served by the app itself, and the link. The code stays on screen, unchanged, until the sign-in completes or you press **Cancel**; pressing **Sign in** again keeps the running sign-in and its code. When a saved sign-in expires or is revoked, the banner returns and the integration raises a Home Assistant repair; the integration's **Signed in** binary sensor turns off.

When opened through Home Assistant Ingress, the app web UI can start the login flow without entering the worker API token because Home Assistant already authenticated the session. Direct HTTP/API calls still require the worker API token.

Use **Settings > Sign out** in the app web UI or the Home Assistant action `codex_cli.logout` to run `codex logout` and remove saved Codex CLI credentials from the worker. Logout is blocked while a Codex task is actively running.

The app uses the built-in Supervisor token for Home Assistant notifications and dashboard saves through the Home Assistant Core API proxy. No Home Assistant long-lived access token is required.

Sign-in uses your ChatGPT account and its Codex allowance. Availability and usage limits depend on your plan and workspace permissions. The worker does not provide an OpenAI API-key option for Codex tasks.

## Pausing the assistant

**Settings > Pause the assistant** stops the app from taking new messages: new chats and replies are refused, and queued messages wait. A banner above the chat says so and offers **Resume the assistant**, which also starts the oldest queued message. A running task is not stopped; use **Stop task** for that. The pause is kept across app restarts.

## AGENTS.md and HA_TOKEN

The app web UI includes an editor for `/config/AGENTS.md` under **Settings** > **Advanced** > **Workspace rules**. This file remains the source of truth for shared Codex project instructions.

The optional `HA_TOKEN` app option is passed to Codex subprocesses as the `HA_TOKEN` environment variable. Configure it only if you want Codex tasks to call Home Assistant APIs directly. A Home Assistant long-lived access token inherits its user's permissions; use a dedicated user with permissions appropriate for the intended work.

From **0.1.68**, a task that has `HA_TOKEN` also gets `HA_URL`, the address those calls go to, and the worker's instructions tell Codex to use the two together. The app's own calls to Home Assistant keep using the Supervisor proxy with the app's own token and do not use this option.

From **0.1.81.2**, the worker discovers Core's port and SSL flag from Supervisor `/core/info` at startup and each task or relay launch. `ha_url` is now an empty-by-default **Core API URL override**. Leave it empty for discovery; an explicit HTTP(S) override wins. Saved values equal to the former direct default or former Supervisor proxy defaults are treated as unset. Overrides cannot contain credentials, a query or a fragment. Discovery requests use the worker's Supervisor credential only at the fixed internal endpoint, without proxy inheritance or redirects, with bounded timeouts and response size.

The manifest grants `hassio_api: true` with the default Supervisor role and no `hassio_role` override. The default role admits read-only `/info` endpoints such as `/core/info`; the grant exists only to discover Core's port and SSL setting. The Supervisor credential stays in the worker. Each relay and task launch retries a failed metadata read once, then probes the two fixed internal Core API ports in order without any credential. Only a verified API response supplies a fallback address; the log records which source and fallback port were used. Failed probes leave discovery unavailable.

With SSL, set the Home Assistant API URL override to the configured certificate hostname and port. Never disable TLS verification or use insecure client flags to bypass a hostname or certificate error. Discovery does not turn verification off.

`HA_URL` is passed to Codex even without `HA_TOKEN`. If discovery is unavailable or malformed and there is no valid override, the value stays unavailable and is omitted from the environment; neither the worker nor the instructions invent a port. Settings reports that state. A later launch refreshes discovery. The app's own authenticated API calls continue through its Supervisor Core proxy, which does not accept an owner Core token. None of these changes grants shell networking: an unsupported direct API read needs native approval for that command and a separately configured credential.

These calls come from Codex's shell. In `workspace-write` and `read-only`, Codex must request permission for the specific command that needs networking. What Codex can do with the token is whatever the token's user may do, including reloading configuration and restarting Home Assistant.

## SSH connections

I include the OpenSSH client so Codex can use an existing SSH server, such as a Home Assistant SSH app, a NAS, or another Linux machine. There are no SSH app options or connection-management screens. Set up each connection through the existing chat and standard SSH files. No `HA_TOKEN` is needed for SSH authentication, and SSH does not mount remote folders inside the worker.

### Set up a connection through chat

Ask Codex, for example:

> Set up a connection named `ha-terminal` to my SSH server at `ssh-host.example`, port `22`, username `hassio`. Generate a dedicated Ed25519 connection key under `/data/.ssh`. Keep the private key out of chat and logs, show me only its public key, and preserve any existing connections. Use `/data/.ssh/config` and `/data/.ssh/known_hosts` explicitly. Wait for me to verify the server fingerprint before trusting it. Save the connection name and invocation in `/config/AGENTS.md`, without credentials.

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

`/data/.ssh` is private to this worker app and persists across restarts and updates, but Codex tasks can read it. Never print private keys or paste them into chat, task prompts, logs, or `/config`. The app excludes the entire `.ssh` directory from Home Assistant app backups, including keys, connection configuration, and trusted host keys. After restoring a backup, set up connections again and revoke replaced public keys on the remote servers. These exclusions do not remove credentials already copied elsewhere or included in older backups.

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

Leave `notify_service` unset or empty to use Home Assistant persistent notifications for task completion, failures, questions, and permission requests. If you want push notifications, set it to a Home Assistant notify service such as `notify.mobile_app_your_phone`.

From **0.1.69**, a question sent to a mobile app notify service has a button for each answer Codex offers. See [Human Input](#human-input).

Home Assistant app configuration schemas do not currently provide a Home Assistant service autocomplete selector, so this option remains a plain text field.

## Human Input

Tasks run without an interactive terminal. If Codex needs a decision, it returns `needs_input` with a question. The task is marked `waiting_for_input`, and your answer continues it as the next message. Sandbox permission requests use the separate [approval flow](#permission-requests) during the running exchange.

From **0.1.69**, Codex can offer up to three short answers with its question, for example **Go ahead** and **Don't change anything**. When it asks whether to go ahead with a change, it is told to say in the summary exactly what it would change.

- **In the chat**, the choices are buttons under the question. Picking one sends it as your next message. You can still type a different answer in the message box, and a draft there stays as it is. Once the question is answered the buttons are gone, and the question stays in the history.
- **In a notification**, a mobile app notify service (`notify.mobile_app_...`) shows a button for each choice, and tapping one sends that answer. The **Codex** integration receives the tap, so it has to be installed and at **0.1.69** or later. Any other notify service, and the persistent notification, lists the choices in the text instead.
- **In automations**, the `codex_cli_task_result` event carries `choices` (a list, empty when Codex offered none) and `turn_id`, and the **Last task** sensor has the same two attributes. Answer with `codex_cli.reply_task`. Its reply is free text, so to pick a choice, send that choice's text.

An answer goes to the question it was given for. `codex_cli.reply_task` accepts an optional `turn_id`. When it is set, the worker takes the reply only while that turn's question is the one waiting, and refuses it otherwise. The buttons in the chat and in notifications always send it, so a button on an old notification cannot answer a newer question in the same chat. A tap the worker refuses shows a Home Assistant notification that says why. A reply without `turn_id` works as before.

If another chat is working when you pick a choice, the answer waits in the [queue](#queueing-messages-while-another-chat-works) and is sent when that chat finishes. Until then the chat takes no other message, and the answer is sent only if its question is still the one waiting.

The choices are a convenience and not a safeguard. Codex decides when to ask, and nothing stops it from changing files without asking. Use `/config/AGENTS.md` to tell it when you want to be asked first.

API: `POST /tasks/<task_id>/reply` and `POST /tasks/<task_id>/continue` accept `turn_id`, and `choice` in place of the text: the position of one of that turn's choices, counted from 0. `choice` needs `turn_id`, because a position means nothing without its question. A `turn_id` that is not the waiting turn returns HTTP 409. A `choice` without `turn_id`, or one the question does not offer, returns HTTP 400. Each turn in `GET /tasks/<task_id>` has `choices`, and `latest_task` in `GET /status` has `choices` and `current_turn_id`. The action of a notification button is `CODEX_CLI_CHOICE_<position>_<turn_id>_<task_id>`, which Home Assistant delivers in the `mobile_app_notification_action` event.

## Saved conversations

The web UI lists recent chats in a resizable sidebar and opens their messages on the right. On mobile, the sidebar becomes a drawer. Select a saved chat and send a message to continue, or choose **New chat** for a fresh session. Settings contains account controls and workspace instructions.

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

The Markdown parser, [marked](https://github.com/markedjs/marked) 18.0.14, is bundled with the app under `web/assets/vendor/` with its MIT license, so the chat does not load scripts from another server. Codex decides how it writes its answers; to ask for Markdown, add an instruction to `/config/AGENTS.md` as described under [Task Output](../docs/upstream-README.md#task-output).

### Queueing messages while another chat works

From **0.1.69**, you can send the next request without waiting for a working chat to finish. The worker still runs one task at a time, because two runs could edit the same files, reload automations, or restart Home Assistant under each other. While a chat is working, a message sent in a new chat or in another saved chat is placed in a queue instead of being refused. The note above the message box says so before you send, and the send button has a dashed outline.

Queued messages are listed under **In queue** at the top of the sidebar, in the order they will start, with their place in line. Opening one shows the message with a dashed outline and an **In queue · not sent yet** label. Until it starts, choose **Edit** to change its text (Enter saves, Escape cancels) or **Remove** to take it out of the queue. Removing the message of a chat that has not started removes that chat. Attached images wait with the message and are removed with it.

Up to 50 messages can wait at once. Each chat can have one waiting message, and the chat that is working cannot queue a follow-up to itself. The model and reasoning pickers are locked for a chat while its message waits. The queue keeps the chat's selected overrides; inherited app defaults are resolved when the message starts.

When the running task ends, whether it completed, failed, was stopped, or needs your reply, the oldest queued message starts on its own. This also happens when the web UI is closed. The queue is stored in `/data/message_queue.json` and continues after a worker restart. A message leaves the stored queue only after its chat or exchange is saved, so if the worker stops at that moment the message is either still waiting or recorded as an interrupted exchange, never lost. If a queued follow-up can no longer start, for example because its saved session was removed, it is recorded as a failed exchange in its chat and the next message starts. Deleting a chat removes its waiting message.

API: `POST /tasks`, `POST /tasks/<task_id>/continue`, and `POST /tasks/<task_id>/reply` accept `"queue": true`. With it, a request made while another task is active returns HTTP 202 with `status: "in_queue"`, `task_id`, `queue_id`, `position`, and `active_task_id` instead of HTTP 409. A request made while messages are still waiting joins the queue behind them in the same way. When nothing is running and the queue is empty, the task starts as usual. Without it the endpoints behave as before, so automations and the Home Assistant actions are unchanged. `GET /queue` and the `queue` list in `GET /tasks` return the waiting messages in order, each with `queue_id`, `task_id`, `new_chat`, `title`, `message`, `chat_settings`, `prompt_attachments`, `position`, `created_at`, and `updated_at`. `POST /queue/<queue_id>` with `{"message": "..."}` replaces the text, and `DELETE /queue/<queue_id>` removes the message. Both return HTTP 404 once the message has started. `GET /tasks/<task_id>` includes `queued_message` for a chat with a waiting message. For a chat that has not started, it returns a placeholder with `status: "in_queue"` and no turns; that chat joins the `tasks` list under the same `task_id` when its message starts. `GET /status` reports `queued_message_count`. A second message for a chat that has one waiting, whether it asks for the queue or not, a full queue, and a settings change for a chat with a waiting message return HTTP 409.

### Per-conversation model and reasoning

From **0.1.48**, the pill below the message box opens a model menu and a reasoning slider. Selecting a value in a saved chat saves it immediately; selections for a new chat are saved with its first message. Both persist across worker restarts. Changes apply to the next message and preserve the saved session. Controls are disabled while that conversation is running.

A chat without a model of its own follows the app's model setting. From **0.1.67**, the pill names that model instead of showing **Default**: the model set in the app options or, when that option is `default`, the model Codex picks for the signed-in account. The worker reads the latter from Codex's own status report when it checks the quota, and shows GPT-6.1 Sol, the bundled CLI's default, until the first report. The menu marks that model, and choosing it makes the chat follow the app setting again. The reasoning reset button inherits the app's reasoning setting. These can be reset independently. New and older chats without saved overrides inherit both defaults. When an exchange starts, it records its resolved `execution_settings`; changing defaults later does not change that running exchange. Queued messages resolve inherited defaults when they start.

With the default pin, the picker offers only the pinned model; set `model_pin` to `none` in the app configuration to use the other choices. With the pin off, model choices match the app model selector, without the retired `gpt-5.5`. Supported reasoning levels follow the bundled CLI 0.166.0 catalog: Low, Medium, High, Extra High, Max and Ultra for GPT-6 Astra, GPT-6.1 Sol, GPT-6.1 Terra, GPT-6 Sol, GPT-5.6 Sol and GPT-5.6 Terra; through Max for GPT-6 Luna and GPT-5.6 Luna. Ultra enables automatic task delegation. With the app model on `default`, the levels are those of the model Codex picks; a model the worker has no entry for gets the common Low through Extra High choices. If switching models makes a saved reasoning choice incompatible, the UI resets reasoning to the app default. An inherited reasoning level that the model does not support falls back to Medium. So does a level saved in a chat that follows the app model, when that model changes to one without the level; the saved level applies again once the model has it. A chat that had GPT-5.5 selected continues on GPT-5.6 Sol, the next model up, and keeps its reasoning level. Model availability still depends on the account; unavailable-model errors are reported by the CLI.

The authenticated worker API exposes `GET /chat-options` for choices and defaults, with `default_model` naming the model a chat without its own selection runs on, and `POST /tasks/<task_id>/settings` with `{"chat_settings": {"model": "gpt-6-astra", "reasoning_effort": "xhigh"}}` to save selections for an idle chat. Each value can be `null` to inherit the add-on default. `POST /tasks`, `/tasks/<task_id>/continue`, and `/tasks/<task_id>/reply` also accept the optional `chat_settings` object. Omitting the object preserves existing selections. Invalid models and reasoning levels, and a level the selected model lacks, return HTTP 400; changes to active chats return HTTP 409. `GET /tasks/<task_id>` includes the saved `chat_settings`. Home Assistant actions continue using their existing parameters and honor settings already saved for the conversation.

### Live activity

From **0.1.53**, the chat shows what Codex is doing while it works on your latest message. Steps appear under the message as Codex reports them: reasoning headlines, progress notes, the commands it runs, the files it edits, web searches, and tool calls. A failed command or an error is highlighted. When a run is stopped or fails, the list ends with a **Stopped** or **Failed** row. The list is expanded while the run lasts and collapses to **Show activity (N steps)** under the answer when it finishes. Only the latest exchange of a chat shows steps; earlier exchanges do not.

From **0.1.63**, the list starts as soon as a message is accepted. The worker reports its own steps alongside those of Codex: recording the state of the configuration files, saving a full snapshot when that option is on, starting Codex, waiting for its first response, and checking the changes afterwards. The heading shows how long the exchange has been running, and its dot pulses for as long as the run lasts.

Command output is not shown by default because it can contain configuration secrets. Each command offers **Show output**, which reveals up to 2,048 characters after the same redaction as the task log and marks output that was cut. File edits list paths only. Reasoning summaries and other activity text are limited to 4,000 characters per field, and at most 500 steps are kept per exchange. The task's `codex.log` retains the underlying redacted event stream.

The worker keeps the steps of the running exchange in memory and writes them to `<task_root>/<task_id>/turns/<turn_id>/activity.json` once when the run ends, so the latest exchange's steps survive a page reload and a worker restart. The authenticated worker API exposes `GET /tasks/<task_id>/activity?after=<seq>`, which returns `turn_id`, `running`, `seq`, `total`, the `steps` whose sequence number is above `after`, and `elapsed_ms` while the exchange runs. A step that changes, such as a command that finishes, is returned again with a new sequence number and the same `index`. The web UI polls this endpoint once a second while a chat is working and pauses when the tab is hidden. The Home Assistant integration does not use it.

### Generated images

From **0.1.49**, images that Codex generates during a conversation appear in the chat under the response that produced them. Click an image to open it at full size, or use **Download** to save it. Images stay with their exchange across reloads, worker restarts, and continued conversations.

Image generation uses Codex's built-in `image_gen` tool with the signed-in ChatGPT account. It needs no separate OpenAI API key. Tool availability and usage limits depend on the account. The app currently bundles Codex CLI 0.166.0.

After each run, the worker reads the session's rollout file under `/data/codex-home/sessions` and looks for completed `image_gen.generation` items that were not already present before the exchange. This works for app-server turns, including images absent from the live event stream. Codex saves each result under `/data/codex-home/generated_images/<session_id>/`. The worker copies the file into `<task_root>/<task_id>/turns/<turn_id>/attachments/` and records metadata on that exchange. If the saved file is missing, the worker decodes the inline result from the rollout instead. The original stays where Codex saved it so follow-up edits in the same conversation keep working.

The task prompt tells Codex that generated images are attached automatically and that it should leave them at the default save location unless the user asks for a file at a specific path, for example under `/config/www` for use on a dashboard.

Limits and retention: only PNG, JPEG, GIF, and WebP content is accepted, checked by inspecting the file rather than trusting its name. Files larger than 25 MiB and images beyond the first 12 per exchange are skipped and noted in the task log. Attachments are retained with the task history and are removed when the task directory is deleted; there is no automatic expiry for generated images. The worker serves them through its authenticated attachment API and does not publish them under `/config/www`; Codex can copy an image there if you request it.

API: `GET /tasks/<task_id>/attachments/<attachment_id>` returns the image with its recorded content type and `X-Content-Type-Options: nosniff`. Add `?download=1` for a download disposition. The endpoint requires the worker API token or an authenticated Home Assistant Ingress session. Before sending, it re-checks the stored file's size, SHA-256, and image signature against the recorded metadata, and returns HTTP 404 for unknown, missing, oversized, or altered files. `GET /tasks/<task_id>` and `codex_cli.get_task` include an `attachments` list on each turn and on the task-level result for the latest exchange. Each entry has `attachment_id`, `kind` (`image`), `name`, `mime_type`, `size`, `sha256`, `created_at`, `revised_prompt`, `generation_id`, `path` (relative to the task directory), and `url` (relative to the worker). The `codex_cli_task_result` event includes the same `attachments` list; it is empty for cancelled and failed launches.

If an image does not appear, open the task log and look for `Skipped image generation` or `Could not attach image generation` lines, which explain why the worker did not attach the file.

### Attaching images

From **0.1.51**, you can attach images to a message: a screenshot of a dashboard problem, a photo of a device, or a design mockup. Use the paperclip button next to the model pill, paste an image from the clipboard into the message box, or drop image files onto the composer. Pending images appear above the message box with a remove button and go out with the next message, in a new chat or when continuing a saved one. They then appear with your message in the conversation and stay with it across reloads and worker restarts.

Codex receives each image with the message as a `localImage` input in sandboxed app-server turns. The task prompt also names the attached files. Images become part of the saved session and can contribute to the context of later turns, so the UI resizes images whose longest edge exceeds 2048 pixels before uploading.

In the Home Assistant Android app, the paperclip picks one image at a time; use it again to add more. The app's file chooser returns nothing when a multi-select picker is used, so the web UI asks Android WebViews for a single selection. Paste and drag-and-drop still accept several images at once.

Limits: PNG, JPEG, GIF, and WebP only, checked by inspecting the bytes rather than the file name; at most 6 images per message and 10 MiB per image after resizing. The worker stores attached images under `<task_root>/<task_id>/turns/<turn_id>/attachments/`, serves them through its authenticated API, and removes them with the chat. It does not publish uploads under `/config/www`.

API: `POST /tasks`, `POST /tasks/<task_id>/continue`, and `POST /tasks/<task_id>/reply` accept an optional `attachments` list of `{"name": "shot.png", "data": "<base64>"}` objects; a `data:` URL prefix is tolerated. Invalid entries return HTTP 400 and no task or turn is created, and requests over roughly 81 MiB return HTTP 413. Each turn in `GET /tasks/<task_id>` and `codex_cli.get_task` lists its uploads under `prompt_attachments` with the same fields as generated images plus `origin: "user"`; generated images carry `origin: "codex"`. `GET /tasks/<task_id>/attachments/<attachment_id>` serves both kinds with the same checks. The `attachments` list on the task result and in the `codex_cli_task_result` event contains only images generated by Codex.

### Managing chats

From **0.1.50**, each chat in the sidebar has an actions menu. On desktop, hover over a chat and choose the **⋯** button, or right-click the row. On phones, long-press the chat to open a bottom sheet. With a keyboard, focus a chat and press Shift+F10; arrow keys move between items and Escape closes the menu.

**Pin chat** keeps the conversation at the top of the list under a **Pinned** heading, ahead of the recently active chats; **Unpin chat** returns it to the recent list. **Rename** opens a dialog for a new title, which appears in the sidebar and the conversation header. Neither action changes the chat's `updated_at` time or its saved messages. **Delete** asks for confirmation, then removes the conversation and its saved history. The delete action is unavailable while that chat is running; stop the task first.

Deleting a chat removes `<task_root>/<task_id>/`, including prompts, output, backups, and attachments, and the entry from the task index. The worker also attempts to remove the Codex session rollout file under `/data/codex-home/sessions` and generated images under `/data/codex-home/generated_images/<session_id>/`. Failures to remove those session files are logged. Deletion cannot be undone in the chat UI and does not erase copies in separate Home Assistant backups.

API: `POST /tasks/<task_id>/pin` with `{"pinned": true}` or `{"pinned": false}` returns the saved state. `POST /tasks/<task_id>/title` with `{"title": "Porch lights"}` trims and collapses whitespace, rejects empty titles and titles longer than 200 characters with HTTP 400, and returns the stored title. Both work on running chats and return HTTP 404 for unknown tasks and HTTP 500 if the metadata could not be written, in which case the previous values are kept. `DELETE /tasks/<task_id>` returns HTTP 409 for queued or running tasks, HTTP 404 for unknown tasks, and HTTP 500 if the task files could not be removed, in which case the chat stays listed. Summary entries from `GET /tasks?summary=true` include a `pinned` flag, and `order=pinned_first` lists pinned chats before the others while keeping the most recently updated first within each group. The `codex_cli.list_tasks` action keeps its existing `created_asc` and `updated_desc` orders.

### Continuing and browsing chats

`codex_cli.continue_task` accepts `task_id` and `message` and resumes completed, waiting, failed, or cancelled tasks. The old `reply_task` action remains restricted to tasks waiting for input. Both use the same resume implementation. Continuation requires a saved session under `/data/codex-home/sessions`; the worker does not fall back to a fresh chat if it is missing.

`GET /tasks` and `codex_cli.list_tasks` accept optional `limit` (1–500), `offset`, `status`, `order` (`created_asc` or `updated_desc`; the worker API also accepts `pinned_first`), and `summary` parameters. `summary: true` returns compact entries for the sidebar. Without filters, all tasks are returned in their original creation order. `GET /tasks/<task_id>` includes `turns`, `history_incomplete`, and `can_continue`. The latter indicates an eligible task with a recorded session ID; availability of its session file is checked when continuing. `POST /tasks/<task_id>/continue` accepts a JSON `message`.

Each new exchange stores its user message, timestamps, status, and response in the task's `turns` list. Prompts, final output, backups, and the list of changed files are stored separately under `<task_root>/<task_id>/turns/<turn_id>/`. The task-level result and existing result events continue to describe the latest exchange. Old tasks are adapted from their available prompt, replies, and last result, with a notice that earlier responses may be missing. History is retained until its files are removed; there is no automatic expiry, except for [backups](#backups).

For local browser checks, run `python codex-cli-worker/tests/web_fixture.py` from the repository root, then `node codex-cli-worker/tests/browser_smoke.cjs` with Playwright available. The fixture uses temporary storage and simulated responses, and never launches Codex or contacts Home Assistant. Set `CODEX_CHAT_BROWSER=msedge` to use installed Edge, and optionally `CODEX_CHAT_QA_DIR` to choose a screenshot directory outside the repository.


### ABI and relay fallback acceptance (N8/N9)

The AppArmor ABI declaration enables domain/stack and network_v8 mediation on HAOS; ptrace was already mediated. Both tool labels keep their existing Unix/raw-netlink socket permissions, so sandboxed AF_INET/AF_INET6/packet socket creation is denied. Approved escalations keep the existing parent/relay policy. The lead must run an ordinary chat-page shell read and the history MCP tool after loading the updated HAOS profile; CI's Ubuntu kernel is not that acceptance.

The plain relay route still creates a user namespace and maps to the fixed non-root identity. It omits the private hostname, not the namespace requirement. If namespace creation is refused, it fails closed and retries with backoff. No working fallback is promised in that case. See [sandbox checks](SANDBOX_TESTING.md#abi-socket-family-change-and-namespace-fallback). The scratch-folder proposal is in [CX-HA-URL's design note](../docs/design/CX-HA-URL.md#scratch-folder-proposal-do-not-build); this change creates no notes folder or sandbox bind.

## Scheduled runs and the lead agent channel

In **Settings → Scheduled runs**, save a name, prompt and schedule, using **Default** or **Read only**. Default follows the app's configured sandbox; Read only narrows that mode. **Save and approve schedule** authorizes the exact prompt and timing you submitted. An agent's proposal stays off until an administrator reviews and approves it on this page. An agent can edit or delete only its own unapproved proposals; an approved schedule is owner-only. If a proposal changes while you review it, refresh before approving it. Proposals whose prompt contains masked credentials cannot be approved or prefilled for editing; create a replacement with a complete prompt.

Intervals accept whole minutes or hours, with a minimum of one minute. One-off entry and displayed dates use Home Assistant’s configured IANA time zone, shown above the form. Cron follows that local wall time across daylight-saving changes: missing minutes are skipped and repeated minutes run once, at the first occurrence. One-off nonexistent local times are refused; repeated times use their first occurrence. Entry and scheduled dispatch pause if the Home Assistant time zone is unavailable. Explicit API timestamps with an offset remain absolute instants. Cron uses five numeric local fields: minute, hour, day of month, month and weekday. Lists, ranges and steps are supported. Sunday is 0 or 7. When neither day field starts with a wildcard, either can match. A leading wildcard, including a step such as `*/2`, keeps the other day field restrictive. The next occurrence must be within 366 days. A cron schedule disables after its last occurrence if no next occurrence lies within that horizon; edit it to enable it again. One-off schedules disable after their occurrence is reserved. Pause, resume, edit or delete schedules from the list; deleting a schedule stops future runs, not a task already in progress. Cancel a running task in its chat.

The worker checks due schedules every five seconds. It uses the ordinary task launcher, backups, validation and permission checks, and starts at most one scheduled task per check. Due work waits while the assistant is busy or paused. A missed interval coalesces into one run, without a burst of catch-up tasks. Limits are 100 approved schedules plus 20 unapproved proposals, 24 runs per schedule per UTC day, and 64 scheduled or lead-submitted runs total per UTC day. A run that reaches a limit waits until a later UTC day. Completed run history retains the latest 500 records; active records are kept separately. Schedules, occurrence reservations, run history and mailbox data live in `/data/schedules.sqlite3` and survive app restarts and updates. Reservation and launch share the normal chat-slot lock; a busy launch restores the due occurrence without overwriting a concurrent owner edit. A crash after reservation but before saving the ordinary task records an interrupted run and does not retry it automatically.

**Permission requests remain human decisions.** The run log shows **waiting for approval** while a native request is pending. Open its chat to review the command or diff and choose **Allow once** or **Deny** as an administrator. No scheduler, lead token or mailbox reply can answer it. The parked exchange keeps the worker's normal execution slot; other schedules do not bypass it. Existing task timeouts, cancellation and restart expiration still apply. A question instead shows **waiting for input**. Scheduled and lead tasks report completion, failure, cancellation and questions in the run log/chat without pushes. Only their existing permission-wait notification uses `notify_service` (or persistent Home Assistant notifications). The lead accepted Home Assistant phone notification and the responsive permission page; a ChatGPT approval mirror is not required or provided.

Codex on the worker receives the `codex_scheduling` tools: `propose_schedule`, `list_schedules`, `read_mailbox` and `post_mailbox`. They use a running-turn capability that expires when the exchange ends. Proposal tools cannot approve schedules or grant native permissions. Mailbox and schedule summaries are paginated in groups of 10 by default (`limit` can select 1–20); prompt/message excerpts are limited to 512 characters. Read the complete proposed prompt on the page before approving. These tools are for page tasks; native remote threads do not receive page-task capabilities.

### Connecting the other local agent

An administrator creates or rotates a separate lead token in Settings. It is shown once, cleared when Settings closes, and never saved in browser storage. Copy it directly into the other agent's private configuration. The worker stores only its hash. Rotation immediately revokes the previous token. This token is separate from the integration's worker token and cannot replace a Home Assistant administrator. Never put it in a prompt, URL, mailbox message or repository.

The API is served by the same worker HTTP listener as the existing local API. The other agent uses its configured local worker address and sends `Authorization: Bearer <lead token>`; it must not rely on Ingress headers. Requests use JSON and are limited to 64 KiB. Prompts are limited to 16,000 characters, names to 80, and mailbox messages to 8,000. Responses are not cached and redact recognized credentials. The API does not configure network exposure; keep it on the existing local app network and protect the token as a credential.

| Method and path | Purpose and fields |
|---|---|
| `POST /lead/tasks` | `{"prompt":"Inspect the sample configuration.","mode":"read-only"}` starts an ordinary task and returns `task_id`. A busy or paused worker refuses it; it is not silently queued or retried. The daily unattended-run limit applies. |
| `GET /lead/tasks/<task_id>` | Read a lead-submitted or scheduled task's saved result and pending-approval status. Unrelated human chats return not found. It cannot answer an approval. |
| `GET /lead/schedules` | Read saved schedules and proposals. |
| `POST /lead/schedules` | Propose `{"name":"Sample inspection","prompt":"Inspect the sample configuration.","mode":"read-only","spec":{"kind":"interval","seconds":3600}}`. The result has `approved:false` and `enabled:false`. |
| `PATCH /lead/schedules/<id>` | Replace only a lead-authored, unapproved proposal with those same four fields. Approved schedules and proposals by other authors return 404. Every edit changes the review revision. Other specification shapes are `{"kind":"cron","expression":"0 9 * * 1-5"}` and `{"kind":"once","at":"2027-03-12T09:00:00Z"}`; choose a future time. |
| `DELETE /lead/schedules/<id>` | Delete only a lead-authored, unapproved proposal. |
| `GET /lead/mailbox` | Read local plain-text messages and replies. |
| `POST /lead/mailbox` | Save `{"body":"A local sample message.","turn_id":"unique-lead-turn"}` as a new thread. |
| `POST /lead/mailbox/<message_id>/reply` | Save `{"body":"A local sample reply.","turn_id":"unique-lead-turn"}` on that root thread. |

The page shows the same mailbox and can post or reply. Messages remain plain text, confer no authority and never execute themselves or reach an external recipient. A lead explicitly submits a separate task if it wants work performed. The mailbox retains at most 500 messages, pruning the oldest other thread (including replies) when full. If only the current reply thread fills it, its oldest replies are pruned. Lead and Codex each have limits of 20 writes per turn and 64 per UTC day, returning HTTP 429 for lead writes at the limit. The lead supplies a stable `turn_id`; Codex’s turn comes from its verified exchange. Usage counters survive message clearing and app restarts. The owner is not rate limited. `DELETE /lead/mailbox` clears lead-authored root threads and replies; `DELETE /lead/mailbox/<id>` clears one such thread. Administrator page controls can clear all threads or delete one. Clearing does not execute anything or reset write limits. No lead endpoint creates permanent permissions, changes sandbox policy, approves a schedule or answers a native permission request. Owner management and token rotation require authenticated administrator Ingress, including a fresh check of the administrator identity under the existing cache rules.

Schedule prompts containing recognized credentials are masked in responses and marked `prompt_redacted`; Edit is disabled so masked text cannot replace the saved prompt. Create a replacement with a complete prompt instead. All save routes reject the literal `[redacted]` marker. The private database and its journal/WAL/SHM paths are protected like the worker token from tool file access and native permission grants. A persistent SQLite journal is materialized before tasks or remote control start, so its mask remains present across transactions. Schema version 1 adds proposal authorship; legacy proposals without an attributable author remain owner-owned.

## Export logs for a bug report

In the add-on page, open Settings and choose **Export logs**. Home Assistant's
integration **Download diagnostics** button includes the same anonymized data
inside Home Assistant's own diagnostics envelope. The whole files differ:
Home Assistant adds system information, including the time zone, component
information and the integration manifest. Review the entire downloaded file
before sharing it. Neither button sends a report to another service.

The worker's data excludes connection settings, accounts, task titles, prompts,
answers and raw tool output. The former integration entry/coordinator snapshot
was removed for this privacy boundary. Error descriptions and tool/model
categories come from fixed allowlists. Numeric release strings are retained;
ambiguous address-shaped four-part values are rejected. Unknown values remain
unknown. A test ties the bundled CLI version to its Dockerfile pin.

Settings lets an administrator choose **10–100 interactions**, with **25 by
default**. The byte cap scales from 128 KiB at ten to 1,280 KiB at 100. Completed
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
stores are protected from named task commands and file-change approvals.
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
subscription, resume, attachment or remote history read is added. Add-on page,
scheduled and lead-channel tasks are recorded by their worker lifecycle. Relay
read RPCs are grouped separately within one capability lifetime, with counts per
operation/domain; they do not describe the remote conversation or its approvals.
Only worker-visible gate refusals and approval events are included. The export
requires an administrator for Ingress requests; the integration's worker token
remains able to fetch it. Only an Ingress administrator may change retention.
An unavailable worker returns an unavailable data section without its exception
message or connection address.


### Chat page and attachments

The page follows Home Assistant colors in light and dark when its host exposes them; otherwise it follows
the device appearance. Recent conversations stay alongside the writing canvas on tablets and desktop.
On a phone the connection chip and Settings stay in the header, with conversations, New chat and the thread
menu below. The composer stays at the bottom. Its model and reasoning controls show the saved chat choice
or configured default, rather than a sample selection.

The plus menu uploads photos or files. A message accepts at most six attachments, up to 10 MB each.
Photos retain their existing resizing and image checks. File uploads support UTF-8 text, Markdown, YAML,
JSON, CSV, logs, Python/JavaScript source, HTML/CSS/XML source, and PDF headers. Binary text and unsupported
extensions are refused. Documents remain passive files: they are never executed or rendered in the page,
and downloads use attachment disposition, no-sniff and a sandboxed content policy. They are stored privately
with the message; Codex receives their validated paths and may read relevant contents as part of that task.
PDF acceptance does not promise a particular extraction tool. Draft attachments stay in the browser until
Send. Archiving a chat hides it from recent conversations without deleting its session or attachments;
Archived chats can restore it. Delete remains a separate action.

The Permissions picker starts at Default permissions: use the add-on's configured sandbox and native
approval flow. Read only narrows the next turn to the read-only sandbox, including a resumed or queued turn.
Neither choice expands a configured read-only sandbox, suppresses native approval, or changes managed
security refusals. A queued message keeps the choice submitted with it. Auto-review and Full access are
shown as Coming and cannot be selected. Camera and Plan mode are not offered in this build.

The thread menu includes Pin, Rename, Copy thread ID, Archive and Status. A thread without a native ID
reports that it is unavailable instead of substituting the page's task ID. Status shows the workspace,
selected model and permissions; context and usage/reset remain unavailable in this view until verified
window data is wired. No quota is inferred from activity.

Remote connection opens a separate view for Off, Starting, Ready, Reconnecting and Error. Pairing uses two
different protocol fields: the manual code for typing, and the opaque code for the Continue in ChatGPT URL
and its QR. The manual code is never substituted into the URL. If only a manual code is available, request
a new code or type it; no app link is invented. The QR has a four-module quiet zone at every width. Codes,
URLs and QR images remain limited to verified Ingress administrators; API tokens alone cannot read them
or start pairing. No live account enrollment or device handoff is implied by local rendering tests.

Settings → Advanced → Workspace rules opens a separate editor. The file is read only when that editor
opens, and Save remains an explicit action. Existing account, diagnostic, scheduling and browser-permission
controls remain in Settings.


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
