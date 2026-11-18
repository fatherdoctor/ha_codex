# Auditing this add-on

This page is for someone who wants to check, by reading the code, that Codex for Home Assistant does what its
security notes say. It lists each protection, the one place that enforces it, and the tests that show it. It does
not describe features; see [DOCS.md](codex-cli-worker/DOCS.md) for those and [SECURITY.md](SECURITY.md) for how
to report a problem.

If you find a protection that is enforced somewhere this page does not name, or a row that points at the wrong
place, that is a bug in this page and worth an issue.

## The shape in one paragraph

The add-on is one container. A Python worker (`codex-cli-worker/server.py`) serves the chat page through Home
Assistant Ingress and starts the Codex CLI for each exchange. Codex runs every command inside a Bubblewrap
sandbox whose launcher the add-on wraps (`codex-bwrap`). Anything outside the sandbox goes to Codex's normal
approval flow, and an approval has to be answered by a Home Assistant administrator on the chat page. After
every exchange the worker validates what changed and keeps a copy of each changed file. A separate, optional
process connects the ChatGPT app to Codex through OpenAI's relay ("remote control"); there Codex's own
approval card on the phone answers the requests.

The add-on has no command check of its own (removed in 0.3.6): no hook, no command-prefix rules, no refusal of
named paths on the approval card, and no scan of `/config` after an exchange or while remote control is on.
Codex's approval mode and the sandbox are the protection, as in the Claude add-on.

So there are three layers:

1. **What Codex's process can see** - its environment and the sandbox's file view.
2. **What a person must approve** - and who counts as that person.
3. **What the worker checks afterwards** - the config check and the backups.

## Where each protection lives

Paths are under `codex-cli-worker/` unless they start with `custom_components/`.

### Layer 1: what Codex can see

