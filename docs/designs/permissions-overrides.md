# Permissions overrides

Status: installed on the worker, with fail-closed current-turn authority and shared diagnostics.
Source baseline: ha_codex main `9dcef3abe5bc`, Codex CLI 0.166.0.

The worker can perform selected Home Assistant actions without an escalation prompt when an administrator
has enabled their group. It must establish the calling turn's effective permissions before doing so.
An MCP tool argument, prompt instruction, global add-on option, or another thread's mode cannot establish
that authority. The worker registers `home_assistant_actions.act` and administrator-only Settings routes.

## Groups and request boundary

| Settings row | Initial value | Worker operations |
| --- | --- | --- |
| Apply my changes | On | Reload automations, scripts, scenes, templates and groups; configuration check; start or continue an integration setup |
| Everyday control | On | Light and switch on/off/toggle; scene activation; automation enable/disable/trigger; input boolean/number/select/text values |
| Organize | On | Device/entity display name, area and labels; save a UI dashboard after its backup succeeds |
| Bigger changes | Off | Run a script; change an entity ID with a references report; Core backup/restart; delete a device, entity, automation, script or dashboard |
| Security and people | Off | Locks, alarms, covers/garage doors, cameras, notifications and messages |

Use typed operations with closed schemas, length limits and exact service/command mappings. Do not accept
arbitrary URLs, service names, shell commands, credentials, config paths or arbitrary WebSocket messages.
Keep the Core credential in the worker. Validate registry identities and current target state using Core
before execution; never infer a domain from a display name. Resolve areas, devices and labels to
explicit entity IDs; ambiguous selectors such as `all` require normal approval. Resolve targets before checking permissions. An unresolved target returns an approval-required result.
Configuration flows may request setup input but must never echo credentials into logs, results or diagnostics.

Serialize checks and execution per request. Back up a dashboard before mutation and refuse if that fails.
Build the old-ID reference report before an entity-ID change. Return the exact action and affected targets
after execution, or a bounded error identifying whether any mutation may already have happened. Do not
automatically retry a write after a timeout. Inform the assistant that a pre-approved action must be named
in its answer; this instruction alone cannot guarantee what the model eventually says.

## Guard rails and indirect targets

Read the owner's marked guard-rail section in `/config/AGENTS.md` on every action; do not cache it across
changes. The kit's `templates/agent-guard-rails.md` supplies six optional natural-language lines and explicitly
allows narrower custom wording. It is not a machine-readable policy schema. Recognize the documented lines
conservatively, but treat an unrecognized/custom restriction, unreadable file, malformed markers or ambiguous
target as requiring approval. Do not silently ignore a restriction because its spelling is unfamiliar.
The add-on's hard refusals always remain: Supervisor/credential access and modification of its own policy
or settings cannot be enabled by a group. Group 5 is an optional group, not a bundled ban on its domains.

For scenes, inspect every entity in the current scene configuration. For automation/script execution,
recursively inspect every reachable action, branch, parallel sequence, script and scene. Check both the
entry action's group and every reachable effect's group and owner restriction. Runtime templates,
unresolved dynamic targets, integrations with unknown indirect effects, recursion, missing configuration,
unsupported actions or an exhausted work budget require approval. Do not claim to have checked these from
state attributes alone. Use a bounded configuration snapshot and detect changes before execution; an
unverifiable snapshot cannot authorize a pre-approved action. A read-only preflight cannot guarantee that
an external actor will not change a scene between checks and execution; document that limitation rather
than claiming a transaction Core does not provide.

## Mode ruling and authority

The named lead reply to `2026-11-12-1612-cx-allow-authority.md` settles the authority boundary:

- Use the chat worker's protected launch policy, never a model-supplied mode. The prepared service resolves
  the existing current-turn capability and rechecks its task/turn/cancellation state before dispatch.
- Read-only, remote, missing, stale and unrecognized authority cannot pre-approve anything. The prepared
  backend accepts only a worker-owned workspace-write/on-request context under today's managed policy.
- No real captured sandbox-state envelopes for the pinned CLI are available here. The prepared implementation
  does not interpret that opaque metadata. Every remote turn requires native approval, including a remote
  thread that claims to be Default. This follows the lead's explicit fallback; no metadata parser is shipped.
- Auto-review and Full access remain conditional future behavior for CX-PERM. No managed policy, hook or
  gate file changes are part of this build. The Authority boundary keeps future mode classification separate
  from model arguments. Full-access behavior still needs the later policy work; it is not enabled here.

Prepared modules are `codex-cli-worker/permission_overrides.py` (settings, authority, guard rails and effect
resolution) and `permission_overrides_api.py` (fixed Core adapter and current-turn/admin-route adapter).
They are imported, registered and packaged with the stdio MCP bridge. Tests use synthetic Core backends only; no live system was accessed. A failed/unknown write is explicitly nonretryable; a failed preflight
never dispatches. An audit failure before execution refuses the action. After execution, audit failure
cannot change the reported execution result.

