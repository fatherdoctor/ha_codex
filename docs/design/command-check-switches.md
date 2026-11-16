# Command-check switch integration plan

This document records the integration points for owner-controlled protection switches.
It describes a proposal; the switches are not implemented here.

## Owner controls

The settings page needs a collapsed Command checks group with five default-on switches:
shell commands, REST commands, command-line integrations, Python scripts and templated actions.
Python scripts covers both python_script and pyscript.
Each kind uses one saved value across the hook, configuration watch and managed rules.
Save settings under /data, outside the normal sandbox's writable roots.
A missing, unreadable or invalid setting keeps every protection on.

A separate Self-protection group covers credential reads, full access and the app's own settings, policy and rules.
Only an administrator changes these values.
Disabling one requires an explicit explanation and confirmation.
The chat page and Status must show which protections are off.
A saved choice cannot grant access beyond the app container and its existing sandbox ceiling.

## Current integration points

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
This branch does not copy the other draft's implementation or change either branch's history.

## Validation required for implementation

Use synthetic settings and requests to check each specified switch independently.
Check that one switch affects only its kind across all implemented layers.
Check administrator authorization, confirmations, visible notices and default-on recovery.
Regenerate the managed catalogue at startup and explain when a Codex restart applies a choice.
Keep the add-on container boundary explicit in the documentation.
No live installation, credential, account or device is needed for these checks.