| protection | enforced in | shown by |
|---|---|---|
| The Supervisor token and the worker's own capabilities are not in Codex's environment | `server.py: codex_env` | `tests/test_server.py`, `tests/test_sandbox_readiness.py` |
| The remote-control process gets an environment without the Supervisor token or per-exchange capabilities | `remote_control.py: STRIPPED_ENV`, `remote_env` | `tests/test_remote_control.py` |
| A sandboxed command cannot see `secrets.yaml`, Home Assistant's sign-in store, the add-on's options, its API token, the schedule database or the container's saved environment | `codex-bwrap: HIDDEN_DIRS`, `HIDDEN_FILES` | `tests/test_bwrap.py`, `tests/apparmor_317_check.py` |
| The one case where the wrapper changes Bubblewrap's arguments (Codex's own `/bin/true` probe) | `codex-bwrap: is_codex_proc_probe`, `codex-bwrap-probe.py`; explained in `SANDBOX_TESTING.md` | `tests/test_bwrap.py`, `tests/test_proc_assertion.py` |
| A sandboxed command cannot see the add-on's GitHub sign-in (`/data/github`); the token is never in an environment the worker builds, never in a git argument, remote URL or log, and the folder is left out of the add-on backup | `codex-bwrap: HIDDEN_DIRS`; `github_access.py: GitRunner.env`, `Store`; `github_askpass.py`; `config.yaml: backup_exclude` | `tests/test_bwrap.py` (`test_the_github_folder_is_hidden_in_every_sandbox`), `tests/test_github_access.py` |
| The relay runs under its own AppArmor label with all capabilities dropped, entered only through one fixed launcher | `apparmor.txt`, `relay-launcher`, `relay-tool-launcher` | `tests/apparmor_317_check.py`, `.github/workflows/apparmor-317.yaml` |
| The worker's memory cannot be read by a process it starts | `server.py: make_worker_undumpable` | `tests/test_undumpable.py` (`test_the_flag_is_set`) |
| Each way in (the chat page, the ChatGPT app, `codex_cli.start_task`) gets the Home Assistant tools that DOCS.md "Ways in" lists, and no others | `server.py: build_app_server_args`, `build_codex_args`, `relay_read_mcp_args`; `remote_control.py: Supervisor._command` | `tests/test_surface_matrix.py` |
| Full access is not available, whatever a client or an option asks for | `gate/requirements.toml` (Codex's managed requirements) and `policy.py: load_policy`, `policy_problem`; checked before each exchange at the top of `server.py: run_task` | `tests/test_policy.py` |
| No hook runs: the managed requirements allow only managed hooks and the image ships none, so a hook planted in `/config` or Codex's home (which would run outside the sandbox) is ignored | `gate/requirements.toml` (`allow_managed_hooks_only`); no `managed_config.toml` in the image (`Dockerfile`) | `tests/test_guards_removed.py` shows the setting and that the image ships no managed hooks. That Codex then ignores a planted hook is Codex's own behavior: no test here runs Codex, so the lab pass before a release checks it |

### Layer 2: who can approve

| protection | enforced in | shown by |
|---|---|---|
| The chat page is reachable only through Home Assistant Ingress; a request that claims to be Ingress from any other address is rejected | `server.py: enforce_ingress_boundary`, `is_ingress_request` | `tests/test_server.py` |
| Every other route needs the worker's API token, compared in constant time | `server.py: require_auth` | `tests/test_api.py`, `tests/test_server.py` |
| When an Ingress session expires, the page gets a new one only from the Home Assistant frontend that holds it, through that frontend's own signed-in connection in a same-origin parent window; the page holds no credential, a parent on another origin gets no request, and the worker's checks above are unchanged | `web/assets/chat.js: renewIngressSession`, `pageFetch` | `tests/browser_accounts.cjs` |
| The API token (held only by the integration) can never answer a permission request; only an active Home Assistant administrator signed in on the chat page can, and a failed lookup refuses. The `action` argument of `ingress_admin` only names the attempted action in the refusal text; it never changes who passes | `server.py: ingress_admin`, `lookup_ha_users`, `decide_approval` | `tests/test_approvals.py` |
| A native approval covers one action in the current exchange; it installs no rule and expires with the exchange. There is no standing permission: no launch (chat page, schedule or ChatGPT app) gives Codex a tool that changes the house without a request | `approvals.py`: `AppServerSession.decide` and `.receive`; `server.py`: `build_codex_args`, `build_app_server_args`, `relay_read_mcp_args` | `tests/test_approvals.py`, `tests/test_approval_smoke.py`, `tests/test_grant_layer_removed.py` |
| The worker writes no add-on option (0.3.5): the remote-control machine name and the number of interactions kept are set on the Configuration tab only, and the page's rename and retention-save routes are gone; the page reads the app's slug from Supervisor only to link to that tab, and a slug that is not lowercase letters, digits and underscores is not used | `server.py`: `/remote-control/<action>` (start, stop, pair, review only), `apply_interaction_limit`, `addon_slug`, `/addon-configuration` | `tests/test_remote_name_and_first_reply.py`, `tests/test_interaction_diagnostics_fix.py`, `tests/test_fb2_split.py` |
| Browser permissions are managed only from the chat page | `server.py: require_ingress` | `tests/test_browser_permissions.py` |
| The GitHub sign-in can be connected, tested, accepted or disconnected only by an administrator on the chat page; the worker's API token can read its status and nothing else | `github_access.py: register` (the `/github` routes) | `tests/test_github_routes.py` |
| A push Codex asks for runs the kit's push guard first, from the read-only copy in the image, and pushes only the commit it checked; a push that adds a `guard: allow` mark is refused, since an allow mark needs the owner; no force, delete or tag push | `github_access.py: GitRunner.push`, `valid_branch`, `GitHubService.dispatch`; `push_guard.py` (byte-identical to the kit's) | `tests/test_github_access.py` |
| Schedules and the mailbox are owner-managed: the page routes need the worker's API token and an administrator on the chat page (`owner`). The local lead channel under `/lead/` is separate: it needs its own token, not Ingress, and reads only background work, not people's chats. The scheduling tools a chat-page task can call use a third path: a Unix socket (mode 0600) where each request must carry the exchange's running-turn capability, which ends with the exchange; a proposal is saved disabled until an administrator approves it | `schedule_api.py: register` (`owner`, `lead`), `schedule_api.py: Service.dispatch`, `Service.serve`, `scheduling.py: Store.token_ok` | `tests/test_scheduling.py` (`test_lead_requires_separate_token_and_ingress_alone_is_not_a_token`, `test_tool_transport_roundtrip_and_expired_capability`), `tests/test_sched_review.py` |

### Mailbox and lead task boundaries

