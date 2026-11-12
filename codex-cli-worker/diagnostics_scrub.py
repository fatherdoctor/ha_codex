"""Pure, allowlisted diagnostics schema shared by worker and integration.

Both packaged copies must stay identical (covered by the worker tests).
"""
from datetime import datetime, timezone
import hashlib
import hmac
import math
from pathlib import Path
import re
MAX_INTERACTIONS = 25
MAX_RETENTION = 100
MAX_BYTES = 128 * 1024
MAX_EVENTS = 48
DOMAINS = frozenset(('light', 'sensor', 'binary_sensor', 'switch', 'climate', 'lock', 'cover', 'alarm_control_panel', 'input_boolean', 'input_number', 'input_text', 'text', 'script', 'automation', 'scene', 'fan', 'media_player', 'camera', 'button', 'number', 'select', 'other'))
TOOLS = frozenset(('commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'read', 'history', 'statistics', 'apply_patch', 'other'))
ORIGINS = frozenset(('addon_page', 'relay_read', 'scheduled_run', 'lead_channel'))
RESULTS = frozenset(('done', 'failed', 'stopped', 'timed_out', 'running', 'unknown', 'unknown (restart)'))
DECISIONS = frozenset(('asked', 'allowed', 'denied', 'expired', 'cancelled', 'unknown'))
ERRORS = {
    'TimeoutError': 'Operation timed out.',
    'PermissionError': 'Operation refused.',
    'OSError': 'Operation unavailable.',
    'ValueError': 'Invalid operation.',
    'RuntimeError': 'Operation failed.',
}
RULES = frozenset(('credential', 'process_environment', 'protected_policy', 'full_access', 'approval_policy_never', 'policy_parse', 'planted_definition', 'template_action', 'other'))
MODELS = frozenset(('gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-sol', 'gpt-5.3-codex', 'gpt-5.4', 'gpt-5.5', 'gpt-6.1', 'gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'default'))


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
    """Accept bounded numeric releases; ambiguous address-shaped quads fail closed."""
    # Build versions are numeric, but address-shaped quads must not become exported metadata.
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?', value):
        return 'unknown'
    parts = value.split('.')
    if len(parts) == 4 and parts[0] != '0' and all(int(part) <= 255 for part in parts):
        return 'unknown'
    return value


FOLDERS = frozenset({'root', 'automations', 'scripts', 'templates', 'packages'})
EXTENSIONS = frozenset({'.yaml', '.yml', '.json'})


def path_categories(values):
    """The only path boundary: raw config paths or already categorized paths."""
    result = []
    # Keep categories only: no basename, absolute host path or traversal survives.
    for value in values[:32] if isinstance(values, list) else []:
        if isinstance(value, dict):
            folder = choice(value.get('folder'), FOLDERS, 'other')
            extension = choice(value.get('extension'), EXTENSIONS, 'other')
        elif isinstance(value, str) and value.startswith('/config/') and ('..' not in Path(value).parts):
            parts = Path(value).parts[2:]
            folder = choice(parts[0], FOLDERS, 'other') if len(parts) > 1 else 'root'
            extension = choice(Path(value).suffix, EXTENSIONS, 'other')
        else:
            continue
        result.append({'folder': folder, 'extension': extension})
    return result


def entity_references(values, *, key=None, salt=None, public=False):
    """One entity boundary for raw IDs, keyed disk references and public IDs.

    Raw IDs require the installation HMAC key. Disk references use HMAC-SHA256,
    not a dictionary-reversible bare hash. Each export adds a fresh private salt.
    The integration only accepts the resulting short public IDs.
    """
    result = []
    # Never hash raw IDs without the installation key; a missing key drops them.
    for value in values[:64] if isinstance(values, list) else []:
        if isinstance(value, str) and key is not None:
            if not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', value):
                continue
            domain = choice(value.split('.', 1)[0], DOMAINS, 'other')
            reference = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
        elif isinstance(value, dict):
            domain = choice(value.get('domain'), DOMAINS, 'other')
            field = 'id' if public else 'reference'
            reference = value.get(field)
            pattern = '[a-f0-9]{12}' if public else '[a-f0-9]{64}'
            if not isinstance(reference, str) or not re.fullmatch(pattern, reference):
                continue
        else:
            continue
        if salt is not None:
            result.append({'domain': domain, 'id': hashlib.sha256(salt + reference.encode()).hexdigest()[:12]})
        else:
            result.append({'domain': domain, 'id' if public else 'reference': reference})
    return result


def scrub(record, entity_key=None, stored=False):
    """Keep only schema-owned values, never attempt to redact arbitrary prose."""
    if not isinstance(record, dict):
        return {}
    out = {
        'source': choice(record.get('source'), ORIGINS),
        'start': timestamp(record.get('start')),
        'end': timestamp(record.get('end')),
        'duration_seconds': number(record.get('duration_seconds')),
        'cli_version': version(record.get('cli_version')),
        'outcome': choice(record.get('outcome'), RESULTS),
        'model': choice(record.get('model'), MODELS),
        'addon_version': version(record.get('addon_version')),
    }
    events = record.get('events')
    out['events'] = []
    # Rebuild each event by kind. Unknown keys and arbitrary prose never cross this boundary.
    for event in events[:MAX_EVENTS] if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        kind = choice(event.get('kind'), {'tool', 'approval', 'gate', 'error', 'ha_read'}, 'other')
        item = {'kind': kind, 'time': timestamp(event.get('time'))}
        if kind == 'tool':
            item.update(name=choice(event.get('name'), TOOLS, 'other'), result=choice(event.get('result'), RESULTS), duration_seconds=number(event.get('duration_seconds')))
        elif kind == 'approval':
            item['decision'] = choice(event.get('decision'), DECISIONS)
        elif kind == 'gate':
            item['rule'] = choice(event.get('rule'), RULES, 'other')
        elif kind == 'error':
            error = choice(event.get('type'), ERRORS, 'RuntimeError')
            item.update(type=error, message=ERRORS[error])
        elif kind == 'ha_read':
            item.update(operation=choice(event.get('operation'), {'state', 'states', 'history', 'statistics'}), domain=choice(event.get('domain'), DOMAINS, 'other'))
        out['events'].append(item)
    values = record.get('entities')
    # New records accept only raw entity IDs; stored records accept only keyed references.
    if not stored:
        values = [value for value in values if isinstance(value, str)] if isinstance(values, list) else []
    out['entities'] = entity_references(values, key=entity_key)
    out['paths'] = path_categories(record.get('paths'))
    out['read_counts'] = []
    counts = record.get('read_counts')
    for count in counts[:64] if isinstance(counts, list) else []:
        if not isinstance(count, dict):
            continue
        total = count.get('count')
        if type(total) is not int or not 1 <= total <= 1000000:
            continue
        out['read_counts'].append({
            'operation': choice(count.get('operation'), {'state', 'states', 'history', 'statistics'}),
            'domain': choice(count.get('domain'), DOMAINS, 'other'),
            'count': total,
        })
    return out


COVERAGE = 'remote ChatGPT threads: not recorded'


def sanitize_export(value):
    """Rebuild untrusted exports, including diagnostic counters, without prose."""
    # Treat even a worker response as untrusted when the integration exports it.
    rows = value.get('interactions') if isinstance(value, dict) else None
    result = []
    for row in rows[-MAX_RETENTION:] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        clean = scrub(row)
        clean['entities'] = entity_references(row.get('entities'), public=True)
        result.append(clean)
    availability = 'unavailable'
    discarded = 0
    reset = 0
    if isinstance(value, dict):
        availability = choice(value.get('availability'), {'available', 'unavailable'}, 'available')
        discarded = value.get('discarded_rows', 0)
        reset = value.get('store_resets', 0)
    return {
        'schema_version': 2,
        'coverage': COVERAGE,
        'availability': availability,
        'discarded_rows': discarded if type(discarded) is int and 0 <= discarded <= 1000000 else 0,
        'store_resets': reset if type(reset) is int and 0 <= reset <= 1000000 else 0,
        'interactions': result,
    }
