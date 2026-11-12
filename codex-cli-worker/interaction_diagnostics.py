"""Bounded worker metadata with keyed references and isolated diagnostic failures."""
from __future__ import annotations
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid
from diagnostics_scrub import MAX_INTERACTIONS, MAX_RETENTION, MAX_BYTES, MAX_EVENTS, DOMAINS, choice, scrub, sanitize_export, entity_references, path_categories
_LOGGER = logging.getLogger(__name__)

def nonfatal(method):
    """No diagnostic entry point may change an execution or permission outcome."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self.store.unavailable = True
            self.failures += 1
            return None
    return guarded

class RollingDiagnostics:
    """One worker process owns this private store and its installation HMAC key."""

    def __init__(self, path, capacity=MAX_INTERACTIONS):
        self.path = Path(path)
        self.key_path = self.path.with_suffix('.key')
        self.settings_path = self.path.with_suffix('.settings.json')
        self.lock = threading.RLock()
        self.unavailable = False
        self.discarded_rows = 0
        self.store_resets = 0
        self._entity_key = None
        self.capacity = MAX_INTERACTIONS
        self.max_bytes = MAX_BYTES * MAX_INTERACTIONS // 10
        self.configure(capacity, persist=False)
        try:
            settings = json.loads(self.settings_path.read_text())
            self.configure(settings['capacity'], persist=False)
        except FileNotFoundError:
            pass
        except Exception:
            self.unavailable = True

    def _atomic_write(self, value, destination=None):
        destination = destination or self.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.diagnostics-', dir=destination.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(value, stream, allow_nan=False)
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)

    def configure(self, capacity, persist=True):
        if type(capacity) is not int or not 10 <= capacity <= 100:
            raise ValueError('Keep between 10 and 100 interactions.')
        with self.lock:
            if persist:
                self._atomic_write({'capacity': capacity}, self.settings_path)
            self.capacity = capacity
            self.max_bytes = MAX_BYTES * capacity // 10
            if persist:
                self._atomic_write(self._retain(self._read()))

    def entity_key(self):
        """Create an atomic, private random key; never include it in a diagnostic record."""
        with self.lock:
            if self._entity_key is not None:
                return self._entity_key
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.key_path.exists():
                descriptor, name = tempfile.mkstemp(prefix='.diagnostics-key-', dir=self.key_path.parent)
                try:
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(secrets.token_bytes(32))
                    try:
                        os.link(name, self.key_path)
                    except FileExistsError:
                        pass
                finally:
                    Path(name).unlink(missing_ok=True)
            descriptor = os.open(self.key_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 63:
                    raise ValueError('Diagnostic key permissions are unsafe.')
                key = stream.read(33)
            if len(key) != 32:
                raise ValueError('Diagnostic key is invalid.')
            self._entity_key = key
            return key

    def prepare(self, record, stored=False):
        if stored and (not record.get('_keyed_entities')):
            record = {**record, 'entities': []}
        clean = scrub(record, entity_key=None if stored else self.entity_key(), stored=stored)
        clean['_keyed_entities'] = True
        if stored and isinstance(record.get('_key'), str) and re.fullmatch('[a-f0-9]{32}', record['_key']):
            clean['_key'] = record['_key']
        return clean

    def _reset(self):
        """Keep malformed input private and give the owner a content-free note."""
        if self.path.exists() or self.path.is_symlink():
            quarantine = self.path.with_name(self.path.name + '.corrupt-' + uuid.uuid4().hex[:12])
            os.replace(self.path, quarantine)
        self.store_resets += 1
        self._atomic_write([])
        _LOGGER.warning('Diagnostic store set aside; a fresh store was started.')
        return []

    def _read(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES * MAX_RETENTION // 10:
                return self._reset()
            data = json.loads(self.path.read_text())
            if not isinstance(data, list):
                return self._reset()
        except FileNotFoundError:
            return []
        except (ValueError, RecursionError):
            return self._reset()
        rows = []
        dropped = 0
        migrated = False
        for row in data:
            if isinstance(row, dict):
                migrated = migrated or not row.get('_keyed_entities', False)
                rows.append(self.prepare(row, stored=True))
            else:
                dropped += 1
        self.discarded_rows += dropped
        if dropped or migrated:
            self._atomic_write(rows)
        return rows

    def _retain(self, rows):
        running = [row for row in rows if row.get('outcome') == 'running']
        completed = sorted((row for row in rows if row.get('outcome') != 'running'), key=lambda row: row.get('start') or '')
        room = max(0, self.capacity - len(running))
        completed = completed[-room:] if room else []
        retained = sorted(completed + running, key=lambda row: row.get('start') or '')
        while len(json.dumps(retained).encode()) > self.max_bytes:
            if completed:
                removed = completed.pop(0)
                retained.remove(removed)
                continue
            changed = False
            for row in retained:
                for field in ('events', 'entities', 'paths', 'read_counts'):
                    if row.get(field):
                        row[field] = row[field][len(row[field]) // 2 + 1:]
                        changed = True
            if not changed:
                raise ValueError('Active diagnostic metadata exceeds storage capacity.')
        return retained

    def append(self, record, key=None, prepared=False):
        with self.lock:
            clean = self.prepare(record, stored=prepared)
            if isinstance(key, str) and re.fullmatch('[a-f0-9]{32}', key):
                clean['_key'] = key
            rows = [row for row in self._read() if not key or row.get('_key') != key]
            rows.append(clean)
            self._atomic_write(self._retain(rows))
            self.unavailable = False

    def export(self):
        """Export a snapshot, never a key, private reference or exception message."""
        try:
            with self.lock:
                rows = self._retain(self._read())
                salt = secrets.token_bytes(32)
                result = []
                for row in rows:
                    clean = scrub(row, stored=True)
                    clean['entities'] = entity_references(row.get('entities'), salt=salt)
                    result.append(clean)
                payload = {
                    'interactions': result,
                    'availability': 'unavailable' if self.unavailable else 'available',
                    'discarded_rows': self.discarded_rows,
                    'store_resets': self.store_resets,
                }
            return sanitize_export(payload)
        except Exception:
            self.unavailable = True
            return sanitize_export({'availability': 'unavailable', 'interactions': []})

class InteractionRecorder:
    """Observe bounded metadata only, without changing execution or permission decisions."""

    def __init__(self, store, addon_version='unknown', cli_version='unknown', clock=time.time):
        self.store = store
        self.addon_version = addon_version
        self.cli_version = cli_version
        self.clock = clock
        self.lock = threading.RLock()
        self.active = {}
        self.failures = 0

    def _save(self, state, force=False):
        state['dirty'] += 1
        if force or state['dirty'] >= 8:
            self.store.append(state['record'], state['key'], prepared=True)
            state['dirty'] = 0

    @nonfatal
    def flush(self):
        with self.lock:
            for state in self.active.values():
                if state['dirty']:
                    self._save(state, force=True)

    @nonfatal
    def begin(self, key, source, model='default', start=None):
        with self.lock:
            if key in self.active:
                self.finish(key, 'stopped')
            state = {
                'key': uuid.uuid4().hex,
                'tools': {},
                'approvals': {},
                'dirty': 0,
                'record': {
                    'source': source,
                    'model': model,
                    'addon_version': self.addon_version,
                    'cli_version': self.cli_version,
                    'start': self.clock() if start is None else start,
                    'outcome': 'running',
                    'events': [],
                    'entities': [],
                    'paths': [],
                },
            }
            state['started'] = state['record']['start']
            state['record'] = self.store.prepare(state['record'])
            self.active[key] = state
            self._save(state, force=True)

    @nonfatal
    def event(self, key, event, *, entities=(), paths=()):
        with self.lock:
            state = self.active.get(key)
            if state is None:
                return
            record = state['record']
            event = {**event, 'time': self.clock()}
            clean = scrub({'events': [event]})['events'][0]
            record['events'] = (record['events'] + [clean])[-MAX_EVENTS:]
            record['entities'] = (record['entities'] + entity_references(list(entities), key=self.store.entity_key()))[-64:]
            record['paths'] = path_categories(record['paths'] + list(paths))[-32:]
            self._save(state)

    @nonfatal
    def model(self, key, value):
        with self.lock:
            state = self.active.get(key)
            if state is not None:
                state['record']['model'] = value
                self._save(state)

    @nonfatal
    def gate_message(self, key, value):
        """Recognize fixed worker/hook refusal text without saving arbitrary stderr."""
        if not isinstance(value, str):
            return
        reasons = {
            'This request enables full access, which is never allowed.': 'full_access',
            'This command reads a process environment, which can hold the Supervisor token, which is never allowed.': 'process_environment',
            "This command reads the app's saved environment, which holds the Supervisor token, which is never allowed.": 'process_environment',
            'This command uses the Supervisor token, which is never allowed.': 'credential',
            'This command reads secrets.yaml, which is never allowed.': 'credential',
            "This command reads Home Assistant's sign-in store, which is never allowed.": 'credential',
            'This edit edits secrets.yaml, which is never allowed.': 'credential',
            "This request changes the add-on's settings, which is never allowed.": 'protected_policy',
            "This request changes the add-on's rules, which is never allowed.": 'protected_policy',
            "This request changes the add-on's permission policy, which is never allowed.": 'protected_policy',
            "This request accesses the add-on's private credential, which is never allowed.": 'credential',
            'This command talks to the Home Assistant Supervisor, which is never allowed.': 'other',
        }
        for line in value[:8192].splitlines()[:64]:
            rule = reasons.get(line.strip().removeprefix('Error: '))
            if rule is not None:
                self.event(key, {'kind': 'gate', 'rule': rule})

    @nonfatal
    def activity(self, key, event):
        if not isinstance(event, dict):
            return
        with self.lock:
            state = self.active.get(key)
            if state is None:
                return
            kind = event.get('type')
            if kind in {'error', 'turn.failed'}:
                self.event(key, {'kind': 'error', 'type': 'RuntimeError'})
                error = event.get('error')
                self.gate_message(key, error.get('message') if isinstance(error, dict) else event.get('message'))
            item = event.get('item')
            if not isinstance(item, dict) or kind not in {'item.started', 'item.completed'}:
                return
            names = {
                'command_execution': 'commandExecution',
                'file_change': 'fileChange',
                'mcp_tool_call': 'mcpToolCall',
                'web_search': 'webSearch',
            }
            name = names.get(item.get('type'))
            if name is None:
                return
            identifier = hashlib.sha256(str(item.get('id', '')).encode()).hexdigest()
            if kind == 'item.started':
                if len(state['tools']) < MAX_EVENTS:
                    state['tools'][identifier] = (self.clock(), name)
                return
            pending = state['tools'].pop(identifier, None)
            started = pending[0] if pending is not None else None
            status = item.get('status')
            result = 'failed' if status in {'failed', 'error'} or item.get('exit_code') not in (None, 0) else 'done' if status in {'completed', 'done'} else 'unknown'
            if result == 'failed':
                self.gate_message(key, item.get('aggregated_output'))
            changes = item.get('changes')
            paths = [x.get('path') for x in changes[:32] if isinstance(x, dict)] if isinstance(changes, list) else []
            self.event(key, {
                'kind': 'tool',
                'name': name,
                'result': result,
                'duration_seconds': self.clock() - started if started is not None else None,
            }, paths=paths)

    @nonfatal
    def approvals(self, key, records):
        with self.lock:
            state = self.active.get(key)
            if state is None:
                return
            for item in records[-MAX_EVENTS:]:
                identifier = hashlib.sha256(str(item.get('approval_id', '')).encode()).hexdigest()
                status = item.get('status')
                decision = {
                    'pending': 'asked',
                    'allowed': 'allowed',
                    'denied': 'denied',
                    'expired': 'expired',
                    'cancelled': 'cancelled',
                }.get(status, 'unknown')
                if state['approvals'].get(identifier) != decision:
                    if len(state['approvals']) >= MAX_EVENTS and identifier not in state['approvals']:
                        continue
                    state['approvals'][identifier] = decision
                    self.event(key, {'kind': 'approval', 'decision': decision})
                    if item.get('can_allow') is False:
                        self.event(key, {
                            'kind': 'gate',
                            'rule': 'protected_policy' if item.get('note') == "This request touches the app's permission policy or settings, which Codex may not edit. Deny it." else 'other',
                        })

    @nonfatal
    def finish(self, key, outcome, error=None):
        with self.lock:
            state = self.active.get(key)
            if state is None:
                return
            if error is not None:
                self.event(key, {'kind': 'error', 'type': type(error).__name__})
            record = state['record']
            for started, name in state['tools'].values():
                self.event(key, {
                    'kind': 'tool',
                    'name': name,
                    'result': 'stopped' if outcome == 'stopped' else 'unknown',
                    'duration_seconds': self.clock() - started,
                })
            record.update(end=self.clock(), duration_seconds=max(0, self.clock() - state['started']), outcome=outcome)
            try:
                self._save(state, force=True)
            finally:
                self.active.pop(key, None)

    @nonfatal
    def relay_read(self, session, metadata, error, started):
        with self.lock:
            key = ('relay', session)
            if key not in self.active:
                self.begin(key, 'relay_read', 'unknown', start=started)
            state = self.active[key]
            operation = choice(metadata.get('operation'), {'state', 'states', 'history', 'statistics'})
            entity = metadata.get('entity_id')
            domain = entity.split('.', 1)[0] if isinstance(entity, str) else metadata.get('domain')
            domain = choice(domain, DOMAINS, 'other')
            counts = state['record']['read_counts']
            counter = next((item for item in counts if item['operation'] == operation and item['domain'] == domain), None)
            if counter is not None:
                counter['count'] = min(1000000, counter['count'] + 1)
            elif len(counts) < 64:
                counts.append({'operation': operation, 'domain': domain, 'count': 1})
            self.event(key, {'kind': 'ha_read', 'operation': operation, 'domain': domain}, entities=[entity] if isinstance(entity, str) else [])
            self.event(key, {
                'kind': 'tool',
                'name': 'read',
                'result': 'failed' if error else 'done',
                'duration_seconds': max(0, self.clock() - started),
            })
            if error is not None:
                self.event(key, {'kind': 'error', 'type': type(error).__name__})
                if type(error).__name__ == 'ReadError':
                    credential = str(error) in {'Use a noncredential entity id.', 'Password-mode text entities are unavailable to relay reads.'}
                    self.event(key, {'kind': 'gate', 'rule': 'credential' if credential else 'other'})

    @nonfatal
    def relay_end(self, session):
        self.finish(('relay', session), 'done')