| protection | enforced in | shown by |
|---|---|---|
| A worker can post mailbox text but cannot start lead work or attach a task to its mail | `schedule_api.py: Service.dispatch` | `tests/test_lead_direct.py: test_worker_mail_can_notify_lead_but_cannot_dispatch_lead_work` |
| Only the separate, authenticated lead task routes accept work: `/lead/tasks` and an explicit task on `/lead/mailbox`. Owner messages and replies cannot attach tasks | `schedule_api.py: lead`, `lead_task_submit`, `lead_mail_send`, `mail_fields` | `tests/test_scheduling.py: test_lead_requires_separate_token_and_ingress_alone_is_not_a_token`; `tests/test_lead_mail_review.py: test_owner_messages_and_replies_never_accept_tasks` |
| Retention and capacity enforcement never remove unread mail. A full unread recipient refuses the entire delivery; a new arrival can evict read copies only | `scheduling.py: Store._prune_mail`, `Store._make_mail_room` | `tests/test_lead_direct.py: test_unread_survives_days_full_refuses_atomically_read_evicts_first`; `tests/test_lead_mail_review.py: test_codex_walks_every_legacy_message_above_capacity` |
| An administrator who views an agent's mailbox does not acknowledge it. The page acknowledges only the owner's mailbox | `schedule_api.py: owner_mail_list`, `mail_page`, `owner_mail_read` | `tests/test_lead_mail_review.py: test_owner_cannot_acknowledge_another_readers_mailbox`; `tests/test_scheduling.py: test_owner_acknowledges_only_displayed_ids_and_requires_admin` |

### Layer 3: what the worker checks afterwards

| protection | enforced in | shown by |
|---|---|---|
| After every exchange, changed files are found by comparing `/config` with the manifest taken before it | `server.py: run_task`, `build_manifest`, `diff_manifests` | `tests/test_backups.py`, `tests/test_server.py` |
| Changed YAML is validated and Home Assistant's own config check is run | `server.py: validate_changed_files`, `check_home_assistant_config` | `tests/test_config_check.py` |
| A copy of each changed file is kept, without credentials | `server.py: create_snapshot`, `saved_copy`, `drop_credential_backups` | `tests/test_backups.py` |

### The browser that inspects dashboards

The worker can open a Home Assistant dashboard in a headless browser to look at it, without giving Codex a login. These are the protections of that one path.

| protection | enforced in | shown by |
|---|---|---|
| The browser signs in as a temporary read-only identity that the integration creates, never as a person. A session lasts 180 seconds, only one is issued at a time, it is revoked when it ends or the integration unloads, and only an administrator who also presents the paired worker's token can ask for one (compared in constant time) | `custom_components/codex_cli/browser_auth.py: BrowserSessions`, `SESSION_SECONDS`, `BrowserSessionView.broker` | `tests/ha/test_browser_auth.py` (`test_real_read_only_identity_lease_and_revocation`, `test_broker_rejects_admin_without_pairing_and_read_only_user`, `test_read_only_session_cannot_save_dashboard`) |
| The page can reach only Home Assistant's own address, with an allow list for HTTP requests and for WebSocket commands. The real access token is held by the controller and sent on the controller's own connection; page scripts see a placeholder, and token replies are replaced before the page reads them | `browser.cjs: allowRequest`, `allowMessage` | `tests/browser_verification.cjs`, `tests/browser_worker_check.py` |
| A saved browser exception is checked again, by its fingerprint and eligibility, each time it is used, not only when it is saved | `browser_permissions.cjs: validate`, `BrowserPermissions`; `browser_permissions.py` (the save-time check); rules in `browser_permission_policy.json` | `tests/browser_permissions.cjs`, `tests/test_browser_permissions.py` |
| An outside resource the page asks for (a script, style, font or image from a public site) is fetched by the controller, not the page: the name is checked and resolved, the connection is pinned to the address that was checked, each redirect is checked again, size and time are limited, private addresses are refused, and no credentials are sent | `browser_resources.cjs: ResourcePolicy`, `createExternalFetcher`, `publicAddress`, `publicURL` | `tests/browser_resources.cjs` |
| Files the chat page serves back (attachments and verification screenshots) carry `X-Content-Type-Options: nosniff` and a Content-Security-Policy of `default-src 'none'; sandbox`, so a served file cannot run as a page | `server.py` (the attachment route; the headers are set at its end) | `tests/test_attachments.py` (the `nosniff` header; the policy header is not asserted, so check it by reading) |

