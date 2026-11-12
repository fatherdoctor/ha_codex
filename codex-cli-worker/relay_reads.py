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
import time

from core_address import get_json, ResponseTooLarge

ENTITY = re.compile(r'^[a-z_][a-z0-9_]*\.[a-z0-9_]+$')
DOMAIN = re.compile(r'^[a-z_][a-z0-9_]*$')
PRIVATE = re.compile(
    r'(?:^|_)(?:pass|passwords?|passcodes?|passphrases?|codes?|usercodes?|pins?|'
    r'pincodes?|keys?|apikeys?|api_keys?|otps?|totps?|secrets?|tokens?|credentials?|pw)'
    r'\d*(?:_|$)')
ATTRIBUTES = frozenset(('friendly_name', 'unit_of_measurement', 'device_class', 'supported_features',
                       'brightness', 'color_temp', 'current_temperature', 'temperature', 'hvac_modes',
                       'supported_color_modes', 'assumed_state', 'icon'))
MAX_RESPONSE = 65536


class ReadError(ValueError):
    """A fixed worker-authored message safe to return to the caller."""


READ_FIELDS = frozenset(('operation', 'entity_id', 'attributes', 'domain', 'offset', 'limit'))


def operation_arguments(args, allowed, reason):
    """Ignore only known, inapplicable fields whose values carry no request."""
    for key, value in args.items():
        if key in allowed:
            continue
        empty = value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)
        if key not in READ_FIELDS or not empty:
            raise ReadError(reason)
    return {key: value for key, value in args.items() if key in allowed}


def fetch_states(path, token):
    if path != '/states' and not (path.startswith('/states/') and ENTITY.fullmatch(path[8:])):
        raise ReadError('Unsupported Core read path.')
    try:
        return get_json('http://supervisor/core/api' + path, token, 16 * 1024 * 1024)
    except ResponseTooLarge:
        raise StateResponseTooLarge('Core state response exceeds 16 MiB. Request a single entity instead.') from None


class StateResponseTooLarge(ReadError):
    pass


def password_state(row):
    attrs = row.get('attributes')
    return str(row.get('entity_id', '')).startswith(('input_text.', 'text.')) and (
        not isinstance(attrs, dict) or str(attrs.get('mode', '')).lower() == 'password')


