# Permissions overrides

Status: implemented with direct-target-only checks, fail-closed current-turn authority and shared diagnostics.
Current behavior: the owner's direct-targets-only ruling, implemented at `11400e28fe69`,
supersedes the earlier indirect-inspection design. See [Permissions overrides in the user guide](../../codex-cli-worker/DOCS.md#permissions-overrides).
Codex CLI baseline: 0.166.0.

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

## Confirmed rules and direct targets

Settings uses the current chat page's Permissions overrides section. Only the exact marked block in
`/config/AGENTS.md` controls the tool. All other prose, including the kit's Careful start and Direct sets,
remains for the assistant to follow in chat. Unknown block lines suspend every group; known selected
lines identify their restricted domains beside the switches.

The first save of the group switches establishes the initial confirmed instructions text and SHA-256
under `/data/house-rules/`, outside the writable agent workspace. Later switch saves do not confirm edited
instructions: Review and confirm shows the text and submits its digest. Missing instructions are a confirmed state.
Compare the live file against the confirmed copy, pin that snapshot at begin(), and refuse every action
if it changes or becomes unreadable. Once observed, a change taints the whole exchange, even if restored.
Confirmation requires a new exchange to use the changed text. The permission adapter protects this
private folder; no new gate, policy, launcher or hook rules are introduced.

The add-on's hard refusals always remain: Supervisor/credential access and modification of its own policy
or settings cannot be enabled by a group. Group 5 is an optional group, not a bundled ban on its domains.

Judge only the requested service and its direct targets: the group switch, selected house-rules lines
for the direct domain or entity, and the Security and people group for direct security actions.
Area, device and label selectors resolve to entities of the service's domain. A direct lock action
still requires its security group and must respect a selected lock limit.

What a scene, script, automation, group, wrapper or template entity does in turn is the owner's own setup.
Do not walk members or downstream actions, classify platforms, compare editable files with loaded
definitions, or inspect new or changed definitions before reloads. An indirect lock effect does not
make a direct scene action a lock action. Unknown or template platforms alone do not prevent direct
control. Dynamic templates in direct request values, unresolved or mixed-domain direct targets and
unavailable Core data still require approval. This preflight does not promise an atomic Core transaction.

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
It performs one HTTP attempt without redirects; WebSocket commands use fixed types. Direct entity
checks read current state and registry identity, preserving refusals for credential entities and the
add-on's own platforms. They do not inspect configured downstream actions. Endpoint tests cover
operation mappings, transport bounds, flow filtering, target expansion, registry authority and private dashboard backups.

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
make a completed write look unexecuted.

The lead merged CX-DIAG's main into this branch. The shared worker/integration scrubbers now retain
only allowlisted `override` events: operation, group, decision, outcome and duration. The worker requires
an active recorder, saves and flushes the requested event before dispatch, and refuses if it cannot save.
Completed, refused and unknown outcomes pass through the same boundary. Raw targets and payloads never
enter these events. This is an extension of the shared schema, not a second logger.

Authority is pinned when the worker launches an exchange; current global options cannot upgrade an
already-running read-only turn. Capability rotation, end/cancellation and a changed launch policy refuse.
One authorization helper rechecks active capability/cancellation, current group settings and confirmed
rules before the audit, after it, and after every direct target's preflight is complete, immediately
before execution. Core does not provide an atomic
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
   custom wording and unreadable instructions. Direct restrictions must refuse the matching direct action,
   while scene/script/automation and template-entity downstream effects remain uninspected.
3. Unsupported direct operations, dynamic request values, unresolved targets and unavailable Core data
   require approval. Supported scene/template reloads do not inspect definitions. Rules changed, group
   disabled, task cancelled or capability revoked during the last target read must each cause zero writes;
   unchanged authorization permits one write. Denial cannot become a reusable blanket approval.
4. Strict schemas and hard refusals; administrator-only settings, atomic persistence and corrupt-file handling;
   no credential/target/message leak in errors or exported diagnostics.
5. Dashboard backup failure, failed ID-reference enumeration, partial failure and timeout; no automatic retries.
6. Full worker/integration suites, repository-data smoke, local light/dark browser checks and the repository's
   existing Container CI. Local container smoke is not run (VFS disk).

The original mode and diagnostics-base questions are resolved by the named lead replies. Remote writes stay unrecognized rather than guessed.


## Visual evidence

The light and dark comparisons under `screenshots/permissions-overrides-*.png` show the actual local
Settings controls beside the shipped Home Assistant Settings frontend (Core 2026.10.3, frontend
20260930.5), rendered by an isolated local HA test fixture with synthetic data and temporary test
authentication. Neither side pictures an owner installation. The official switch demo was also checked. Settings-row geometry was checked against
`home-assistant/frontend`'s `src/components/ha-settings-row.ts` (16px padding; 72px two-line rows).
The switch uses the current `ha-switch.ts` geometry (48px track, 24px height, 18px thumb), border and
checked/unchecked HA theme variables, inherited from the embedding HA page when accessible.
Component reference: https://contributors.home-assistant.io/design/components/switch/ . The actual HA
Settings-page screenshots came from the existing installed HA test environment; no live installation
was accessed. The isolated fixture and its browser children were shut down and reaped after capture.

The corresponding Claude add-on groups remain a later CLA-DESIGN handoff. This implementation changes only the Codex add-on; the lead can carry that parity note to the separate design card.


Device removal uses the supported Core `config/device_registry/remove_config_entry` command, as read
in the 2026.6.1 handler. The newer dev-only `remove` spelling is not assumed. A device must have exactly
one verified integration attachment; ambiguous/multiple attachments require normal approval. This
preserves the backend's own support checks and avoids guessing how to dismantle a composite device.


## Second peer review corrections

The earlier scene-effect inspection is superseded by the direct-targets-only ruling.
`guard_effect` checks the direct domain and selected limits; it never follows scene members,
scripts or automation branches. The selected external-publication rule still restricts direct
security actions and the supported direct configuration changes to which it applies.

Integration setup returns at most 64 field descriptors (name, type, required) and bounded
Core validation error keys. Defaults, choices, labels, descriptions, submitted values and
entry data are never returned. Before continuing, the adapter rereads the current form.
Password/secret fields (by field name, secret flag or password text selector) and unsupported
field shapes hand the entire step to the owner in Home Assistant, without a POST. Unknown
submitted fields are refused. Supported nonsecret form fields can be submitted; reflected
submission values are removed from response metadata and error keys too. No submitted
input or form description enters diagnostics. Core can change a flow after the preflight;
its API offers no atomic form-check-and-submit operation.

Non-service receipts name their reload kind, entity, device, setup flow or dashboard target.
Entity ID changes name both IDs. Registry display names and dashboard contents stay out of
receipts, and diagnostics continue to contain only the fixed operation/group/outcome fields.

The peer screenshot pair shows a native Home Assistant profile Settings row with an actual
`ha-switch` beside the add-on rows, in both themes. Both sides use isolated synthetic fixtures.

## Reloads and setup inputs

Apply my changes permits the supported automation, script, scene, template and group reloads of
whatever the owner's files currently hold. No loaded snapshots, file-versus-loaded comparisons or
new/changed-definition walks are used. Planted commands remain the existing command check's and
configuration watch's job; neither is changed by the groups. Person-started chat-page exchanges alone use the groups;
scheduled and lead-channel exchanges retain their normal prompts. Remote turns still lack verifiable authority.

Setup fills only host, port, name, username, title and scan_interval. Any other name, secret flag or password
selector hands off the entire form. No field values, choices, defaults or raw Core errors enter results.
