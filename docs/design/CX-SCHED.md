# Scheduled runs and the lead agent channel

Design recorded before implementation for CX-SCHED. The worker uses the same ordinary task launcher, sandbox policy, approval session, backups and validation for scheduled and lead-submitted tasks. This adds no permission decision route for agents and no outbound recipient or notification service.

## Data and restart behavior

A SQLite database under `/data` stores schedules, run records, mailbox messages and a hash of a separate lead token. A schedule contains a name, prompt, mode (Default or Read only), interval/local cron/one-off specification, enabled flag, owner approval and next due time. A run links one due occurrence to one normal task. SQLite transactions reserve the occurrence and advance its due time before launch. A crash in that gap is reported as interrupted; it is never retried automatically. On restart, ordinary saved tasks supply the outcome. Missed intervals coalesce into one run, with no catch-up burst. One-off schedules disable after reservation. Updates preserve the database in `/data`.

Cron has five numeric fields, in Home Assistant’s IANA time zone, with lists, ranges and steps; Sunday is 0 or 7. Day-of-month and day-of-week use OR when neither field starts with a wildcard; a leading wildcard, including */N, requires the other day field to match too. Next-occurrence search is bounded to 366 days and rejects specifications with no occurrence in that horizon. Intervals are at least one minute. Limits: 100 approved schedules and 20 proposals, 24 runs per schedule per UTC day, 64 scheduled runs total per UTC day, 500 retained completed run records and 500 mailbox messages. The normal worker's single task slot, pause switch and queue limits still apply. Due schedules wait while the worker is busy or paused.

## Owner approval and unattended runs

An administrator creates, edits, pauses, approves or deletes schedules on the page. An agent may propose a schedule; a proposal does not run until an administrator approves its exact current prompt, timing and mode. Agents can edit/delete only their own unapproved proposals. Approved schedules are owner-only. Legacy proposals without proven authorship remain owner-owned. A saved Default run follows the configured sandbox; Read only can only narrow it. Unsupported modes fail before launch. Approval policy is always the existing on-request policy.

A permission request parks the native exchange: no automatic answer, no permanent grant and no background retry. The run log says “waiting for approval” and links to the existing task's permission card. The native process stays pending in the normal execution slot until the owner answers or cancels it; other schedules do not bypass it. Existing permission notifications point to the page, including on a configured phone. A restart expires native approvals; the saved run reports the task's interrupted result, and an old permission card cannot authorize a new exchange. This is parking a run, not granting it unattended authority.

## Lead API and mailbox

The administrator creates or rotates a separate token on the page and copies it directly to the other agent's private configuration. It is returned once; the worker stores only its SHA-256 hash. Rotation revokes the old token. It is not the integration token, and Ingress headers cannot authenticate a lead API call. Every `/lead/` route requires a bearer token, bounds its inputs and returns redacted text. The token cannot approve native actions, approve schedules, change policy or impersonate an administrator.

- `POST /lead/tasks`: submit a bounded prompt as an ordinary task, with Default or Read only; busy/paused workers refuse without silently creating duplicate work.
- `GET /lead/tasks/<id>`: read background work's normal result and pending approval state; unrelated human chats are refused.
- `GET/POST /lead/schedules`, `PATCH /lead/schedules/<id>`: list or propose a schedule; only the lead’s own unapproved proposals may be edited or deleted; exact revisions require owner approval on the page.
- `GET/POST /lead/mailbox`, `POST /lead/mailbox/<id>/reply`: exchange plain-text messages and replies with the owner/other agent. Messages are data, not executable commands. The same mailbox is visible on the page. The worker does not send them to people or execute them. A lead must explicitly submit a task to request work.

Page management routes require authenticated Home Assistant Ingress and administrator verification. They never accept the lead or integration token in place of that identity. Lead task submissions pass the same protected policy checks as page tasks; all existing refusals and native human approval checks remain unchanged.

## Page and validation

A settings section offers a schedule form, a list with edit/pause/approve/delete, next due time and run history, plus token rotation and mailbox controls. All stored prompts and messages are rendered as text. A bounded refresh updates parked outcomes without opening a second runner.

Tests use temporary databases, a fixed clock and a synthetic worker. They cover cron/date edges, validation, proposal ownership and approval revisions, limits, restart/crash gaps, no duplicate claims, persistence, paused/busy workers, refusal of unapproved schedules, read-only propagation, pending native approval parking, token isolation/rotation, administrator-only management, task-result readback and mailbox replies. The full worker suite and repository container/browser jobs validate integration. Live-box and phone acceptance remain the lead's.

## Accepted phone surface and review fixes

The lead accepted configured Home Assistant phone notification plus the responsive permission page. No ChatGPT approval mirror is required or implemented. Scheduled and lead runs send no completion, failure, cancellation or question pushes; their only push is the existing permission-wait notification. All other outcomes stay in the run log and chat.

One-off entry/display uses Home Assistant time, not the browser’s zone. Cron stays at the same local wall time across DST: skip nonexistent minutes; run repeated minutes once at the first occurrence. Reject nonexistent one-off times. Missing HA time-zone metadata pauses entry and dispatch. Explicit offset API dates remain absolute. Ship IANA tzdata in the image.

Reserve/submit holds the normal worker slot lock. A Busy result restores the due occurrence only if its revision is unchanged; deferred attempts do not consume daily run caps. Crash gaps remain interrupted and never automatically retried.

Mailbox writes are capped independently for Codex and lead at 20 per turn and 64 per UTC day. Codex turn identity comes from its verified exchange; lead supplies a stable turn_id. Clearing does not reset counters. At 500 messages, prune the oldest other root thread and replies; if only the current thread remains, prune its oldest reply. The owner can clear all threads; lead can clear only lead-authored root threads. Replies are data, never authority.

All four private SQLite paths (database, persistent journal, WAL and SHM) join the worker-token masks, protected file paths and command checks. Initialize the database and persistent journal before task/relay startup. Schema version 1 adds author provenance with conservative owner defaults for legacy rows. Masked prompts have an explicit flag, disabled Edit and a backend redaction-marker refusal. Constant-time capability comparison avoids ordinary string equality.

Tests cover the F1–F9 findings in temporary databases and a synthetic worker, including DST transitions, bounded mail recovery, proposal ownership, redacted edits, launch races and actual wrapper argument masks. Local container smoke remains unavailable under VFS; repository CI provides container checks. Live HAOS and phone acceptance remain the lead’s.
