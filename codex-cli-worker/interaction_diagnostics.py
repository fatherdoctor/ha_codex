"""Allowlisted diagnostic records; no prompts, output or raw identifiers persist.

Foundation only: interaction collectors and export endpoints must use this
boundary before this feature can be enabled.
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

MAX_INTERACTIONS = 10
MAX_BYTES = 128 * 1024
MAX_EVENTS = 48
DOMAINS = frozenset(('light', 'sensor', 'binary_sensor', 'switch', 'climate', 'lock',
                     'cover', 'alarm_control_panel', 'input_boolean', 'input_number',
                     'input_text', 'text', 'script', 'automation', 'scene', 'fan',
                     'media_player', 'camera', 'button', 'number', 'select', 'other'))
TOOLS = frozenset(('commandExecution', 'fileChange', 'mcpToolCall', 'webSearch',
                   'read', 'history', 'statistics', 'apply_patch', 'other'))
ORIGINS = frozenset(('addon_page', 'remote_thread', 'scheduled_run', 'lead_channel'))
RESULTS = frozenset(('done', 'failed', 'stopped', 'timed_out', 'running', 'unknown'))
DECISIONS = frozenset(('asked', 'allowed', 'denied', 'expired', 'cancelled', 'unknown'))
ERRORS = {'TimeoutError': 'Operation timed out.', 'PermissionError': 'Operation refused.',
          'OSError': 'Operation unavailable.', 'ValueError': 'Invalid operation.',
          'RuntimeError': 'Operation failed.'}
RULES = frozenset(('credential', 'process_environment', 'protected_policy',
                   'full_access', 'planted_definition', 'template_action', 'other'))
MODELS = frozenset(('gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-sol', 'gpt-5.3-codex',
                    'gpt-5.4', 'gpt-5.5', 'gpt-6.1', 'default'))


def choice(value, allowed, default='unknown'):
    return value if isinstance(value, str) and value in allowed else default


def number(value, maximum=86400):
    return round(float(value), 3) if type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value) else None


def timestamp(value):
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00', value):
        try:
            value = datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    value = number(value, 4102444800)
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def scrub(record):
    """Keep only schema-owned values, never attempt to redact arbitrary prose."""
    if not isinstance(record, dict):
        return {}
    out = {'source': choice(record.get('source'), ORIGINS),
           'start': timestamp(record.get('start')), 'end': timestamp(record.get('end')),
           'duration_seconds': number(record.get('duration_seconds')),
           'outcome': choice(record.get('outcome'), RESULTS),
           'model': choice(record.get('model'), MODELS),
           'addon_version': record.get('addon_version') if isinstance(record.get('addon_version'), str)
           and re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}(?:\.\d{1,4})?', record['addon_version']) else 'unknown'}
    events = record.get('events')
    out['events'] = []
    for event in events[:MAX_EVENTS] if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        kind = choice(event.get('kind'), {'tool', 'approval', 'gate', 'error', 'ha_read'}, 'other')
        item = {'kind': kind, 'time': timestamp(event.get('time'))}
        if kind == 'tool':
            item.update(name=choice(event.get('name'), TOOLS, 'other'),
                        result=choice(event.get('result'), RESULTS),
                        duration_seconds=number(event.get('duration_seconds')))
        elif kind == 'approval':
            item['decision'] = choice(event.get('decision'), DECISIONS)
        elif kind == 'gate':
            item['rule'] = choice(event.get('rule'), RULES, 'other')
        elif kind == 'error':
            error = choice(event.get('type'), ERRORS, 'RuntimeError')
            item.update(type=error, message=ERRORS[error])
        elif kind == 'ha_read':
            item.update(operation=choice(event.get('operation'), {'state', 'states', 'history', 'statistics'}),
                        domain=choice(event.get('domain'), DOMAINS, 'other'))
        out['events'].append(item)
    # Stored identifiers are one-way references, re-keyed again for each export.
    out['entities'] = []
    for entity in record.get('entities', [])[:64] if isinstance(record.get('entities'), list) else []:
        if isinstance(entity, str) and re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', entity):
            domain = choice(entity.split('.', 1)[0], DOMAINS, 'other')
            out['entities'].append({'domain': domain, 'reference': hashlib.sha256(entity.encode()).hexdigest()})
    # No arbitrary folder names survive: only a known /config top-level category and extension.
    out['paths'] = []
    for value in record.get('paths', [])[:32] if isinstance(record.get('paths'), list) else []:
        if isinstance(value, str) and value.startswith('/config/') and '..' not in Path(value).parts:
            parts = Path(value).parts[2:]
            folder = choice(parts[0], {'automations', 'scripts', 'templates', 'packages'}, 'other') if len(parts) > 1 else 'root'
            extension = choice(Path(value).suffix, {'.yaml', '.yml', '.json'}, 'other')
            out['paths'].append({'folder': folder, 'extension': extension})
    return out


class RollingDiagnostics:
    """Thread-safe bounded writer for a single worker process; collectors not wired."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _read(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES:
                return []
            data = json.loads(self.path.read_text())
            return data[-MAX_INTERACTIONS:] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def append(self, record):
        clean = scrub(record)
        with self.lock:
            rows = deque(self._read(), maxlen=MAX_INTERACTIONS)
            rows.append(clean)
            while len(json.dumps(list(rows)).encode()) > MAX_BYTES:
                rows.popleft()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.diagnostics-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(list(rows), stream, allow_nan=False)
                os.replace(name, self.path)
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
        return {'schema_version': 1, 'interactions': result}
