"""Stdio bridge for the active relay's bounded, read-only Core state tool."""
from __future__ import annotations
import json
import os
import socket
import sys

ATTRIBUTES = ['friendly_name', 'unit_of_measurement', 'device_class', 'supported_features', 'brightness',
              'color_temp', 'current_temperature', 'temperature', 'hvac_modes', 'supported_color_modes', 'assumed_state', 'icon']
TOOL = {'name': 'read', 'description': 'Read Home Assistant entity ids and current states through the worker without a hand-made API token. This is observational: no service calls, reloads, arbitrary URLs or credential entities. Use states with an optional domain and bounded page; use state with an exact entity id and optional documented attributes. Truncated values are marked. This does not verify automation behavior.',
        'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {
            'operation': {'enum': ['states', 'state']}, 'entity_id': {'type': 'string', 'maxLength': 255},
            'attributes': {'type': 'array', 'maxItems': 10, 'items': {'enum': ATTRIBUTES}},
            'domain': {'type': 'string', 'maxLength': 64}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, 'required': ['operation']},
        'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}}


def read(arguments):
    if not isinstance(arguments, dict) or set(arguments) - set(TOOL['inputSchema']['properties']):
        raise ValueError('Unsupported read arguments.')
    raw = json.dumps({'capability': os.environ.get('HA_RELAY_CAPABILITY', ''), 'arguments': arguments}).encode() + b'\n'
    if len(raw) > 16384:
        raise ValueError('Read request is too large.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(os.environ.get('HA_RELAY_SOCKET', '/data/relay-read.sock'))
        client.sendall(raw)
        with client.makefile('rb') as stream:
            response = stream.readline(65537)
    if not response or len(response) > 65536:
        raise ValueError('Read response is unavailable or oversized.')
    return json.loads(response)


def respond(request):
    method = request.get('method')
    if method == 'initialize':
        return {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'home-assistant-relay-reads', 'version': '1'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [TOOL]}
    if method == 'tools/call':
        try:
            params = request.get('params', {})
            if params.get('name') != 'read':
                raise ValueError()
            result = read(params.get('arguments', {}))
        except (OSError, ValueError, TypeError, AttributeError):
            result = {'ok': False, 'error': 'Relay read unavailable or expired. No action was performed.'}
        return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': not result.get('ok')}
    raise ValueError('Unsupported method')


def main():
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
