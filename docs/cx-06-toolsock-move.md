# Tool transport extraction: preliminary note

This note is incomplete. On the reviewed release branch, `codex-cli-worker/tool_socket.py` and
`codex-cli-worker/mcp_stdio.py` do not exist. They are proposed extraction targets, so their present behavior
cannot be read. The proposed characterization test files for those modules do not exist on this branch either.
The move needs a decision about whether to characterize the existing implementations first.

## Existing boundaries

- `codex-cli-worker/verification.py` and `codex-cli-worker/ha_data.py` each provide a Unix socket server.
  Their handlers read one bounded JSON line, dispatch it, and write one JSON line. The verification handler
  sets a 240-second timeout; the data handler sets 60 seconds. Both restrict the socket to its owner.
- `codex-cli-worker/ha_call.py`, `relay_reads.py`, `schedule_api.py`, and `github_access.py` also serve tool
  sockets. Each has its own dispatch and refusal behavior.
- `codex-cli-worker/ha_call_mcp.py` reads bounded JSON-RPC input, sends responses on stdout, and formats
  action results. The other tool bridges have their own tool tables and result formatting.
- `codex-cli-worker/server.py` builds individual MCP configurations. They carry distinct server names,
  tool approvals, timeouts, required flags, and relay environment settings. The scheduler is conditional.

## Proposed sequence to confirm

1. Characterize each existing socket server and each MCP bridge before extraction. Include framing, byte
   limits, timeout, refusal, malformed input, connection close, result format, and end of stream.
2. Introduce a shared socket server with explicit dispatch, size, timeout, and refusal inputs. Switch one
   existing server at a time. Preserve socket ownership, daemon threads, cleanup, and the old responses.
3. Introduce a shared stdio loop. Keep each bridge's tool definitions, argument checks, server identity,
   and result formatter. Check protocol errors, notifications, and output boundaries for every bridge.
4. Replace the MCP argument builders with a table only after recording their output. Preserve the names,
   executable and environment, scheduling switch, relay options, approval modes, and timeouts.
5. Run the existing tool and page tests and the new characterization tests after each move. Compare the
   configuration and protocol results with the recorded baseline.

## Open test mapping

The proposed socket and stdio characterization cards name tests for the new modules, but those modules
are the result of the later move. A test written against them now cannot capture current behavior. The
socket tests should guard step 2 and the stdio tests step 3 once their targets and baseline are agreed.
The server configuration needs separate coverage for step 4; the proposed two test files do not name it.
