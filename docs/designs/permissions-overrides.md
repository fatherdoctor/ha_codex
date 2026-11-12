# Permissions overrides

Status: design prepared; implementation waits on a remote-thread permission contract.
Source baseline: ha_codex main `6966e2cb8ced`, Codex CLI 0.166.0.

The worker can perform selected Home Assistant actions without an escalation prompt when an administrator
has enabled their group. It must establish the calling turn's effective permissions before doing so.
An MCP tool argument, prompt instruction, global add-on option, or another thread's mode cannot establish
that authority. No write tool or settings route is enabled by this design document.

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
before execution; never infer a domain from a display name. Resolve areas, devices, labels and `all` to
explicit entity IDs before checking permissions. An unresolved target returns an approval-required result.
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

## Modes and the unresolved authority boundary

| Effective calling turn | Intended behavior |
| --- | --- |
| Read only | Never use a group to authorize a write |
| Default | Enabled groups run after target/guard checks; otherwise use the existing approval path |
| Auto-review | Check enabled groups before the reviewer; other requests go to that reviewer |
| Full access | Name/log actions and retain guard rails and hard refusals; do not add a prompt merely for a group |
| Custom | Groups apply only when the actual sandbox allows writes and the request would otherwise ask |
| Missing, stale or unverifiable authority | No pre-approved write |

The current chat worker owns its task mode, but the remote app-server owns remote threads. The existing
`relay_reads.py:70` capability rotates per relay launch, not per thread or turn. Its MCP bridge forwards
only the capability and model-supplied arguments (`relay_reads_mcp.py:28`). The shared relay capability
proves an active relay, not whether this call came from a read-only thread. Do not repurpose it as write authority.

The pinned upstream has a possible transport, not yet a verified mode-classification contract:

- [mcp_tool_call.rs](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/mcp_tool_call.rs#L540)
  adds host-owned thread/session metadata and can negotiate experimental sandbox state.
- [sandbox_metadata.rs](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/codex-mcp/src/sandbox_metadata.rs#L23)
  explicitly defines that state as **opaque**, to be forwarded unchanged to `codex sandbox
  --sandbox-state-json`; only the separate launcher envelope's documented fields may be interpreted.
  The existing bridge neither advertises this capability nor authenticates a turn-specific envelope to the worker.
- [hook_runtime.rs](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/hook_runtime.rs#L1073)
  derives hook `permission_mode` from approval policy alone: on-request is `default`. It does not distinguish
  read-only/on-request from workspace-write/on-request. Copying that string would not solve the problem.

Before implementation, establish a supported, authenticated per-call classification of the effective
sandbox and approval/reviewer settings, with evidence for remote mode changes and resumed threads.
Thread IDs alone, model-supplied mode fields, stale startup settings and parsing undocumented opaque
policy fields are not sufficient evidence. A local subprocess probe may be investigated, but success
must prove the relevant authority without granting side effects or trusting a caller-selected launcher.
No live Home Assistant or owner-account probe is part of this work.

There is also a policy decision to settle: `gate/requirements.toml:11` allows only read-only/workspace-write
and on-request approvals; `policy.py:88` refuses full access, never approvals and a reviewer other than the
user. Thus the currently shipped policy cannot exercise the card's full-access/auto-review cases as written.
This design does not loosen that policy. The outside-lane brief excludes hook files. The lead needs to
clarify whether these cases are conditional future behavior or require a separate policy/authority change.

When a group is off or a guard rail applies, return `approval_required` with a safe description, without
executing. That response is not itself an approval prompt. Verify the real client approval/reviewer path
and its single-action binding before promising seamless fallback, especially where a direct command has
no separately configured Core API token.

## Settings and diagnostics

Add **Permissions overrides** to the current Settings view, moving it to the new view if that view has
merged before implementation. Five rows use Home Assistant's `ha-settings-row` spacing/dividers and
`ha-switch` dimensions, checked track/knob colors and theme variables. Read/save routes are administrator
ingress only; a worker API token is not enough. Persist booleans atomically in worker-owned data, mode 0600;
reject unknown keys, wrong types and non-admin requests. A chat, model tool call or writable config file
cannot toggle its own permissions. Unreadable settings fail closed rather than restoring permissive defaults.

Capture light/dark settings screenshots beside a Home Assistant settings reference using synthetic local
fixtures. None are supplied yet: there is no working settings implementation to photograph.

Record the action kind, group, decision, outcome and duration through the shared diagnostics boundary.
Apply its scrubber and anonymize targets; never put message bodies, flow credentials, dashboards or raw
owner instructions in exported diagnostics. Ensure an observer failure cannot change authorization or
make a completed write look unexecuted. The current main does not yet contain the diagnostics PR, so
integrate its published interface after merge rather than recreating its delivered implementation.

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

Implementation is blocked at the authority contract and policy interpretation above. This is not a
claim that upstream lacks any possible solution; the cited experimental transport needs a supported
interpretation and an offline proof before it can authorize writes outside the sandbox.
