"""Bounded stdio bridge for worker-verified, owner-selected Core actions."""
import json
import os
import socket
import sys
from permission_overrides import OPERATIONS

TOOL = {
    'name': 'act',
    'description': 'Perform a supported Core action only when the administrator enabled its group and this exchange permits writes. Owner guard rails and hard refusals always apply. Name every successful performed action in your answer. If approval_required, use normal native approval; never enable a group yourself. If do_not_retry, inspect the outcome before attempting anything again. Remote turns require normal approval in this version.',
    'inputSchema': {'type': 'object', 'additionalProperties': False,
                    'properties': {'operation': {'type': 'string', 'enum': sorted(OPERATIONS)},
                                   'target': {'type': ['string', 'array', 'object', 'null']},
                                   'service': {'type': 'string'}, 'data': {'type': 'object'}},
                    'required': ['operation']},
    'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': False, 'openWorldHint': False},
}


def act(arguments):
    if not isinstance(arguments, dict) or set(arguments) - set(TOOL['inputSchema']['properties']):
        raise ValueError()
    encoded = json.dumps({'arguments': arguments, 'capability': os.environ.get('HA_DATA_CAPABILITY', '')}, allow_nan=False).encode() + b'\n'
    if len(encoded) > 1024 * 1024:
        raise ValueError()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(230)
        client.connect(os.environ.get('HA_OVERRIDE_SOCKET', '/data/permission-overrides.sock'))
        client.sendall(encoded)
        with client.makefile('rb') as stream:
            raw = stream.readline(1024 * 1024 + 1)
    if not raw or len(raw) > 1024 * 1024:
        raise ValueError()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError()
    return value


def respond(value):
    method = value.get('method')
    if method == 'initialize':
        return {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'home-assistant-actions', 'version': '1'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [TOOL]}
    if method != 'tools/call':
        raise ValueError()
    try:
        params = value.get('params', {})
        if params.get('name') != 'act':
            raise ValueError()
        result = act(params.get('arguments'))
    except Exception:
        # Transport loss may follow a dispatched write: never suggest retry or normal
        # approval for the same action until its current state has been checked.
        result = {'ok': False, 'approval_required': False, 'do_not_retry': True,
                  'reason': 'The action result is unavailable. Inspect its state before another attempt.'}
    return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': not result.get('ok')}


def main():
    while raw := sys.stdin.buffer.readline(1024 * 1024 + 1):
        if len(raw) > 1024 * 1024:
            return 1
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or 'id' not in value:
                continue
            result = {'jsonrpc': '2.0', 'id': value['id']}
            try:
                result['result'] = respond(value)
            except (ValueError, TypeError, AttributeError):
                result['error'] = {'code': -32601, 'message': 'Unsupported request'}
        except (ValueError, UnicodeError):
            result = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON'}}
        print(json.dumps(result), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
