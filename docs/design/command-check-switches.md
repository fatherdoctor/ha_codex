# Command-check switch integration plan

This document records the shared contract implemented by this branch.
The original plan commit remains in history; the lead supplied the managed-rule base before this implementation.

## Owner controls

The collapsed Command checks group has five default-on switches: shell commands, REST commands, command-line integrations, Python scripts and templated actions.
Python scripts covers python_script and pyscript. REST commands also covers the REST and scrape configuration checks.
Each kind has one saved value shared by the hook and configuration watch. The reload-check build can use the same module.

The separate Self-protection group controls credential reads, full-access restrictions, and the app's settings, policy and rules.
These choices feed the hook, approval adapter, native sandbox masks, managed prefix catalogue and launch-policy validation.
The credential group is labeled Protect credentials and Supervisor commands and includes the existing Supervisor-connection restrictions.
Each restriction and shipped managed rule has an explicit group; display wording never selects it.
Turning off full-access protection does not change reviewer identity or the administrator approval requirement.
Only an Ingress administrator can save a choice. Each disabled self-protection requires a confirmation that names its effect.
The chat page and Status name disabled protections and explain when a restart is required.
They also warn that remaining protections may no longer hold if app settings can be changed or full access is used.
Confirmation text is supplied by the worker in the same status response the UI renders.

The private record lives at /data/protection-checks/settings.json. Its signing key lives beside it.
The reader requires a complete boolean schema, a private regular file, and the app's signature.
A missing, unreadable, malformed or modified record keeps every protection on.
The normal sandbox masks that directory. Disabling app-settings protection deliberately exposes the record and its signing key inside the container.

The native launcher keeps its namespaces and capability ceiling. It changes only masks and writable binds for existing app-internal paths.
It adds no host mount, capability, Supervisor scope, device access or AppArmor grant.
File permissions and the shipped container profile still apply.

## Applying a choice

The hook and watch read the current choice. A new sandbox command reads it before it enters the sandbox.
The worker regenerates managed requirements before a new Codex process starts.
An existing Codex process retains its loaded requirements until it restarts.
An app restart records the common applied state and clears the pending-restart notice for all processes.
Starting one new process does not clear a notice for another process that may still hold an older policy.

## Original integration survey

At main ce6751d5078b, gate/gate_hook.py calls gate_rules.check_tool_call.
The gate_rules module also supplies the configuration checks used by remote_control.ConfigWatch.
The approvals module applies its own protected-path decisions.
The codex-bwrap launcher masks credential and settings paths in sandboxed commands.
The policy module validates launch permissions.
Every affected layer needs the same validated setting; a UI-only switch cannot meet that contract.

The managed-rule interface is in draft PR #64 at 09c5bfd988ca.
Its remote_control.exec_rules_enabled function explicitly reserves the saved-setting connection for this work.
That function and managed catalogue are absent from the required main base.
The final connection and its cross-layer tests therefore need that implementation on the base first.
The lead later merged that implementation into this branch at 2a4f2277797a. This implementation uses the named seam.

## Validation required for implementation

Use synthetic settings and requests to check each specified switch independently.
Check that one switch affects only its kind across all implemented layers.
Check administrator authorization, confirmations, visible notices and default-on recovery.
Regenerate the managed catalogue at startup and explain when a Codex restart applies a choice.
Keep the add-on container boundary explicit in the documentation.
No live installation, credential, account or device is needed for these checks.
