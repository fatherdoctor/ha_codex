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
sandbox whose launcher the add-on wraps (`codex-bwrap`). Before any tool call, Codex runs a hook the add-on
ships in the image (`gate/gate_hook.py`), which can refuse but never approve. Anything the hook does not refuse
goes to Codex's normal approval flow, and an approval has to be answered by a Home Assistant administrator on
the chat page. After every exchange the worker compares `/config` with what it was before. A separate, optional
process connects the ChatGPT app to Codex through OpenAI's relay ("remote control"); it has stricter rules
because nobody is at the chat page to answer prompts.

So there are four layers, and no single one is meant to hold alone:

1. **What Codex's process can see** - its environment and the sandbox's file view.
2. **What the hook refuses** - a text check on each command and patch.
3. **What a person must approve** - and who counts as that person.
4. **What the worker checks afterwards** - the config scan, the config check, the backups.

## Where each protection lives

Paths are under `codex-cli-worker/` unless they start with `custom_components/`.

### Layer 1: what Codex can see

| protection | enforced in | shown by |
|---|---|---|
| The Supervisor token and the worker's own capabilities are not in Codex's environment | `server.py: codex_env` | `tests/test_server.py`, `tests/test_sandbox_readiness.py` |
| The remote-control process gets an environment without the Supervisor token or per-exchange capabilities | `remote_control.py: STRIPPED_ENV`, `remote_env` | `tests/test_remote_control.py` |
| A sandboxed command cannot see `secrets.yaml`, Home Assistant's sign-in store, the add-on's options, its API token, the schedule database or the container's saved environment | `codex-bwrap: HIDDEN_DIRS`, `HIDDEN_FILES` | `tests/test_bwrap.py`, `tests/apparmor_317_check.py` |
| The one case where the wrapper changes Bubblewrap's arguments (Codex's own `/bin/true` probe) | `codex-bwrap: is_codex_proc_probe`, `codex-bwrap-probe.py`; explained in `SANDBOX_TESTING.md` | `tests/test_bwrap.py`, `tests/test_proc_assertion.py` |
| The relay runs under its own AppArmor label with all capabilities dropped, entered only through one fixed launcher | `apparmor.txt`, `relay-launcher`, `relay-tool-launcher` | `tests/apparmor_317_check.py`, `.github/workflows/apparmor-317.yaml` |
| The worker's memory cannot be read by a process it starts | `server.py: make_worker_undumpable` | `tests/test_gate.py` (`test_the_flag_is_set`) |
| Full access is not available, whatever a client or an option asks for | `gate/requirements.toml` (Codex's managed requirements) and `policy.py: load_policy`, `policy_problem`; checked before each exchange at the top of `server.py: run_task` | `tests/test_policy.py` |

### Layer 2: what the hook refuses

| protection | enforced in | shown by |
|---|---|---|
| The hook runs for every tool call and permission request, from every client, and a hook planted in `/config` cannot replace it | `gate/managed_config.toml`, `gate/requirements.toml` (`allow_managed_hooks_only`) | `remote_control.py: requirements_problem`, `hook_problem`, `gate_problem` refuse to start remote control when these are not in place |
| A request the hook cannot read or judge is refused, not passed | `gate/gate_hook.py: main` | `tests/test_gate.py` |
| The hook answers before its own deadline, because Codex runs the action if a hook is still working when its timeout passes; an answer found after the deadline is refused | `gate/gate_hook.py: DEADLINE_SECONDS`, `wait_for_child`, `answer_before_deadline`; the Codex-side `timeout` in `gate/managed_config.toml` leaves room beyond it | `tests/test_gate.py` (`test_a_stuck_scan_is_refused_by_the_deadline`, `test_the_deadline_leaves_room_inside_the_codex_timeout` and their neighbors) |
| Commands that read a process environment, use the Supervisor token, talk to the Supervisor, or read the sign-in store or `secrets.yaml` are refused (*) | `gate_rules.py: check_tool_call`, `PROTECTED_COMMAND_RES`, `_ha_supervisor_command` | `tests/test_gate.py`, `tests/fixtures/gate_tool_calls.json` |
| Edits that add a command which can reach anything (`shell_command`, `rest_command`, `python_script`, ...), a platform that runs on load, or a templated action are refused | `gate_rules.py: find_refused`, `STRICT_RULES`, `policy/refused-domains.json` (the file can add domains, never remove the built-in ones: `load_rules`) | `tests/test_gate.py` |
| With remote control on: a patch is rebuilt in memory and refused if the result holds something new that is refused, or if it cannot be rebuilt with certainty | `gate_rules.py: patch_versions`, `new_refused`, `refused_blocks` | `tests/test_gate.py` |
| With remote control on: the add-on's settings, rules, policy files, schedule database and API token cannot be named in a command | `gate_rules.py: _policy_restriction` | `tests/test_gate.py` |
| Files the checks read are opened without following links and with a size limit | `gate_rules.py: read_config_bytes` | `tests/test_gate.py` |

The hook reads text. It does not run a shell, so a path or command built while the command runs is out of its
reach; DOCS.md lists those limits, and layers 1, 3 and 4 are what cover them.

(*) Known weakness: the shell text is split by two copies of the same code, `_strip_comments` and `_split_words`,
one in `gate_rules.py` and one in `approvals.py`. Both refuse quoting they cannot read. Nothing but review keeps
the copies the same, so check both when you check one.

### Layer 3: who can approve

| protection | enforced in | shown by |
|---|---|---|
| The chat page is reachable only through Home Assistant Ingress; a request that claims to be Ingress from any other address is rejected | `server.py: enforce_ingress_boundary`, `is_ingress_request` | `tests/test_server.py` |
| Every other route needs the worker's API token, compared in constant time | `server.py: require_auth` | `tests/test_api.py`, `tests/test_server.py` |
| The API token (held only by the integration) can never answer a permission request; only an active Home Assistant administrator signed in on the chat page can, and a failed lookup refuses | `server.py: ingress_admin`, `lookup_ha_users`, `decide_approval` | `tests/test_approvals.py` |
| A native approval covers one action in the current exchange; it installs no rule and expires with the exchange. Standing permission exists only as the administrator's pre-approved action groups, in the next rows | `approvals.py`: `AppServerSession.decide` and `.receive` | `tests/test_approvals.py`, `tests/test_approval_smoke.py` |
| Pre-approved action groups: an administrator can switch on groups of Home Assistant actions (`apply`, `everyday` and `organize` start on; `bigger` and `security` start off), and Codex then performs them through `home_assistant_actions.act` with no per-action approval. The `security` group (locks, alarm, covers, cameras, notifications) is off unless the administrator turns it on | `permission_overrides.py: DEFAULTS`, `SECURITY`, `SERVICES`, `OPERATIONS`; entity targets and action data are bounded (`entity`, `bounded_json`) and templates need normal approval | `tests/test_permission_overrides.py`, `tests/test_permission_overrides_direct.py`, `tests/test_permission_overrides_runtime.py` |
| Only an exchange the worker itself launched can use the groups: authority is built from the worker's own launch policy and never from MCP input, and anything unknown asks for normal approval. Remote-control turns cannot use the groups in this version | `permission_overrides.py: Authority.mode`; `permission_overrides_api.py` (where `Authority` is built from the launch policy) | `tests/test_permission_overrides_final_authorization.py`, `tests/test_permission_overrides_review.py` |
| Owner guard-rail lines (no locks, alarm, covers, cameras, messages or external publishing) are read from one marked block in the agent's rules and refuse the matching actions even when the group is on; a custom line or a block that cannot be verified asks for normal approval | `permission_overrides.py: GUARD_LINES`, `selected_guard_lines`, `guard_rules` | `tests/test_permission_overrides_peer2.py`, `tests/test_permission_overrides_review.py` |
| The groups can be changed only by an administrator on the chat page; the settings file is small, exactly five booleans, and a bad file means normal approval | `permission_overrides_api.py`: the `/permissions-overrides` route (`require_auth`, `require_ingress`, `ingress_admin`); `permission_overrides.py: Settings.read`, `validate` | `tests/test_permission_overrides.py` |
| A change to the add-on's own policy, rules, options or credentials cannot be approved at all | `approvals.py: touches_protected_path`, `command_names_protected_path`, `PROTECTED_PREFIXES` | `tests/test_approvals.py` |
| Browser permissions are managed only from the chat page | `server.py: require_ingress` | `tests/test_browser_permissions.py` |
| Schedules and the mailbox are owner-managed: the page routes need the worker's API token and an administrator on the chat page (`owner`). The local lead channel under `/lead/` is separate: it needs its own token, not Ingress, and reads only background work, not people's chats. The scheduling tools a chat-page task can call use a third path: a Unix socket (mode 0600) where each request must carry the exchange's running-turn capability, which ends with the exchange; a proposal is saved disabled until an administrator approves it | `schedule_api.py: register` (`owner`, `lead`), `schedule_api.py: Service.dispatch`, `Service.serve`, `scheduling.py: Store.token_ok` | `tests/test_scheduling.py` (`test_lead_requires_separate_token_and_ingress_alone_is_not_a_token`, `test_tool_transport_roundtrip_and_expired_capability`), `tests/test_sched_review.py` |

### Layer 4: what the worker checks afterwards

| protection | enforced in | shown by |
|---|---|---|
| After every exchange, including a cancelled or failed one, changed files are compared with the manifest taken before it, and an edit that plants a refused command is undone and reported | `server.py: run_task` (after the process is reaped), `refuse_planted_edits`, `build_manifest`, `diff_manifests` | `tests/test_backups.py`, `tests/test_server.py` |
| Changed YAML is validated and Home Assistant's own config check is run | `server.py: validate_changed_files`, `check_home_assistant_config` | `tests/test_config_check.py` |
| A copy of each changed file is kept, without credentials | `server.py: create_snapshot`, `saved_copy`, `drop_credential_backups` | `tests/test_backups.py` |
| With remote control on, `/config` is watched for refused content that arrived by a route the hook cannot see, and a Home Assistant repair is raised | `remote_control.py: ConfigWatch` | `tests/test_remote_control.py` |

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
| Relay state reads: listed entities and attributes only, no password-mode text entities, no credential-like names, bounded size, valid only while the relay that was given the capability is alive | `relay_reads.py`: `RelayReads._dispatch`, `PRIVATE`, `ATTRIBUTES`, `password_state` | `tests/test_relay_read_filters.py`, `tests/test_relay_request_shapes.py` |
| Recorder history: read-only, per exchange, limited in count | `ha_data.py`: `HomeAssistantData.dispatch` | `tests/test_ha_data.py` |
| Diagnostics export: an allowlisted schema, nothing else leaves | `diagnostics_scrub.py` | `tests/test_interaction_diagnostics.py`, `tests/ha/test_diagnostics_export.py` |

`diagnostics_scrub.py` exists twice, in `codex-cli-worker/` and in `custom_components/codex_cli/`, because the
two are packaged separately. The copies must be byte-identical and a worker test fails when they are not.

## Checking it yourself

- Run the worker tests: `python -m unittest discover -s codex-cli-worker/tests` (they need `aiohttp`, `flask`, `requests`,
  `websocket-client` and `PyYAML`; `.github/workflows/validate.yml` shows the exact setup).
- Ask the hook directly. It reads one event on standard input and exits 2 with a reason when it refuses, 0 with
  no output when it has no opinion:

  ```
  echo '{"hook_event_name":"PreToolUse","tool_input":{"command":"cat /config/secrets.yaml"}}' \
    | python3 -I codex-cli-worker/gate/gate_hook.py --state /nonexistent
  ```

- Compare the protected names. `codex-bwrap` (`HIDDEN_FILES`), `approvals.py` (`PROTECTED_PREFIXES`) and
  `gate_rules.py` (`_policy_restriction`) each list them in their own notation for their own layer.
- The image places the hook, the rules and the policy read-only under `/usr/share/ha_codex` and `/etc/codex`;
  the `Dockerfile` is where to confirm that.

## Keeping this page true

A pull request that moves a protection, adds one, or changes what a row's tests cover updates this page in the
same change. Reviews check for that.
