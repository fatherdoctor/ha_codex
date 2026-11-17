"""Owner-selected protections shared by the worker, hook and sandbox launcher."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
from pathlib import Path

STATE_PATH = Path('/data/protection-checks/settings.json')
REQUIREMENTS_PATH = Path('/etc/codex/requirements.toml')
TEMPLATE_PATH = Path('/usr/share/ha_codex/requirements-template.toml')
COMMANDS = {
    'shell_commands': 'Shell commands',
    'rest_commands': 'REST commands',
    'command_line': 'Command-line integrations',
    'python_scripts': 'Python scripts',
    'templated_actions': 'Templated actions',
}
SELF = {
    'credential_reads': 'Credential reads',
    'full_access': 'Full access',
    'app_settings': 'App settings, policy and rules',
}
DEFAULTS = dict.fromkeys((*COMMANDS, *SELF), True)
CONFIRMATIONS = {
    'credential_reads': ('The assistant can read private credentials and use connections already available inside this app. '
                         'Continue?'),
    'full_access': ('The assistant can request full access inside this app container. '
                    'Normal approval and sandbox restrictions can then be removed. Continue?'),
    'app_settings': ('The assistant can change this app’s settings, policy, rules and these protection switches. '
                     'Continue?'),
}
KINDS = {
    'shell_command': 'shell_commands', 'rest_command': 'rest_commands',
    'rest': 'rest_commands', 'scrape': 'rest_commands',
    'command_line': 'command_line', 'python_script': 'python_scripts',
    'pyscript': 'python_scripts', 'template': 'templated_actions',
    'hassio': 'credential_reads',
}
_lock = threading.RLock()


def validate(values: object) -> dict[str, bool]:
    """Accept one complete, strictly typed set, never a permissive partial record."""
    if (not isinstance(values, dict) or set(values) != set(DEFAULTS)
            or any(type(value) is not bool for value in values.values())):
        raise ValueError('Protection settings must contain every switch as true or false.')
    return dict(values)


def _private_bytes(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError('Protection storage is not private.')
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError('Protection storage exceeds its limit.')
    return value


def _payload(values: dict[str, bool]) -> bytes:
    return json.dumps({'version': 1, 'protections': values}, sort_keys=True,
                      separators=(',', ':')).encode()


def _write_private(path: Path, record: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path: Path | None = None) -> dict[str, bool]:
    """A missing, unreadable, malformed or unsigned choice keeps every check on."""
    path = path or STATE_PATH
    try:
        record = json.loads(_private_bytes(path, 8192))
        if (set(record) != {'version', 'protections', 'signature'}
                or type(record['version']) is not int or record['version'] != 1):
            raise ValueError('Unrecognized protection record.')
        values = validate(record['protections'])
        key = _private_bytes(path.with_name('signing.key'), 32)
        if len(key) != 32 or not isinstance(record['signature'], str):
            raise ValueError('Invalid protection signature.')
        signature = hmac.new(key, _payload(values), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, record['signature']):
            raise ValueError('Protection record was not saved by this app.')
        return values
    except (OSError, ValueError, TypeError, KeyError):
        return dict(DEFAULTS)


def save(values: dict[str, bool], path: Path | None = None) -> None:
    """Save a signed private record atomically; only the administrator route calls this."""
    values = validate(values)
    path = path or STATE_PATH
    with _lock:
        changed = load(path) != values
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_path = path.with_name('signing.key')
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        key = _private_bytes(key_path, 32)
        if len(key) != 32:
            raise ValueError('Protection signing key is unavailable.')
        record = json.loads(_payload(values))
        record['signature'] = hmac.new(key, _payload(values), hashlib.sha256).hexdigest()
        _write_private(path, record)
        if changed:
            # An older Codex process can retain a permissive snapshot even if
            # the owner turns the switch back on before the app restarts.
            _write_private(path.with_name('pending.json'), {'restart_required': True})


def kind_enabled(kind: str, values: dict[str, bool]) -> bool:
    return values.get(KINDS.get(kind, kind), True)


def filtered_rules(rules: dict, values: dict[str, bool] | None = None) -> dict:
    values = load() if values is None else values
    return {**rules, **{key: [kind for kind in rules[key] if kind_enabled(kind, values)]
                       for key in ('reaches_anything', 'runs_on_load_platforms', 'runs_on_load_keys')},
            '_protections': dict(values)}


def status(path: Path | None = None) -> dict:
    path = path or STATE_PATH
    values = load(path)
    try:
        applied = validate(json.loads(_private_bytes(path.with_name('applied.json'), 8192)))
    except (OSError, ValueError, TypeError):
        applied = DEFAULTS
    restart_required = applied != values or path.with_name('pending.json').exists()
    off = [label for key, label in {**COMMANDS, **SELF}.items() if not values[key]]
    notice = 'Protections selected off: ' + ', '.join(off) + '.' if off else ''
    if restart_required:
        notice += ' Restart the app; previous managed rules and launch permissions remain until their Codex process restarts.'
    return {'protections': values, 'off': off,
            'notice': notice.strip(), 'restart_required': restart_required,
            'restart_note': 'Restart the app after a change to apply managed command rules and launch permissions.'}


def register(worker) -> None:
    """Keep the write endpoint inside the existing signed-in administrator boundary."""
    from flask import jsonify, request

    @worker.app.route('/protection-checks', methods=['GET', 'POST'])
    @worker.require_auth
    @worker.require_ingress
    def protection_checks():
        administrator, refusal = worker.ingress_admin()
        if administrator is None:
            return jsonify(ok=False, error=refusal), 403
        path = Path(worker.DATA_ROOT) / 'protection-checks/settings.json'
        if request.method == 'POST':
            if request.content_length is None or request.content_length > 1024:
                return jsonify(ok=False, error='Protection request exceeds its limit.'), 400
            data = request.get_json(silent=True)
            if (not isinstance(data, dict) or set(data) - {'kind', 'enabled', 'confirmed'}
                    or data.get('kind') not in DEFAULTS or type(data.get('enabled')) is not bool
                    or ('confirmed' in data and type(data['confirmed']) is not bool)):
                return jsonify(ok=False, error='Choose one known protection and true or false.'), 400
            kind = data['kind']
            if kind in SELF and data['enabled'] is False and data.get('confirmed') is not True:
                return jsonify(ok=False, confirmation_required=True, error=CONFIRMATIONS[kind]), 409
            with _lock:
                values = load(path)
                values[kind] = data['enabled']
                try:
                    save(values, path)
                except (OSError, ValueError):
                    return jsonify(ok=False, error='The protection change could not be confirmed. Reload the view to check its current state.'), 503
        response = jsonify(ok=True, **status(path))
        response.headers['Cache-Control'] = 'no-store'
        return response


def requirements(template: str, values: dict[str, bool]) -> str:
    """Keep the shipped catalogue, omitting only rules of an explicitly disabled group."""
    import tomllib
    data = tomllib.loads(template)
    modes = data['allowed_sandbox_modes']
    policies = data['allowed_approval_policies']
    if not values['full_access']:
        modes = list(dict.fromkeys([*modes, 'danger-full-access']))
        policies = list(dict.fromkeys([*policies, 'never']))
    lines = ['# Generated from the shipped template and the owner’s saved protection choices.',
             'allowed_sandbox_modes = ' + json.dumps(modes),
             'allowed_approval_policies = ' + json.dumps(policies),
             'allow_managed_hooks_only = ' + str(values['app_settings']).lower()]
    for rule in data.get('rules', {}).get('prefix_rules', []):
        why = rule.get('justification', '')
        group = ('credential_reads' if 'credential access' in why or 'Supervisor access' in why
                 else 'app_settings' if 'settings and policy' in why
                 else 'full_access' if 'full access' in why else None)
        if group and not values[group]:
            continue
        tokens = []
        for token in rule['pattern']:
            tokens.append('{ ' + ', '.join(key + ' = ' + json.dumps(value)
                                           for key, value in token.items()) + ' }')
        lines += ['', '[[rules.prefix_rules]]', 'pattern = [' + ', '.join(tokens) + ']',
                  'decision = ' + json.dumps(rule['decision']), 'justification = ' + json.dumps(why)]
    return '\n'.join(lines) + '\n'


def prepare_native() -> None:
    """Refresh the installed catalogue before Codex starts, never from a task argument.

    Development checkouts have no installed template. An app restart records
    that all processes start with the new choice; a single new process must
    not clear the notice for an older remote process that is still alive.
    """
    if not TEMPLATE_PATH.is_file():
        return
    with _lock:
        text = requirements(TEMPLATE_PATH.read_text(), load())
        fd, temporary = tempfile.mkstemp(dir=REQUIREMENTS_PATH.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, REQUIREMENTS_PATH)
        finally:
            Path(temporary).unlink(missing_ok=True)


def mark_app_start(values: dict[str, bool], path: Path | None = None) -> None:
    """Only the app startup, before it starts any Codex process, clears the notice."""
    path = path or STATE_PATH
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(path.with_name('applied.json'), validate(values))
    path.with_name('pending.json').unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-requirements', type=Path)
    parser.add_argument('--template', type=Path)
    parser.add_argument('--sandbox-state', action='store_true')
    args = parser.parse_args()
    if args.sandbox_state:
        values = load()
        print(int(values['credential_reads']), int(values['app_settings']))
    elif args.write_requirements and args.template:
        values = load()
        args.write_requirements.write_text(requirements(args.template.read_text(), values), encoding='utf-8')
        mark_app_start(values)
    else:
        parser.error('Choose the requirements output or sandbox state.')
