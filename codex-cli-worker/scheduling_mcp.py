"""Stdio tools for schedule proposals and the schedule list, never approvals."""
from __future__ import annotations
import json
import os
import socket
import sys
from typing import Any

import tool_threads

PROPOSAL: dict[str, Any] = {'type': 'object', 'additionalProperties': False,
            'properties': {'name': {'type': 'string', 'maxLength': 80},
                           'prompt': {'type': 'string', 'maxLength': 16000},
                           'mode': {'enum': ['default', 'read-only']},
                           'spec': {'type': 'object', 'description': 'interval: kind/seconds; cron: kind/expression (five numeric UTC fields); once: kind/at (ISO with offset).'}},
            'required': ['name', 'prompt', 'spec']}
# No minimum or maximum here: a limit outside 1-20 is clamped by the service
# (a larger one returns 20 items and a next_offset), never refused.
LIMIT: dict[str, Any] = {'type': 'integer', 'description': 'Items per page, 1-20 (default 10). A larger number returns 20 items and a next_offset to continue from.'}
TOOLS: list[dict[str, Any]] = [
    {'name': 'propose_schedule', 'description': 'Save a schedule proposal for owner review. It remains disabled until a Home Assistant administrator approves its exact current contents on the page. Cannot approve or activate it.', 'inputSchema': PROPOSAL},
    {'name': 'list_schedules', 'description': 'Read a bounded page of schedule summaries. This does not enable or change schedules.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100}, 'limit': LIMIT}}},
]


def fetch(name: Any, arguments: Any, thread_id: str) -> Any:
    """Send one tool call to the add-on's scheduling socket and return its decoded answer.

    The name and the arguments come from Codex unchecked, so they are checked here first; each refusal is a
    ValueError with a fixed sentence.
    """
    tool = next((t for t in TOOLS if t['name'] == name), None)
    if tool is None:
        raise ValueError('Unknown scheduling tool; the tools are ' + ', '.join(t['name'] for t in TOOLS) + '.')
    allowed = tool['inputSchema']['properties']
    if not isinstance(arguments, dict) or set(arguments) - set(allowed):
        raise ValueError(f'{name} accepts only these arguments: ' + ', '.join(allowed) + '.')
    data = json.dumps({'operation': name, 'arguments': arguments, 'thread_id': thread_id}).encode() + b'\n'
    if len(data) > 65536:
        raise ValueError('The request is larger than 64 KiB.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(os.environ.get('CODEX_SCHEDULE_SOCKET', '/data/schedules.sock'))
        client.sendall(data)
        with client.makefile('rb') as response:
            raw = response.readline(65537)
    if not raw or len(raw) > 65536:
        raise ValueError('The scheduling service sent no usable answer.')
    return json.loads(raw)


def respond(request: dict[str, Any]) -> dict[str, Any]:
    """Return the result of one MCP request; raise ValueError for a method this server does not have."""
    method = request.get('method')
    if method == 'initialize':
        return {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codex_scheduling', 'version': '1'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        try:
            params = request.get('params', {})
            result = fetch(params.get('name'), params.get('arguments', {}), tool_threads.thread_id_from_call(params))
        except OSError:
            result = {'ok': False, 'error': 'The scheduling service is not reachable; the add-on may be restarting.'}
        except json.JSONDecodeError:
            # A JSONDecodeError is a ValueError, so it is caught first.
            result = {'ok': False, 'error': 'The scheduling service sent no usable answer.'}
        except ValueError as exc:
            # fetch() raises only the fixed sentences above.
            result = {'ok': False, 'error': str(exc)}
        except (TypeError, AttributeError):
            result = {'ok': False, 'error': 'A tool call needs params with a name and an arguments object.'}
        return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': not result.get('ok')}
    raise ValueError('Unsupported method')


def main() -> int:
    """Answer MCP requests, one JSON line each, on stdin and stdout until stdin closes."""
    while raw := sys.stdin.buffer.readline(65537):
        if len(raw) > 65536:
            return 1
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or 'id' not in request:
                continue
            response = {'jsonrpc': '2.0', 'id': request['id']}
            try:
                response['result'] = respond(request)
            except (ValueError, TypeError, AttributeError):
                response['error'] = {'code': -32601, 'message': 'Unsupported request'}
        except (ValueError, UnicodeError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON'}}
        print(json.dumps(response), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
