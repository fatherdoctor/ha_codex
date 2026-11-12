"""Allowlisted diagnostic records; no prompts, output or raw identifiers persist.

Bounded worker-only observation; remote ChatGPT thread events are not collected.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import uuid

from diagnostics_scrub import (MAX_INTERACTIONS, MAX_BYTES, MAX_EVENTS, DOMAINS, TOOLS,
                               choice, scrub, sanitize_export)


class RollingDiagnostics:
    """Thread-safe bounded writer for a single worker process."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.unavailable = False

    def _read(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES:
                self.unavailable = True
                return []
            data = json.loads(self.path.read_text())
            self.unavailable = not isinstance(data, list)
            return data[-MAX_INTERACTIONS:] if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            self.unavailable = True
            return []

    @staticmethod
    def _stored(row):
        """Revalidate old disk rows before retaining them in another write."""
        clean = scrub(row)
        if not isinstance(row, dict):
            return clean
        clean['entities'] = [{'domain': choice(e.get('domain'), DOMAINS, 'other'), 'reference': e['reference']}
                             for e in row.get('entities', [])[:64]
                             if isinstance(e, dict) and isinstance(e.get('reference'), str)
                             and re.fullmatch('[a-f0-9]{64}', e['reference'])] if isinstance(row.get('entities'), list) else []
        clean['paths'] = sanitize_export({'interactions': [row]})['interactions'][0]['paths']
        if isinstance(row.get('_key'), str) and re.fullmatch('[a-f0-9]{32}', row['_key']):
            clean['_key'] = row['_key']
        return clean

    def append(self, record, key=None):
        clean = scrub(record)
        if isinstance(key, str) and re.fullmatch("[a-f0-9]{32}", key):
            clean["_key"] = key
        with self.lock:
            rows = [self._stored(r) for r in self._read() if isinstance(r, dict) and (not key or r.get("_key") != key)]
            rows.append(clean)
            rows = deque(sorted(rows, key=lambda row: row.get('start') or '' if isinstance(row, dict) else '')[-MAX_INTERACTIONS:])
            while len(json.dumps(list(rows)).encode()) > MAX_BYTES:
                rows.popleft()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.diagnostics-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(list(rows), stream, allow_nan=False)
                os.replace(name, self.path)
                self.unavailable = False
            finally:
                Path(name).unlink(missing_ok=True)

    def export(self):
        """Export only the scrubbed schema, re-keying references per download."""
        with self.lock:
            rows = self._read()
        salt = secrets.token_bytes(32)
        result = []
        for row in rows:
            clean = scrub(row)
            # Re-scrub disk contents; only exact fixed-size stored references qualify.
            clean['entities'] = []
            for entity in row.get('entities', [])[:64] if isinstance(row, dict) and isinstance(row.get('entities'), list) else []:
                if isinstance(entity, dict) and isinstance(entity.get('reference'), str) and re.fullmatch('[a-f0-9]{64}', entity['reference']):
                    clean['entities'].append({'domain': choice(entity.get('domain'), DOMAINS, 'other'),
                                              'id': hashlib.sha256(salt + entity['reference'].encode()).hexdigest()[:12]})
            # Stored paths are already categories; validate them independently.
            clean['paths'] = [{'folder': choice(p.get('folder'), {'root', 'automations', 'scripts', 'templates', 'packages'}, 'other'),
                               'extension': choice(p.get('extension'), {'.yaml', '.yml', '.json'}, 'other')}
                              for p in row.get('paths', [])[:32] if isinstance(p, dict)] if isinstance(row, dict) and isinstance(row.get('paths'), list) else []
            result.append(clean)
        return sanitize_export({'interactions': result, 'availability': 'unavailable' if self.unavailable else 'available'})


class InteractionRecorder:
    """Observe bounded metadata only, without changing execution or permission decisions."""

    def __init__(self, store, addon_version='unknown', cli_version='unknown', clock=time.time):
        self.store = store
        self.addon_version = addon_version
        self.cli_version = cli_version
        self.clock = clock
        self.lock = threading.RLock()
        self.active = {}

    def _save(self, state):
        try:
            self.store.append(state['record'], state['key'])
        except (OSError, ValueError):
            # Diagnostic storage must never change an execution/approval outcome.
            self.store.unavailable = True

    def begin(self, key, source, model='default', start=None):
        with self.lock:
            if key in self.active:
                self.finish(key, 'stopped')
            while len(self.active) >= MAX_INTERACTIONS:
                self.finish(next(iter(self.active)), 'unknown')
            state = {'key': uuid.uuid4().hex, 'tools': {}, 'approvals': {},
                     'record': {'source': source, 'model': model, 'addon_version': self.addon_version,
                                'cli_version': self.cli_version, 'start': self.clock() if start is None else start,
                                'outcome': 'running', 'events': [], 'entities': [], 'paths': []}}
            self.active[key] = state
            self._save(state)

    def event(self, key, event, *, entities=(), paths=()):
        with self.lock:
            state = self.active.get(key)
            if state is None:
                return
            record = state['record']
            # Keep only the scrubbed event in memory as well as on disk.
            event = {**event, 'time': self.clock()}
            clean = scrub({'events': [event]})['events'][0]
            record['events'] = (record['events'] + [clean])[-MAX_EVENTS:]
            record['entities'] = (record['entities'] + [x for x in entities if isinstance(x, str)])[-64:]
            record['paths'] = (record['paths'] + [x for x in paths if isinstance(x, str)])[-32:]
            self._save(state)

    def model(self, key, value):
        with self.lock:
            state = self.active.get(key)
            if state is not None:
                state['record']['model'] = value
                self._save(state)

    def gate_message(self, key, value):
        """Recognize fixed worker/hook refusal text without saving arbitrary stderr."""
        if not isinstance(value, str):
            return
        reasons = {
            "This request enables full access, which is never allowed.": "full_access",
            "This command reads a process environment, which can hold the Supervisor token, which is never allowed.": "process_environment",
            "This command reads the app's saved environment, which holds the Supervisor token, which is never allowed.": "process_environment",
            "This command uses the Supervisor token, which is never allowed.": "credential",
            "This command reads secrets.yaml, which is never allowed.": "credential",
            "This command reads Home Assistant's sign-in store, which is never allowed.": "credential",
            "This edit edits secrets.yaml, which is never allowed.": "credential",
            "This request changes the add-on's settings, which is never allowed.": "protected_policy",
            "This request changes the add-on's rules, which is never allowed.": "protected_policy",
            "This request changes the add-on's permission policy, which is never allowed.": "protected_policy",
            "This request accesses the add-on's private credential, which is never allowed.": "credential",
            "This command talks to the Home Assistant Supervisor, which is never allowed.": "other",
        }
        for line in value[:8192].splitlines()[:64]:
            rule = reasons.get(line.strip().removeprefix('Error: '))
            if rule is not None:
                self.event(key, {'kind': 'gate', 'rule': rule})

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
            names = {'command_execution': 'commandExecution', 'file_change': 'fileChange',
                     'mcp_tool_call': 'mcpToolCall', 'web_search': 'webSearch'}
            name = names.get(item.get('type'))
            if name is None:
                return  # Never keep user/assistant/reasoning content.
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
            self.event(key, {'kind': 'tool', 'name': name, 'result': result,
                             'duration_seconds': self.clock() - started if started is not None else None}, paths=paths)

    def approvals(self, key, records):
        with self.lock:
            state = self.active.get(key)
            if state is None:
                return
            for item in records[-MAX_EVENTS:]:
                identifier = hashlib.sha256(str(item.get('approval_id', '')).encode()).hexdigest()
                status = item.get('status')
                decision = {'pending': 'asked', 'allowed': 'allowed', 'denied': 'denied',
                            'expired': 'expired', 'cancelled': 'cancelled'}.get(status, 'unknown')
                if state['approvals'].get(identifier) != decision:
                    if len(state['approvals']) >= MAX_EVENTS and identifier not in state['approvals']:
                        continue
                    state['approvals'][identifier] = decision
                    self.event(key, {'kind': 'approval', 'decision': decision})
                    if item.get('can_allow') is False:
                        self.event(key, {'kind': 'gate', 'rule': 'protected_policy' if item.get('note') == "This request touches the app's permission policy or settings, which Codex may not edit. Deny it." else 'other'})

    def finish(self, key, outcome, error=None):
        with self.lock:
            state = self.active.get(key)
            if state is None:
                return
            if error is not None:
                self.event(key, {'kind': 'error', 'type': type(error).__name__})
            record = state['record']
            for started, name in state['tools'].values():
                self.event(key, {'kind': 'tool', 'name': name, 'result': 'stopped' if outcome == 'stopped' else 'unknown',
                                 'duration_seconds': self.clock() - started})
            record.update(end=self.clock(), duration_seconds=max(0, self.clock() - record['start']), outcome=outcome)
            self._save(state)
            self.active.pop(key, None)