The prepared Core adapter uses the existing worker's Core authentication, not sandbox-visible credentials.
It performs one HTTP attempt without redirects; WebSocket commands use fixed types. Definition preflight
uses Core configuration endpoints, never state attributes alone. Source checks used Home Assistant Core's
config/automation.py, config/script.py, config/scene.py and config/config_entries.py handlers. Endpoint tests cover all operation mappings, transport bounds, flow filtering, target expansion, registry authority and private dashboard backups.

## Settings and diagnostics

Add **Permissions overrides** to the current Settings view, moving it to the new view if that view has
merged before implementation. Five rows use Home Assistant's `ha-settings-row` spacing/dividers and
`ha-switch` dimensions, checked track/knob colors and theme variables. Read/save routes are administrator
ingress only; a worker API token is not enough. Persist booleans atomically at `/data/codex-home/house-rules/permission-overrides.json`, mode 0600;
reject unknown keys, wrong types and non-admin requests. A chat, model tool call or writable config file
cannot toggle its own permissions. Unreadable settings fail closed rather than restoring permissive defaults.

Capture light/dark settings screenshots beside a Home Assistant settings reference using synthetic local
fixtures. Use only synthetic local data and the official frontend component reference, never owner data.

Record the action kind, group, decision, outcome and duration through the shared diagnostics boundary.
Apply its scrubber and anonymize targets; never put message bodies, flow credentials, dashboards or raw
owner instructions in exported diagnostics. Ensure an observer failure cannot change authorization or
make a completed write look unexecuted. The current main does not yet contain the diagnostics PR, so
integrate its published interface after merge rather than recreating its delivered implementation.

The lead merged CX-DIAG's main into this branch. The shared worker/integration scrubbers now retain
only allowlisted `override` events: operation, group, decision, outcome and duration. The worker requires
an active recorder, saves and flushes the requested event before dispatch, and refuses if it cannot save.
Completed, refused and unknown outcomes pass through the same boundary. Raw targets and payloads never
enter these events. This is an extension of the shared schema, not a second logger.

Authority is pinned when the worker launches an exchange; current global options cannot upgrade an
already-running read-only turn. Capability rotation, end/cancellation and a changed launch policy refuse.
The worker rechecks activity after the audit and before execution. Core does not provide an atomic
check-and-write transaction: external configuration changes during dispatch remain a documented limit.
The returned result names the action; the tool description and worker instructions require naming it in
the answer, but the model's final wording is not mechanically guaranteed.

The settings directory is under the existing remote hook's protected house-rules path. The native
approval adapter refuses that directory too, including ancestor replacement commands. No gate, hook,
managed policy or CI file is changed. The shell sandbox's writable workspace remains `/config`.

## Acceptance checks before enabling writes

1. Prove the authority contract with two concurrent remote threads of different modes, changes between turns,
   resumed threads, Custom read-only, expired/forged/replayed envelopes and missing metadata. Read-only must
   make zero Core write calls. Check chat-page parity and the managed-policy refusals too.
2. Each group on/off/default, all targets in mixed-domain/area/device/label requests, every guard-rail line,
   custom wording and unreadable instructions; indirect actions with disabled or restricted targets.
3. Unknown/template/cyclic actions and changing configuration require approval; no write before approval;
   denial cannot become a group permission or a reusable blanket approval.
4. Strict schemas and hard refusals; administrator-only settings, atomic persistence and corrupt-file handling;
   no credential/target/message leak in errors or exported diagnostics.
5. Dashboard backup failure, failed ID-reference enumeration, partial failure and timeout; no automatic retries.
6. Full worker/integration suites, repository-data smoke, local light/dark browser checks and the repository's
   existing Container CI. Local container smoke is not run (VFS disk).

The original mode and diagnostics-base questions are resolved by the named lead replies. Remote writes stay unrecognized rather than guessed.


## Visual evidence

The light and dark comparisons under `screenshots/permissions-overrides-*.png` show the actual local
Settings controls beside the official public frontend switch demo. They are explicitly labeled component
references, not screenshots of an owner installation. Settings-row geometry was checked against
`home-assistant/frontend`'s `src/components/ha-settings-row.ts` (16px padding; 72px two-line rows).
The switch uses the current `ha-switch.ts` geometry (48px track, 24px height, 18px thumb), border and
checked/unchecked HA theme variables, inherited from the embedding HA page when accessible.
Reference: https://contributors.home-assistant.io/design/components/switch/ . A full production HA
Settings-page screenshot was not taken because this lane cannot access a live installation.

The corresponding Claude add-on groups remain a later CLA-DESIGN handoff. This implementation changes only the Codex add-on; the lead can carry that parity note to the separate design card.
