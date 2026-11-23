# Home Assistant verification

**Codex can check its own work in Home Assistant during a task:** find an entity by name, read fresh entity state,
run the configuration check, read Core logs and read a dashboard's configuration back. These checks run through
the worker, so they need no Home Assistant password, no long-lived token and no `HA_TOKEN` setting.

To change a dashboard, Codex makes the change, reads the configuration back and reports what changed, as the
Codex CLI does. No tool renders or screenshots a dashboard. Before version 0.4.0 the app opened dashboards in a
built-in browser with its own Home Assistant user; version 0.4.0 removed that browser, the
**Dashboard verification** user and the two browser options (see [Upgrading from an earlier
version](#upgrading-from-an-earlier-version)).

## Checks and results

- YAML changes retain the existing syntax and Home Assistant configuration checks.
- Entity checks fetch fresh state and up to ten explicitly named attributes. An
  expected state can be compared exactly. Run checks after any authorized reload;
  matching state does not prove automation triggers, conditions, or actions work.
- The last 100 lines of Core logs are available, capped
  at 32 KiB. Known credential patterns are redacted before persistence/model access.
  Logs can still contain personal information or unrecognized secrets. Other apps'
  logs are not accessible through this tool.
- A dashboard readback compares Home Assistant's loaded configuration with the edited one, or reports it when
  there is nothing to compare. A storage dashboard that Codex edited is saved when the turn ends (with
  `auto_save_lovelace`), and its backup and undo line stay as before.

API results are `observed`, `passed` or `failed`; a check that could not run is `unavailable`. Problems stay
visible even when the edit itself completes. Each turn retains `verification` in the task API,
`codex_cli.get_task` and normal task-result events, and the chat shows the results under the answer. Every check
is one Codex ran during the turn; the app runs none of its own after the final answer.

## Task tools

The worker registers a trusted `home_assistant` stdio MCP server for each task.
Its `verify` tool accepts the following JSON argument objects. The bridge connects
to the private worker socket outside the shell sandbox, so `workspace-write` and
`read-only` keep their existing network restrictions. The temporary capability is
forwarded to this process through an environment variable, never stored in config.
This bounded tool is approved for non-interactive calls; it does not grant general
shell network access, service calls, reloads or arbitrary URLs.

```json
{"operation":"find","name":"kitchen ceiling","domain":"light"}
{"operation":"entity","entity_id":"light.kitchen","expected_state":"on","attributes":["brightness"]}
{"operation":"config_check"}
{"operation":"logs"}
{"operation":"dashboard_readback","path":"/lovelace/lights"}
{"operation":"dashboard_readback","path":"/lovelace/lights","save_pending":true}
```

`find` looks an entity up by the words a person uses for it, so a device command needs no search of the files
under `/config`. An entity matches when its friendly name or entity id holds every word; `domain` is optional.
The answer holds at most 10 matches, each with its entity id, friendly name and state, the closest name first,
and says how many matched in all. Entities with a credential-like name and password-mode text entities are left
out. It is a read: it changes nothing and counts toward the message's check limit like any other check.

`dashboard_readback` reads a dashboard's configuration from Home Assistant over its WebSocket API. With
`save_pending: true` the worker first saves a storage dashboard edited since this turn's baseline and compares the
readback with the edited file; this needs `auto_save_lovelace` and a writable permission mode. Without it, the
readback reads the dashboard Home Assistant has loaded, and the worker saves the edit when the turn ends. Omit
`save_pending` for a dashboard this turn did not edit. Perform reloads/device actions only with the user's authorization,
then use a fresh readback. Do not equate an accepted command with a verified outcome.

## Authentication

The worker reads entities, the configuration check and dashboards with its automatic Supervisor token and its
existing `homeassistant_api` permission. No additional Supervisor role is granted to the worker, and the app
creates no Home Assistant user.

Log reads use one narrow integration endpoint, `/api/codex_cli/diagnostic_logs`. It needs a Home Assistant
administrator token (the Supervisor proxy's) and the paired worker token in the JSON body, because a custom header
would not survive the Supervisor proxy's header allowlist. It reads only Core's logs, with Core's Supervisor token,
and refuses every other target, so it does not allow other installed apps' logs.

## Upgrading from an earlier version

Versions before 0.4.0 created a read-only, local-only Home Assistant system user named **Dashboard
verification** for their browser. The first start of the 0.4.0 integration removes it: it reads the user's ID
from the integration's file `codex_cli.browser_identity` in `.storage`, removes that user only if Home Assistant
marks it as a system user with that exact name, deletes the file and writes one line to the Home Assistant log.
A person's account is never removed, even if the file names it. Removing the integration does the same.

The **Enable built-in browser** and **Browser memory limit (MiB)** options are no longer on the Configuration tab.
A value saved by an earlier version is ignored. Screenshots that earlier versions saved in a chat are deleted on
their old schedule, seven days after they were taken, and the chat no longer shows them.

## Limits

- At most 24 checks per turn, one at a time.
- Requests and answers are bounded: a request of at most 16 KiB, a Core answer of at most 256 KiB, a dashboard
  answer of at most 1 MiB.

## Credential and recovery boundaries

Supervisor credentials are removed from AI subprocess environments. An explicitly
configured `HA_TOKEN` retains its opt-in behavior. Secret files, which are every
`secrets.yaml` and every file under `.storage` (dashboards kept in storage included),
remain in change detection but are excluded from per-file backups and full snapshots. Snapshots made before
0.1.58 are not rewritten; like all backups they are deleted after the retention
period. Those files need Home Assistant backups for recovery. Other files can contain inline
secrets, so this is targeted protection, not complete secret detection.

Run commands from the repository root. The full `pytest` suite needs Linux
(or WSL on Windows), Python 3.14, and the Home Assistant test dependencies; see
[Testing](../CONTRIBUTING.md#testing). Tests under `codex-cli-worker/tests/ha`
use real Home Assistant authentication.
`python codex-cli-worker/tests/codex_verification_smoke.py` uses the pinned CLI and a local mock model
endpoint to check MCP discovery and real entity-tool calls in both sandbox modes,
while confirming that shell socket connections remain denied. Set `HA_TEST_CODEX`
to the pinned CLI binary when it is not at `/usr/local/bin/codex`. No model account
or paid request is involved.
`codex-cli-worker/tests/test_live_proofs.py` runs the pinned CLI's `app-server` with the worker's own launch
arguments and tool programs, and a local stand-in model, to check what one long-lived process with several
chats would rely on: tool programs start with each thread and report ready, `thread/unsubscribe` unloads a
thread and stops its tools, a resumed thread keeps its history, a turn may change the permission mode without
starting the tools again, `turn/interrupt` ends one turn and leaves the thread usable, two threads run turns at
the same time, and every tool call carries the thread id in `_meta.threadId` (a sub-agent's call carries its own
thread's id). It also prints three start-up times and what Codex does when a second process resumes a thread
that is live. Each tool program runs behind a small relay that logs what Codex sends it; the tool sockets do not
exist in the test, so tool calls fail after Codex has sent them. Set `HA_TEST_CODEX` as above; without the pinned
CLI the tests are skipped, unless `HA_TEST_REQUIRE_CODEX=1` (set in the container job) makes that a failure. The
socket check of the same kind is in
[SANDBOX_TESTING.md](SANDBOX_TESTING.md#the-workers-sockets-and-the-sandbox-live-proof).
CI also builds both container architectures and checks that the image holds no browser. Fixture tests are not a
deployment test of a user's HAOS kernel or AppArmor policy.

Reference: [app authentication](https://contributors.home-assistant.io/apps/guides/communication/).