class RelayReads:
    def __init__(self, state, token, redact, fetch=fetch_states, observer=None):
        self.state = state; self.token = token; self.redact = redact; self.fetch = fetch
        self.lock = threading.Lock(); self.capability = ''
        self.inflight = threading.Lock()
        self.observer = observer
        self.observation_session = None

    def environment(self, base):
        """Called only by relay launch/probe paths; every new launch rotates it."""
        self.end_observation()
        with self.lock:
            self.observation_session = secrets.token_hex(16)
            self.capability = 'harc_' + secrets.token_urlsafe(32)
            return {**base, 'HA_RELAY_CAPABILITY': self.capability}

    def end_observation(self):
        session = self.observation_session
        self.observation_session = None
        if session is not None and self.observer is not None:
            try:
                self.observer({'event': 'end', 'session': session}, None, None, time.time())
            except Exception:
                pass

    def finish_observation_if_stopped(self):
        proc, stopping = self.state()
        if proc is None or stopping or proc.poll() is not None:
            self.end_observation()

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
        """Keep observations separate from authorization and readback behavior."""
        observation = {}
        started = time.time()
        result, error = None, None
        try:
            result = self._dispatch(request, observation)
            return result
        except Exception as exc:
            error = exc
            raise
        finally:
            if (self.observer is not None and observation.get('authorized')
                    and observation.get('session') == self.observation_session):
                args = request.get('arguments')
                metadata = {k: args.get(k) for k in ('operation', 'entity_id', 'domain')} if isinstance(args, dict) else {}
                metadata['session'] = observation.get('session')
                try:
                    self.observer(metadata, result, error, started)
                except Exception:
                    pass  # Diagnostic failures must not change the read result.

    def _dispatch(self, request, observation):
        if not isinstance(request, dict) or set(request) != {'capability', 'arguments'} or not self.active(request['capability']):
            raise ReadError('Inactive relay capability.')
        observation['authorized'] = True
        observation['session'] = self.observation_session
        args = request['arguments']
        if not isinstance(args, dict):
            raise ReadError('Read arguments must be an object.')
        operation = args.get('operation')
        if operation == 'state':
            args = operation_arguments(args, {'operation', 'entity_id', 'attributes'}, 'Unsupported state fields.')
            try:
                entity_id = self.entity_id(args.get('entity_id'))
            except ValueError:
                raise ReadError('Use a noncredential entity id.') from None
            attributes = args.get('attributes')
            if attributes is None:
                attributes = []
            if (not isinstance(attributes, list) or len(attributes) > 10
                    or any(not isinstance(key, str) or key not in ATTRIBUTES for key in attributes)):
                raise ReadError('Use only the documented observational attributes.')
            path = '/states/' + entity_id
        elif operation == 'states':
            args = operation_arguments(args, {'operation', 'domain', 'offset', 'limit'}, 'Unsupported entity-list fields.')
            domain, offset, limit = args.get('domain'), args.get('offset'), args.get('limit')
            if domain is None or (isinstance(domain, str) and not domain.strip()):
                domain = None
            if offset is None:
                offset = 0
            if limit is None:
                limit = 10
            if domain is not None and (not isinstance(domain, str) or not DOMAIN.fullmatch(domain) or len(domain) > 64):
                raise ReadError('Invalid domain.')
            if type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or limit < 1:
                raise ReadError('Use an integer page offset from 0 through 100000 and a positive integer limit.')
            clamped = limit > 50
            limit = min(limit, 50)
            path = '/states'
        else:
            raise ReadError('Only entity listings and state readback are supported. No actions or arbitrary URLs.')
        if not self.inflight.acquire(blocking=False):
            raise ReadError('A relay read is already running. Try again later.')
        try:
            if not self.active(request['capability']):
                raise ReadError('Relay capability expired.')
            token = self.token()
            if not token:
                raise ReadError('Worker Core access is unavailable.')
            data = self.fetch(path, token)
            if operation == 'state':
                if not isinstance(data, dict) or data.get('entity_id') != entity_id or not isinstance(data.get('state'), str):
                    raise ReadError('Core returned an unavailable or invalid entity.')
                if password_state(data):
                    raise ReadError('Password-mode text entities are unavailable to relay reads.')
                raw_attrs = data.get('attributes')
                if not isinstance(raw_attrs, dict):
                    raise ReadError('Core returned invalid attributes.')
                result = {'ok': True, 'entity_id': entity_id, 'state': data['state'][:512],
                          'state_truncated': len(data['state']) > 512,
                          'attributes': {key: raw_attrs[key] for key in attributes if key in raw_attrs}}
            else:
                if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                    raise ReadError('Core returned an invalid entity listing.')
                safe = []
                hidden = False
                for row in data:
                    raw_id = row.get('entity_id')
                    if domain is not None and (not isinstance(raw_id, str) or raw_id.split('.', 1)[0] != domain):
                        continue
                    if password_state(row):
                        hidden = True
                        continue
                    try:
                        candidate = self.entity_id(raw_id)
                    except ValueError:
                        if isinstance(raw_id, str) and ENTITY.fullmatch(raw_id) and PRIVATE.search(raw_id.split('.', 1)[1]):
                            hidden = True
                        continue
                    if domain is None or candidate.split('.', 1)[0] == domain:
                        if not isinstance(row.get('state'), str):
                            raise ReadError('Core returned an invalid state.')
                        safe.append({'entity_id': candidate, 'state': row['state'][:512],
                                     'state_truncated': len(row['state']) > 512})
                safe.sort(key=lambda item: item['entity_id'])
                result = {'ok': True, 'entities': safe[offset:offset + limit], 'total': len(safe),
                          'next_offset': offset + limit if offset + limit < len(safe) else None}
                if clamped:
                    result['notes'] = ['Page limit capped at 50.']
                if hidden:
                    result.setdefault('notes', []).append('Credential-like or password-mode entities were hidden.')
            result = self.clean(result)
            if len(json.dumps(result).encode()) + 1 > MAX_RESPONSE:
                raise ReadError('Read response is too large. Request fewer attributes or a smaller page.')
            if not self.active(request['capability']):
                raise ReadError('Relay capability expired before readback finished.')
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
                        raise ReadError('Read request exceeds 16 KiB.')
                    try:
                        payload = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise ReadError('Invalid relay read request JSON.') from None
                    result = broker.dispatch(payload)
                except ReadError as exc:
                    result = {'ok': False, 'error': str(exc)}
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
