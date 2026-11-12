# Core address discovery and first-run remote reads

CX-HA-URL implementation plan, recorded before code changes. No live Core, account or lab acceptance is performed by the outside lane.

## Address and credentials

At worker startup and each task or relay launch, read Supervisor `/core/info` with the worker's own Supervisor credential. Validate an integer port from 1 through 65535 and a boolean SSL flag, then form Core's service address with that scheme and port. Keep the credential in the worker; never copy it to a task, relay or MCP child. Refuse redirects and bound the request timeout and response size. A missing or invalid reading stays unavailable; do not invent a default port.

The `ha_url` option defaults to empty and is an explicit override. Existing saved values equal to the former default or former Supervisor proxy default are unset. A valid override wins without supplying credentials. The resolved Core address is exported as HA_URL even when HA_TOKEN is absent. A manually configured HA_TOKEN remains optional and separate; native approval never supplies it. The page reports discovery/override/unavailable honestly.

## First-run path

The owner turns on remote control and pairs normally. Remote threads receive a separate read-only MCP tool for bounded entity listings and state readback. No hand-made Core token is needed for those reads: the worker makes only fixed GET requests through its existing Supervisor Core proxy. The relay gets a temporary capability, not the Supervisor token. It rotates for each relay launch, is usable only while that process is active, and is refused immediately when stopping or after replacement. Page-task verification/history capabilities are never reused.

The bridge accepts only the documented read operations and explicit validated fields. It cannot call a service, reload, run a command, follow an arbitrary URL, read credentials or answer a permission request. Bound payloads, read timeouts, result size and concurrent reads; redact recognized credentials and discard a result if its capability expired while the request ran. The MCP subprocess runs outside the shell sandbox, like the existing page verification bridge, while the shell's network policy and command gate stay unchanged.

Relay instructions prefer this read tool. For a read outside its supported operations, Codex must request native approval for that specific network command; an ordinary network denial must not become a claim that Core is unavailable. Direct commands still need a separately configured Core credential. The first-run read path does not grant service-write authority or change the #22/#27 gate and native approval flow. Permission requests for writes and other escalations remain native human decisions.

## Review nits and acceptance

- N8: correct the ABI explanation: domain/stack and network_v8 become mediated; ptrace was already mediated. Document the tool-label socket-family restriction. The ordinary chat-path HAOS acceptance remains the lead's.
- N9: document that the plain relay route still requires a user namespace and fails closed if creation is denied; it is not a promise to work without user namespaces.
- N10: the requested stricter errno assertion is in tests/apparmor_317_check.py, an existing CI helper. Outside brief §4 forbids CI/helper edits. Leave it byte-identical and report this scope conflict to the lead; do not infer an exception from the earlier, different card's CI permission.

Tests cover discovery on the default, alternate and custom ports, SSL, old defaults, overrides and unavailable/malformed readings; refresh on actual launch paths; first-run reads with no HA_TOKEN; malformed/mutation requests and token leakage; capability rotation, stop and in-flight expiry; and unchanged native policies. Use local synthetic Core/Supervisor responses and sockets only. Container and pinned CLI checks use the repository jobs; local container smoke is not run because of VFS disk.

## Scratch-folder proposal (do not build)

A future owner-controlled notes folder under the worker's persistent data tree could keep Codex's own notes out of `/config`. It needs an explicit retention/size policy, a narrow sandbox bind and review of the existing `/data` masks and self-edit refusals. It must not share the credential/control folders. Do not add that folder or widen any bind in this card. Temporary analysis can continue in the existing ephemeral scratch space; durable notes require a separate scoped change and owner choice.
