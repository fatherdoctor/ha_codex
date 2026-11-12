# Scheduled runs and the House Lead channel

Design recorded before implementation for CX-SCHED. The worker uses the same ordinary task launcher, sandbox policy, approval session, backups and validation for scheduled and lead-submitted tasks. This adds no permission decision route for agents and no outbound recipient or notification service.

## Data and restart behavior

A SQLite database under `/data` stores schedules, run records, mailbox messages and a hash of a separate lead token. A schedule contains a name, prompt, mode (Default or Read only), interval/UTC cron/one-off specification, enabled flag, owner approval and next due time. A run links one due occurrence to one normal task. SQLite transactions reserve the occurrence and advance its due time before launch. A crash in that gap is reported as interrupted; it is never retried automatically. On restart, ordinary saved tasks supply the outcome. Missed intervals coalesce into one run, with no catch-up burst. One-off schedules disable after reservation. Updates preserve the database in `/data`.

Cron has five numeric fields, UTC only, with lists, ranges and steps; Sunday is 0 or 7. Day-of-month and day-of-week use the conventional OR when both are restricted. Next-occurrence search is bounded to 366 days and rejects specifications with no occurrence in that horizon. Intervals are at least one minute. Limits: 100 schedules, 24 runs per schedule per UTC day, 64 scheduled runs total per UTC day, 500 retained completed run records and 500 mailbox messages. The normal worker's single task slot, pause switch and queue limits still apply. Due schedules wait while the worker is busy or paused.

## Owner approval and unattended runs

An administrator creates, edits, pauses, approves or deletes schedules on the page. An agent may propose a schedule; a proposal does not run until an administrator approves its exact current prompt, timing and mode. Agent edits invalidate approval and disable it. A saved Default run follows the configured sandbox; Read only can only narrow it. Unsupported modes fail before launch. Approval policy is always the existing on-request policy.

A permission request parks the native exchange: no automatic answer, no permanent grant and no background retry. The run log says “waiting for approval” and links to the existing task's permission card. The native process stays pending in the normal execution slot until the owner answers or cancels it; other schedules do not bypass it. Existing permission notifications point to the page, including on a configured phone. A restart expires native approvals; the saved run reports the task's interrupted result, and an old permission card cannot authorize a new exchange. This is parking a run, not granting it unattended authority.

## Lead API and mailbox

The administrator creates or rotates a separate token on the page and copies it directly to the other agent's private configuration. It is returned once; the worker stores only its SHA-256 hash. Rotation revokes the old token. It is not the integration token, and Ingress headers cannot authenticate a lead API call. Every `/lead/` route requires a bearer token, bounds its inputs and returns redacted text. The token cannot approve native actions, approve schedules, change policy or impersonate an administrator.

- `POST /lead/tasks`: submit a bounded prompt as an ordinary task, with Default or Read only; busy/paused workers refuse without silently creating duplicate work.
- `GET /lead/tasks/<id>`: read that task's normal result and pending approval state.
- `GET/POST /lead/schedules`, `PATCH /lead/schedules/<id>`: list or propose a schedule; proposals and agent edits require owner approval on the page.
- `GET/POST /lead/mailbox`, `POST /lead/mailbox/<id>/reply`: exchange plain-text messages and replies with the owner/other agent. Messages are data, not executable commands. The same mailbox is visible on the page. The worker does not send them to people or execute them. A lead must explicitly submit a task to request work.

Page management routes require authenticated Home Assistant Ingress and administrator verification. They never accept the lead or integration token in place of that identity. Lead task submissions pass the same protected policy checks as page tasks; all existing refusals and native human approval checks remain unchanged.

## Page and validation

A settings section offers a schedule form, a list with edit/pause/approve/delete, next due time and run history, plus token rotation and mailbox controls. All stored prompts and messages are rendered as text. A bounded refresh updates parked outcomes without opening a second runner.

Tests use temporary databases, a fixed clock and a synthetic worker. They cover cron/date edges, validation, approval invalidation, limits, restart/crash gaps, no duplicate claims, persistence, paused/busy workers, refusal of unapproved schedules, read-only propagation, pending native approval parking, token isolation/rotation, administrator-only management, task-result readback and mailbox replies. The full worker suite and repository container/browser jobs validate integration. Live-box and phone acceptance remain the lead's.

## Implementation boundary needing a lead ruling

The existing worker routes page-task approvals to `create_approval_session` and Home Assistant notifications. Its separate remote-control process owns ChatGPT-native approvals for remote threads. No supported bridge is established here for mirroring a scheduled page-task approval into a ChatGPT thread. The implemented page works on phones and retains configured Home Assistant push notifications; a ChatGPT-native mirror is unproven. The card's phone requirement needs a ruling on that surface or a supported integration contract before this portion can be completed. This draft does not change the relay, gate, policy or approval-decision code to guess at that contract.

Implementation also binds proposal approval to a fresh revision, refuses approval of a redacted agent prompt, and supports bounded tool-page sizes so large text can be read in smaller pages. These details preserve the original design's owner-review and response-size requirements.
