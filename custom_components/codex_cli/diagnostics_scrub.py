"""Pure, allowlisted diagnostics schema shared by worker and integration.

Both packaged copies must stay identical (covered by the worker tests).
"""
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re

MAX_INTERACTIONS = 10
MAX_BYTES = 128 * 1024
MAX_EVENTS = 48
DOMAINS = frozenset(('light', 'sensor', 'binary_sensor', 'switch', 'climate', 'lock',
                     'cover', 'alarm_control_panel', 'input_boolean', 'input_number',
                     'input_text', 'text', 'script', 'automation', 'scene', 'fan',
                     'media_player', 'camera', 'button', 'number', 'select', 'other'))
TOOLS = frozenset(('commandExecution', 'fileChange', 'mcpToolCall', 'webSearch',
                   'read', 'history', 'statistics', 'apply_patch', 'other'))
ORIGINS = frozenset(('addon_page', 'relay_read', 'scheduled_run', 'lead_channel'))
RESULTS = frozenset(('done', 'failed', 'stopped', 'timed_out', 'running', 'unknown'))
DECISIONS = frozenset(('asked', 'allowed', 'denied', 'expired', 'cancelled', 'unknown'))
ERRORS = {'TimeoutError': 'Operation timed out.', 'PermissionError': 'Operation refused.',
          'OSError': 'Operation unavailable.', 'ValueError': 'Invalid operation.',
          'RuntimeError': 'Operation failed.'}
RULES = frozenset(('credential', 'process_environment', 'protected_policy',
                   'full_access', 'planted_definition', 'template_action', 'other'))
MODELS = frozenset(('gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-sol', 'gpt-5.3-codex',
                    'gpt-5.4', 'gpt-5.5', 'gpt-6.1', 'gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'default'))


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


def version(value):
    return value if isinstance(value, str) and re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}(?:\.\d{1,4})?', value) else 'unknown'


def scrub(record):
    """Keep only schema-owned values, never attempt to redact arbitrary prose."""
    if not isinstance(record, dict):
        return {}
    out = {'source': choice(record.get('source'), ORIGINS),
           'start': timestamp(record.get('start')), 'end': timestamp(record.get('end')),
           'duration_seconds': number(record.get('duration_seconds')),
           'cli_version': version(record.get('cli_version')),
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


COVERAGE = "remote ChatGPT threads: not recorded"


def sanitize_export(value):
    """Rebuild an untrusted export without copying arbitrary text or keys."""
    rows = value.get('interactions') if isinstance(value, dict) else None
    result = []
    for row in rows[-MAX_INTERACTIONS:] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        clean = scrub(row)
        clean['entities'] = []
        for entity in row.get('entities', [])[:64] if isinstance(row.get('entities'), list) else []:
            if isinstance(entity, dict) and isinstance(entity.get('id'), str) and re.fullmatch('[a-f0-9]{12}', entity['id']):
                clean['entities'].append({'domain': choice(entity.get('domain'), DOMAINS, 'other'), 'id': entity['id']})
        clean['paths'] = [{'folder': choice(p.get('folder'), {'root', 'automations', 'scripts', 'templates', 'packages'}, 'other'),
                           'extension': choice(p.get('extension'), {'.yaml', '.yml', '.json'}, 'other')}
                          for p in row.get('paths', [])[:32] if isinstance(p, dict)] if isinstance(row.get('paths'), list) else []
        result.append(clean)
    return {'schema_version': 1, 'coverage': COVERAGE, 'availability': choice(value.get('availability'), {'available', 'unavailable'}, 'available') if isinstance(value, dict) else 'unavailable', 'interactions': result}