### Reads that go through the worker instead of Codex

Codex holds no Home Assistant credential for these; it holds a short-lived capability and the worker does the read.

| protection | enforced in | shown by |
|---|---|---|
| Entity lookup by name on the chat page (`verify` operation `find`): a read only, at most 10 rows of entity id, friendly name and state, no password-mode text entities, no credential-like names, valid only for the running message's capability and inside its check limit | `verification.py`: `Verification.find`, `find_row`, `MAX_FIND_MATCHES`; filters shared from `relay_reads.py`: `PRIVATE`, `password_state` | `tests/test_cx_parity.py` (`DeviceCommandSpeedTests`), `tests/test_verification.py` |
| Relay state reads: listed entities and attributes only, no password-mode text entities, no credential-like names, bounded size, valid only while the relay that was given the capability is alive | `relay_reads.py`: `RelayReads._dispatch`, `PRIVATE`, `ATTRIBUTES`, `password_state` | `tests/test_relay_read_filters.py`, `tests/test_relay_request_shapes.py` |
| Recorder history: read-only, per exchange, limited in count | `ha_data.py`: `HomeAssistantData.dispatch` | `tests/test_ha_data.py` |
| `exchange_info`: returns only the details the worker recorded for the running exchange (backup folder, attachments, notes); it takes no arguments, reads no Home Assistant data and needs the same capability as the recorder reads | `ha_data.py`: `HomeAssistantData.read_exchange_info`; `server.py`: `exchange_info` | `tests/test_app_thread_store.py` |
| `ask_owner` and `report_failure` set only the page's status, question and choices: the bridge answers them without the worker's socket; the worker uses a call only when it finished and keeps to the limits; a failure the worker finds (exit, timeout, stall, configuration check, refused edit) wins over both | `ha_data_mcp.py`: `record_status`, `call_tool`; `server.py`: `status_call`, `apply_status_calls`, `run_task` | `tests/test_app_thread_store.py` |
| GitHub: clone (into a new folder `/share/<folder>`, one name with no slash, `..` or leading dot; links are resolved first and nothing outside the two is accepted; `/share/HomeAssistant` is never touched), link (sets a plain https origin; no credential is written into the repository), fetch, pull (a fetch only: the worker never updates a work tree, so a filter planted in the repository never runs in the worker) and push for a repository the owner connected, per exchange; git runs in the worker with an askpass helper that answers only GitHub's own prompt, with repository settings limited to a short allowlist and hooks, fsmonitor, credential helpers and other transports switched off for every git child, and the effective push URL checked. A sandboxed shell can reach `/data/github.sock`, but it offers only this same tool, needs the running exchange's capability, and has no operation that returns the token | `github_access.py`: `GitHubService.dispatch`, `GitRunner.check_config`, `CONFIG_OVERRIDES`, `origin_repository`; `github_askpass.py`; `github_mcp.py` | `tests/test_github_access.py` (`test_planted_repo_config_cannot_run_or_redirect`) |
| Diagnostics export: an allowlisted schema, nothing else leaves | `diagnostics_scrub.py` | `tests/test_interaction_diagnostics.py`, `tests/ha/test_diagnostics_export.py` |

`diagnostics_scrub.py` exists twice, in `codex-cli-worker/` and in `custom_components/codex_cli/`, because the
two are packaged separately. The copies must be byte-identical and a worker test fails when they are not.

## Checking it yourself

- Run the worker tests: `python -m unittest discover -s codex-cli-worker/tests` (they need `aiohttp`, `flask`, `requests`,
  `websocket-client` and `PyYAML`; `.github/workflows/validate.yml` shows the exact setup).
- The protected names live in one place: `codex-bwrap` (`HIDDEN_FILES`, `HIDDEN_DIRS`) hides them from every
  sandboxed command. A command allowed once runs outside the sandbox, and no check of the add-on's own stands
  behind that.
- The image places Codex's managed requirements and the policy read-only under `/etc/codex` and
  `/usr/share/ha_codex`; the `Dockerfile` is where to confirm that.

## Keeping this page true

A pull request that moves a protection, adds one, or changes what a row's tests cover updates this page in the
same change. Reviews check for that.
