# Sandbox compatibility workaround and verification

This worker includes a temporary workaround for [upstream Codex issue #44329](https://github.com/openai/codex/issues/44329), reported here as [issue #9](https://github.com/moryoav/home-assistant-codex/issues/9).

## Scope

The `codex-bwrap` wrapper recognizes Codex's internal, harmless `/bin/true` or `/usr/bin/true` proc-mount probe by its namespace flags and exact command after `--`. Only that probe uses `codex-bwrap-probe.py`. On failure, the helper changes complete, recognized `Can't mount proc on /proc` diagnostic lines to `Can't mount proc on /newroot/proc`, preserving the original failure status. This lets Codex select its own no-proc fallback.

No Bubblewrap arguments are removed, permissions are not expanded, and normal task commands continue through the original direct `exec` path with `--cap-drop ALL`. That proc-mount workaround does not change the CLI version, model choices, integration or sandbox defaults. The separate relay profile design is described below. There is no automatic downgrade to `danger-full-access`.

The no-proc fallback is not identical to mounting a fresh `/proc`: it retains the other sandbox restrictions but can expose some process metadata from the app container. This workaround does not add host PID access.

## Readiness diagnostics

The worker now verifies a real Codex sandbox execution in addition to the raw Bubblewrap namespace check. It uses the selected `read-only` or `workspace-write` mode, `/config` working directory, and worker environment. The command is `/bin/true`, not an AI task. Errors and timeouts leave readiness false and block task launch; login and diagnostics remain accessible.

The execution probe has its own 20-second timeout (`CODEX_SANDBOX_PROBE_TIMEOUT_SECONDS`), separate from the 5-second raw Bubblewrap probes. It starts Node, the native CLI, and several Bubblewrap processes, so slow or busy hardware needs more time, and a timeout would otherwise block every task.

In `/health`, `sandbox_readiness.namespace_probe.codex_probe` reports the actual execution result when the raw namespace check succeeded. The standalone `proc_probe` is still included, even when Codex fails. A failed standalone proc mount with successful Codex execution is allowed, but the log no longer promises that a fallback was used merely because a raw namespace test passed.

`danger-full-access` keeps its existing behavior and skips sandbox probes.

## Automated tests

Run the standalone worker tests from the repository root:

```sh
python -m unittest discover -s codex-cli-worker/tests -v
```

For the full suite, including the Home Assistant integration tests, run `pytest`
in a Linux or WSL environment with the dependencies described in
[Testing](../CONTRIBUTING.md#testing).

The fake-Bubblewrap tests cover both true-command paths, recognized error reasons and line endings, unchanged unrelated errors, successful probes, capability dropping, preserved descriptors and signal termination, direct-exec PID identity, help/version calls, and task arguments that must not be mistaken for the internal probe. Readiness tests cover both sandbox modes, real-probe failures despite raw namespace success, timeouts, missing CLI, and unrestricted-mode bypass.

These tests do not prove HAOS/AppArmor compatibility or filesystem isolation on the affected host.

### HAOS parser and policy ABI

HAOS 16–18.5 pins AppArmor 3.1.7. The profile declares `abi <abi/3.0>,` so
domain/stack and network_v8 mediation participate in compilation. Ptrace was
already mediated before this declaration. In 3.1.7 an untagged
profile uses a legacy policy ABI without `domain/stack`; even when the kernel
supports stacks, the change-profile rule then fails with "does not support
stacking of named transitions". A literal target alone does not fix it.
Do not pass a policy-feature override to the compatibility check: it hides
this missing declaration. The exact relay-only target stays unchanged.

The 3.1.7 source is the HAOS buildroot release tarball, SHA-256 checked against
[the HAOS buildroot pin](https://github.com/home-assistant/buildroot/blob/5c344b3789e3/package/apparmor/apparmor.hash).
Tests compile the actual shipped source with local and repository-prefixed
Supervisor names, and remove only the ABI declaration for a failing control.
A local offline compile does not prove that the kernel accepts a load.

The `AppArmor 3.1.7 enforcement` CI job builds that pinned release and loads
the shipped policy on a disposable Ubuntu runner, using its real kernel
features. It tests both Supervisor prefixes and both production relay launch
routes. Pipe and PTY commands must run the actual guard under `NoNewPrivs=1`,
retain the relay plus tool stack, and deny reads of synthetic relay/sibling
proc views. A same-label positive control proves the synthetic marker was
readable before the stack. Removing only the stack permission must make the
guard refuse execution. Missing enforcement support fails the job; it never
silently skips. The job removes only its own profiles and fixture files.
This is loaded-policy evidence; the lead must still perform the HAOS version
update and enrolled phone acceptance below.

Both relay paths map outside root to UID/GID 1000 inside their outer user
namespace before `--cap-drop ALL`. The fallback omits UTS setup but uses the
same mapping and verifies all capability sets are zero and NNP is set before
exec. No file ownership changes: a mode-0600 file owned by container root is
still owned by the relay's kernel UID. Without the mapping, Linux's UID-0
CAP_SETFCAP requirement prevents the capability-free relay from creating a
nested user namespace. Do not retain capabilities to work around that failure.

The 3.1.7 job starts as real root, loads both Supervisor names, and checks both
routes with the image's SHA-256-verified CLI release bundle. Real `command/exec`
pipes and PTYs must read the root-owned `/config` fixture, refuse its write in
read-only, and allow its write in workspace-write. The inner UID/GID must be
1000, host ownership must remain 0:0, all five capability sets must be zero,
and NNP must remain 1. The named route must keep its private hostname without
changing the runner's hostname. The direct guarded commands additionally use
the existing proc view to verify denials even when outer PIDs are visible.

### Remote-control profile regression

The relay has its own nested AppArmor label. Relay tools retain that component and add the
further nested tool component as a stack while `NoNewPrivs=1` is already set. An immutable
`bwrap.real` guard performs the narrow `AA_CHANGE_STACK` operation, checks the actual label
and NNP, then execs the fixed native `bwrap.exec`. A refused or missing stack fails closed;
it never starts native Bubblewrap under only the relay label. The chat tool path retains
its existing profile. The public mask/proc wrapper is unchanged.

**Install a version update before testing an AppArmor change.** On the lab's Supervisor,
`ha apps rebuild` does not reload the profile. A same-version rebuild does not reload it. The release/lead must use a bumped candidate
version and an add-on version update, then confirm the os-agent profile-load record. Do not
count a rebuild against a stale loaded profile as a test of this policy. This card changes
no version itself. The HAOS worker container is `app_local_codex_cli_worker`, not an `addon_`
prefix. If the existing SSH add-on has no Docker CLI/socket, stop that step and report
not run; do not disable its protection or add an unreviewed endpoint to get around it.
No `aa-exec` or source tests are needed in the image. From the lead's SSH add-on shell, select the
candidate worker container (not a host-wide profile change) and run:

```sh
docker exec app_local_codex_cli_worker python3 /relay_diagnostics.py --profile local_codex_cli_worker
docker exec app_local_codex_cli_worker python3 /relay_diagnostics.py --profile local_codex_cli_worker --plain
```

Use the actual Supervisor profile name if it differs from `local_codex_cli_worker`. The probe
requires that actual enforcing parent label, checks the post-setup relay label and all-zero
capability sets, then exercises the pinned app-server's `command/exec` pipe and PTY paths in both
sandbox modes. Both pipe and PTY commands must report `nnp: "1"` and an enforcing label with exactly these
two components (their printed order may differ):

- `local_codex_cli_worker///opt/codex-sandbox/relay-launcher`
- `local_codex_cli_worker///opt/codex-sandbox/relay-launcher///opt/codex-sandbox/bwrap.real`

The separator is `//&`. A singleton relay label, singleton tool label, missing NNP or any
readable/writable protected probe is a failure. The two modes produce four JSON lines,
including labels/NNP/errnos for PTY as well as pipes. These are actual command results,
not labels inferred from the profile text. The probe attempts only synthetic reads through the
fixed control parent and the relay's `/proc/<pid>/root`, `cwd` and `environ`, plus a synthetic
write through `cwd`. It fails if any is accessible. The stacked tool component also denies ptrace
reads of a sibling with the same relay+tool stack; `/proc/self` still works.
This means process inspection tools can see fewer sibling details than on the
single-label chat path. This restriction is intentional. Errno 2 means the PID/path is hidden; errno
13/1 means denied, and errno 30 means read-only. Capture those results and the host's AppArmor
DENIED records to distinguish namespace hiding from an enforced cross-label denial. In particular,
repeat on the affected HAOS no-proc fallback where outer PIDs are visible. A pass on a fresh proc
mount alone does not prove that fallback. Do not attach credential paths or audit data containing
private arguments to the public PR.

The probe ships in the worker image, uses a temporary synthetic Codex home and passive app-server,
and never enables remote control, signs in, requests a pairing code or calls Home Assistant. The
control-mask parent must already exist at startup while remote control is off; the probe does not
create it. The normal launch path and `--plain` fallback must both pass. A fallback that cannot
create its safe user mapping or drop its bounding set fails closed. Do not relax AppArmor or give the worker new privileges to
make it pass.

For an account-free Linux regression with the pinned CLI installed separately, run:

```sh
HA_TEST_RELAY_NAMESPACE=1 HA_TEST_CODEX=/path/to/pinned/codex python3 -m unittest discover -s codex-cli-worker/tests -p test_rc_exec_f.py -v
```

This native test uses the production outer mount flags, with a local Bubblewrap path, and no loaded
HAOS profile. Its negative removes only the fresh `--dev /dev` and shared-memory bind: `command/exec` must fail. The positive
covers pipes and PTYs in read-only, workspace-write and a disposable unmanaged outside-sandbox
fixture. The latter is not a native approval or a managed-policy bypass. Local syscall tracing
identified `EACCES` opening `/dev/null` and `/dev/ptmx`; the old inherited-exec rule was a no-op.
The outer relay now uses upstream's minimal `--dev /dev` plus
`--bind-try /dev/shm /dev/shm`, rather than exposing all container devices.

Launcher failures name only a fixed setup stage: relay AppArmor label entry, capability
removal/verification, relay-tool stack transition/verification or fixed binary exec. No
argument, environment value, raw exception or caller-supplied label is printed. A missing
relay label after an update should first be checked against the actual profile-load record.

Kernel references for the NNP invariant and stack operation:
[exec NNP subset check](https://github.com/torvalds/linux/blob/v6.18/security/apparmor/domain.c#L978),
[stack proc attribute](https://github.com/torvalds/linux/blob/v6.18/security/apparmor/lsm.c), and
[HA Supervisor profile-name adjustment](https://github.com/home-assistant/supervisor/blob/main/supervisor/utils/apparmor.py).
Supervisor rewrites the root declaration only. The stack target is computed from the
kernel's current relay label; the permission pattern uses AppArmor's `profile_name` expansion.
The image does not use a hard-coded local/repository prefix or an unconfined transition.

The lead must additionally use the real phone relay to deny an escalation, allow a fresh one once,
verify the ordinary sandboxed read and Core API call using configured variables, and repeat after
an add-on image update: the app should reconnect with the same enrollment and no new pairing code.
The key is relay URL + account + stable initialize client name `ha_codex`, not the display/UTS name.
No synthetic fixture is evidence of a real enrolled reconnect.

## HAOS verification before release

For changes to the wrapper or pinned CLI, build and install a candidate worker image in a test environment, keeping the existing protection and AppArmor settings. On the affected HAOS installation, verify both `read-only` and `workspace-write`; a successful `danger-full-access` run does not test the sandbox.

From a shell **inside the worker container**, the basic execution checks are:

```sh
export CODEX_HOME=/data/codex-home HOME=/data
cd /config
/usr/local/bin/codex sandbox --config 'sandbox_mode="read-only"' -- /bin/true
/usr/local/bin/codex sandbox --config 'sandbox_mode="workspace-write"' -- /bin/true
```

The pinned CLI (0.165.0) takes the command directly after `codex sandbox [options] --`. It has no platform subcommand: `codex sandbox linux ...` tries to run a program named `linux` and fails with `Failed to execvp linux`. Re-check this grammar, and the worker's readiness probe that uses it, whenever `CODEX_VERSION` in the Dockerfile is bumped.

Both should exit successfully. Then run the original issue's read-only inspection task and confirm actual shell execution, not just an assistant reply. In a disposable test setup, use new temporary files to verify that `read-only` denies writes, `workspace-write` allows a test write under `/config`, and writes outside configured writable roots (for example a new test file under `/data`) are still denied. Do not use existing HA configuration or credential files for write tests. Also check normal task cancellation.

Repeat the basic checks on a host where fresh `/proc` mounting succeeds. A successful `/bin/true` check alone is not proof that all write restrictions are enforced.

## Removing the workaround

Once the pinned upstream CLI recognizes both proc error formats, remove the wrapper's special-probe path, the helper, and its Docker copy. Retain the real Codex readiness check and fail-closed tests. Verify the affected HAOS setup before removing compatibility code or declaring issue #9 resolved.


### ABI socket-family change and namespace fallback

On HAOS's kernel features, ABI 3.0 enables network_v8 mediation for both chat
and relay tool labels. Those labels retain Unix sockets and raw netlink only;
AF_INET/AF_INET6/packet socket creation in sandboxed commands is refused.
Approved escalations retain the existing parent/relay policy. The lead must
run one ordinary chat-page sandboxed shell read plus the history MCP tool on
HAOS after a profile reload. Runner tests do not substitute for that acceptance.

The plain relay route omits private UTS setup, but still needs a user namespace
and the non-root mapping. If user namespace creation itself is denied, the
relay fails closed and retries with backoff; no working fallback is promised.

The loaded-policy direct rows know the relay pid is visible and require
errno 13. The pinned command/exec rows can instead hide that pid in Codex's pid
namespace, where errno 2 is not evidence of ptrace enforcement. Ubuntu runner
kernel features, including its socket mediation, differ from HAOS. N10 now
requires EACCES (13) on every direct-row proc read. Pinned command/exec rows
retain accepted denials 1, 2 and 13. The named lead exception permits only this
assertion in the existing CI helper; loaded-policy CI proves it on real profiles.
