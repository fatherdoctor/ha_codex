"""Bounded Core state reads for the active relay; credentials stay in the worker."""
from __future__ import annotations
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import threading

from core_address import get_json

ENTITY = re.compile(r'^[a-z_][a-z0-9_]*\.[a-z0-9_]+$')
DOMAIN = re.compile(r'^[a-z_][a-z0-9_]*$')
PRIVATE = re.compile(r'(?:^|_)(?:token|password|secret|credential|api_key|pin|code)(?:_|$)')
ATTRIBUTES = frozenset(('friendly_name', 'unit_of_measurement', 'device_class', 'supported_features',
                       'brightness', 'color_temp', 'current_temperature', 'temperature', 'hvac_modes',
                       'supported_color_modes', 'assumed_state', 'icon'))
MAX_RESPONSE = 65536


def fetch_states(path, token):
    if path != '/states' and not (path.startswith('/states/') and ENTITY.fullmatch(path[8:])):
        raise ValueError('Unsupported Core read path.')
    return get_json('http://supervisor/core/api' + path, token, 2 * 1024 * 1024)


class RelayReads:
    def __init__(self, state, token, redact, fetch=fetch_states):
        self.state = state; self.token = token; self.redact = redact; self.fetch = fetch
        self.lock = threading.Lock(); self.capability = ''
        self.inflight = threading.Lock()

    def environment(self, base):
        """Called only by relay launch/probe paths; every new launch rotates it."""
        with self.lock:
            self.capability = 'harc_' + secrets.token_urlsafe(32)
            return {**base, 'HA_RELAY_CAPABILITY': self.capability}

    def active(self, supplied):
        if not isinstance(supplied, str) or not supplied:
            return False
        with self.lock:
            if not hmac.compare_digest(supplied, self.capability):
                return False
        # Do not hold the capability lock while taking the supervisor's lock.
        proc, stopping = self.state()
        if proc is None or stopping or proc.poll() is not None:
            return False
        with self.lock:
            return hmac.compare_digest(supplied, self.capability)

    @staticmethod
    def entity_id(value):
        if not isinstance(value, str) or len(value) > 255 or not ENTITY.fullmatch(value) or PRIVATE.search(value.split('.', 1)[1]):
            raise ValueError('Use a noncredential entity id.')
        return value

    def clean(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        return value

    def dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {'capability', 'arguments'} or not self.active(request['capability']):
            raise ValueError('Inactive relay capability.')
        args = request['arguments']
        if not isinstance(args, dict):
            raise ValueError('Read arguments must be an object.')
        operation = args.get('operation')
        if operation == 'state':
            if set(args) - {'operation', 'entity_id', 'attributes'}:
                raise ValueError('Unsupported state fields.')
            entity_id = self.entity_id(args.get('entity_id'))
            attributes = args.get('attributes', [])
            if (not isinstance(attributes, list) or len(attributes) > 10
                    or any(not isinstance(key, str) or key not in ATTRIBUTES for key in attributes)):
                raise ValueError('Use only the documented observational attributes.')
            path = '/states/' + entity_id
        elif operation == 'states':
            if set(args) - {'operation', 'domain', 'offset', 'limit'}:
                raise ValueError('Unsupported entity-list fields.')
            domain, offset, limit = args.get('domain'), args.get('offset', 0), args.get('limit', 10)
            if domain is not None and (not isinstance(domain, str) or not DOMAIN.fullmatch(domain) or len(domain) > 64):
                raise ValueError('Invalid domain.')
            if type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError('Use a valid page offset and a limit from 1 through 50.')
            path = '/states'
        else:
            raise ValueError('Only entity listings and state readback are supported. No actions or arbitrary URLs.')
        if not self.inflight.acquire(blocking=False):
            raise ValueError('A relay read is already running. Try again later.')
        try:
            if not self.active(request['capability']):
                raise ValueError('Relay capability expired.')
            token = self.token()
            if not token:
                raise ValueError('Worker Core access is unavailable.')
            data = self.fetch(path, token)
            if operation == 'state':
                if not isinstance(data, dict) or data.get('entity_id') != entity_id or not isinstance(data.get('state'), str):
                    raise ValueError('Core returned an unavailable or invalid entity.')
                raw_attrs = data.get('attributes')
                if not isinstance(raw_attrs, dict):
                    raise ValueError('Core returned invalid attributes.')
                result = {'ok': True, 'entity_id': entity_id, 'state': data['state'][:512],
                          'state_truncated': len(data['state']) > 512,
                          'attributes': {key: raw_attrs[key] for key in attributes if key in raw_attrs}}
            else:
                if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                    raise ValueError('Core returned an invalid entity listing.')
                safe = []
                for row in data:
                    try:
                        candidate = self.entity_id(row.get('entity_id'))
                    except ValueError:
                        continue
                    if domain is None or candidate.split('.', 1)[0] == domain:
                        if not isinstance(row.get('state'), str):
                            raise ValueError('Core returned an invalid state.')
                        safe.append({'entity_id': candidate, 'state': row['state'][:512],
                                     'state_truncated': len(row['state']) > 512})
                safe.sort(key=lambda item: item['entity_id'])
                result = {'ok': True, 'entities': safe[offset:offset + limit], 'total': len(safe),
                          'next_offset': offset + limit if offset + limit < len(safe) else None}
            result = self.clean(result)
            if len(json.dumps(result).encode()) + 1 > MAX_RESPONSE:
                raise ValueError('Read response is too large. Request fewer attributes or a smaller page.')
            if not self.active(request['capability']):
                raise ValueError('Relay capability expired before readback finished.')
            return result
        finally:
            self.inflight.release()

    def serve(self, path):
        broker = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(10)
                try:
                    raw = self.rfile.readline(16385)
                    if len(raw) > 16384:
                        raise ValueError()
                    result = broker.dispatch(json.loads(raw))
                except Exception:
                    result = {'ok': False, 'error': 'Read unavailable, refused or expired. No action was performed.'}
                try:
                    self.wfile.write(json.dumps(result).encode() + b'\n')
                except OSError:
                    pass
        Path(path).unlink(missing_ok=True)
        service = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        service.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=service.serve_forever, daemon=True).start()
        return service
