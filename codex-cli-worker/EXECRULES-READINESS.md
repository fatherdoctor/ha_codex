# Managed command rules: blocked integration

Reviewed on 2026-11-14 against ha_codex main `3721128e63ce` and the
[CX-EXECRULES research](https://github.com/fatherdoctor/HomeAssistant/blob/main/docs/reviews/external/CX-EXECRULES.x-2026-11-13.md).
This is an implementation handoff, not a shipped command protection or a Bronze claim.

The card requires every check kind to obey the owner's CX-CHECK-TOGGLES setting,
with one saved value read by every layer. That interface is absent on this main.
The CX-CHECK-TOGGLES row still waits until 2026-11-16. Its settings, confirmation
and status behavior are separate work; this report does not invent their schema
or silently deliver mandatory rules that a future switch cannot disable.

| Requirement | Current evidence and disposition |
| --- | --- |
| Managed forbidden/prompt prefixes, no allow | `gate/requirements.toml:1-14` carries sandbox, approval and hook requirements, without a rules section. Adding native rules remains implementation work. The pinned research establishes the managed TOML format and literal/pipeline behavior. |
| Preserve semantic hook checks | `gate_rules.py:821` is the existing tool-call check. Wrappers, arbitrary credential readers, structured edits and YAML inspection cannot be replaced by a prefix catalogue. No hook or policy source was changed. |
| Rule-load warning fails self-test and keeps Turn on unavailable | `remote_control.py:125` checks the managed requirements, `remote_control.py:196` gates startup, and `remote_control.py:1038` reads process output. Native rule-load validation and warning handling remain implementation work; existing readiness is not evidence that new rules loaded. |
| Each check obeys its saved owner switch | Blocked: no shared saved check-kind settings contract exists in main. CX-CHECK-TOGGLES owns switches across the approval, hook, rules and reload layers, including administrative confirmation and visible notices. |
| Each literal and pipeline refused through app-server without approval | Not run: no implementation to validate. Standalone argv parsing would not substitute for these runtime checks. |
| Environment/assignment wrappers remain hook-tested | Not run in this handoff; no changed hook or rule implementation is claimed. |
| Malformed ordinary rule file fails self-test | Not run: no new self-test exists. A malformed ordinary file can discard ordinary custom rules; managed requirements and a failed startup check must cover this case. |

The released 0.166 binary and the existing dependencies are available in the
executor. A child-subreaper-wrapped, disposable Bubblewrap mount-namespace smoke
check completed successfully without altering `/etc/codex`, credentials or a live
system. Thus runtime isolation is available; it does not resolve the missing
settings contract. No ordinary `.rules` file or allow rule is added by this draft.

Needed before implementation resumes: the shared settings contract on main, or
a named ruling that explicitly sequences this card's rule delivery and its
switch integration. The owner was asked which scope to use; no answer was
available when this handoff was written. The repository mailbox records the same
blocker. No code suite was run for this documentation-only handoff. The worker's
existing protections remain the behavior of main; this draft adds none.
