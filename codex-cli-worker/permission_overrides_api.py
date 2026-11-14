"""Fixed Core endpoints for permission overrides; no arbitrary URL, command or retry surface."""
from __future__ import annotations
from contextlib import closing
import hmac
import hashlib
import stat
import json
import os
from pathlib import Path
import re
import socketserver
import tempfile
import threading
import time
import uuid

from flask import jsonify, request
from permission_overrides import ApprovalRequired, Authority, Engine, Settings, bounded_json, entity, selected_guard_lines, guard_rules, GUARD_LINES, PRIVATE

IDENTIFIER = re.compile(r'[a-z0-9_-]{1,128}')

# Core sends a serialized field list. Return its shape only, never defaults/options/values.
SAFE_FLOW_FIELDS = frozenset(('host', 'port', 'name', 'username', 'title', 'scan_interval'))
FLOW_SCALARS = frozenset(('string', 'integer', 'float', 'boolean', 'select', 'multi_select'))
FLOW_SELECTORS = frozenset(('text', 'number', 'boolean', 'select', 'entity', 'device', 'area',
                            'label', 'duration', 'date', 'time', 'datetime', 'color_rgb', 'icon'))


def flow_result(value, submitted=None):
    if not isinstance(value, dict):
        raise ApprovalRequired('Integration setup could not be described; finish this step in Home Assistant.')
    # Also drop metadata that reflects a submitted value, even when identifier-shaped.
    submitted_text = set()
    def remember(item):
        if isinstance(item, str) and item:
            submitted_text.add(item)
        elif isinstance(item, dict):
            for child in item.values():
                remember(child)
        elif isinstance(item, list):
            for child in item:
                remember(child)
    remember(submitted or {})
    def safe_identifier(item):
        return (isinstance(item, str) and bool(IDENTIFIER.fullmatch(item))
                and not any(text in item for text in submitted_text))
    result = {key: value[key] for key in ('flow_id', 'type', 'step_id')
              if safe_identifier(value.get(key))}
    if value.get('type') != 'form':
        result['handoff'] = 'Continue or check this setup in Home Assistant.'
        return result
    schema = value.get('data_schema')
    if not isinstance(schema, list) or len(schema) > 64:
        raise ApprovalRequired('Integration form is unavailable or oversized; finish this step in Home Assistant.')
    fields, names, handoff = [], set(), False
    for field in schema:
        if not isinstance(field, dict) or not safe_identifier(field.get('name')):
            raise ApprovalRequired('Integration field could not be described; finish this step in Home Assistant.')
        name = field['name']
        if name in names or type(field.get('required', False)) is not bool:
            raise ApprovalRequired('Integration field could not be verified; finish this step in Home Assistant.')
        names.add(name)
        kind = field.get('type')
        secret = bool(name not in SAFE_FLOW_FIELDS or PRIVATE.search(name.lower())
                      or field.get('secret') or kind == 'password')
        selector = field.get('selector')
        if selector is not None:
            if not isinstance(selector, dict) or len(selector) != 1:
                kind = 'unsupported'
            else:
                kind, options = next(iter(selector.items()))
                secret = secret or (kind == 'text' and isinstance(options, dict) and options.get('type') == 'password')
                if kind not in FLOW_SELECTORS:
                    kind = 'unsupported'
        elif kind not in FLOW_SCALARS:
            kind = 'unsupported'
        if secret:
            kind = 'secret'
        handoff = handoff or kind in ('secret', 'unsupported')
        fields.append({'name': name, 'type': kind, 'required': field.get('required', False)})
    result['fields'] = fields
    errors = value.get('errors', {})
    if not isinstance(errors, dict) or len(errors) > 65:
        raise ApprovalRequired('Integration errors exceed their limit; finish this step in Home Assistant.')
    result['errors'] = {key: error for key, error in errors.items()
                        if (key == 'base' or key in names) and safe_identifier(key) and safe_identifier(error)}
    if handoff:
        result['handoff'] = 'Finish this step in Home Assistant. Password, secret or unsupported fields are never filled by the add-on.'
    bounded_json(result, 32768)
    return result


class Core:
    def __init__(self, worker):
        self.worker = worker
        self.deadline = time.monotonic() + 120

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ApprovalRequired('Action checking exceeded its deadline; request normal approval.')
        return min(15, remaining)

    def rest(self, method, path, data=None):
        token = self.worker.ha_token()
        if not token:
            raise ApprovalRequired('Core authentication is unavailable; request normal approval.')
        with self.worker.requests.request(method, self.worker.ha_api_url(path), json=data,
                headers={'Authorization': 'Bearer ' + token}, timeout=self.remaining(),
                allow_redirects=False, stream=True) as response:
            if response.status_code not in (200, 201, 202):
                raise ApprovalRequired('Core refused the action or its preflight; request normal approval.')
            raw = response.raw.read(1024 * 1024 + 1, decode_content=True)
            if len(raw) > 1024 * 1024:
                raise ApprovalRequired('Core response exceeds the action limit; request normal approval.')
            return json.loads(raw) if raw else {}

    def ws(self, kind, **data):
        token = self.worker.ha_token()
        if not token:
            raise ApprovalRequired('Core authentication is unavailable; request normal approval.')
        with closing(self.worker.websocket.create_connection(self.worker.ha_ws_url(), timeout=self.remaining())) as stream:
            initial = json.loads(stream.recv())
            if initial.get('type') == 'auth_required':
                stream.send(json.dumps({'type': 'auth', 'access_token': token}))
                initial = json.loads(stream.recv())
            if initial.get('type') != 'auth_ok':
                raise ApprovalRequired('Core authentication failed; request normal approval.')
            stream.send(json.dumps({**data, 'type': kind, 'id': 1}))
            for _ in range(32):
                stream.settimeout(self.remaining())
                raw = stream.recv()
                if len(raw) > 1024 * 1024:
                    break
                message = json.loads(raw)
                if message.get('id') == 1:
                    if message.get('success') is not True:
                        break
                    return message.get('result')
        raise ApprovalRequired('Core could not verify or complete the action; request normal approval.')

    def require_entity(self, target):
        row = self.rest('GET', 'states/' + entity(target))
        if not isinstance(row, dict) or row.get('entity_id') != target:
            raise ApprovalRequired('The entity is unavailable; request normal approval.')
        if target.startswith(('input_text.', 'text.')) and row.get('attributes', {}).get('mode') == 'password':
            raise ApprovalRequired('Credential targets are not available through overrides.')
        registry = self.ws('config/entity_registry/get', entity_id=target)
        if isinstance(registry, dict) and registry.get('platform') in ('codex_cli', 'ha_codex', 'hassio'):
            raise ApprovalRequired('The add-on and Supervisor settings cannot be changed through overrides.')
        return row


    def resolve(self, selector, service_domain=None):
        if isinstance(selector, str):
            selector = [item.strip() for item in selector.split(',')]
        if isinstance(selector, list):
            if not 1 <= len(selector) <= 64:
                raise ApprovalRequired('Target count exceeds the action limit; request normal approval.')
            return selector
        if not isinstance(selector, dict) or not selector or set(selector) - {'entity_id', 'device_id', 'area_id', 'label_id'}:
            raise ApprovalRequired('Targets require explicit entities, devices, areas or labels; request normal approval.')
        domain_filter = service_domain if set(selector) <= {'area_id', 'device_id', 'label_id'} else None
        entities = self.ws('config/entity_registry/list')
        devices = self.ws('config/device_registry/list')
        if not isinstance(entities, list) or not isinstance(devices, list) or len(entities) > 30000 or len(devices) > 30000:
            raise ApprovalRequired('The target registry is unavailable or oversized; request normal approval.')
        device_map = {row['id']: row for row in devices}
        result = []
        for key, values in selector.items():
            values = values if isinstance(values, list) else [values]
            if not values or len(values) > 64 or any(not isinstance(v, str) or not v for v in values):
                raise ApprovalRequired('An empty or dynamic target requires normal approval.')
            for value in values:
                matches = []
                for row in entities:
                    device = device_map.get(row.get('device_id'), {})
                    if ((key == 'entity_id' and row.get('entity_id') == value)
                            or (key == 'device_id' and row.get('device_id') == value)
                            or (key == 'area_id' and (row.get('area_id') or device.get('area_id')) == value)
                            or (key == 'label_id' and (value in row.get('labels', []) or value in device.get('labels', [])))):
                        if domain_filter is None or row['entity_id'].split('.')[0] == domain_filter:
                            matches.append(row['entity_id'])
                if not matches:
                    raise ApprovalRequired('A selector could not be fully resolved; request normal approval.')
                result.extend(matches)
                if len(set(result)) > 64:
                    raise ApprovalRequired('Target count exceeds the action limit; request normal approval.')
        return result

    def config_id(self, target):
        domain, object_id = entity(target).split('.')
        row = self.require_entity(target)
        identifier = object_id if domain == 'script' else row.get('attributes', {}).get('id')
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ApprovalRequired('The configuration identifier is unavailable; request normal approval.')
        return identifier


    def validate_service(self, service, data):
        domain, name = service.split('.')
        allowed = {'light': {'brightness', 'color_temp_kelvin', 'transition', 'rgb_color'},
                   'input_number': {'value'}, 'input_text': {'value'}, 'input_select': {'option'},
                   'script': {'variables'}, 'automation': {'skip_condition', 'stop_actions'},
                   'scene': {'transition'}, 'lock': {'code'}, 'alarm_control_panel': {'code'},
                   'cover': {'position'}, 'notify': {'message', 'title', 'target'},
                   'persistent_notification': {'message', 'title', 'notification_id'}}.get(domain, set())
        if set(data) - allowed:
            raise ApprovalRequired('Unsupported service data requires normal approval.')
        if domain in ('input_number', 'input_text') and name == 'set_value' and 'value' not in data:
            raise ApprovalRequired('A helper value is required.')
        if domain == 'input_select' and not isinstance(data.get('option'), str):
            raise ApprovalRequired('A helper option is required.')
        if domain in ('notify', 'persistent_notification') and name != 'dismiss' and not isinstance(data.get('message'), str):
            raise ApprovalRequired('A message is required.')
        if any(key in data and type(data[key]) not in (int, float) for key in ('brightness', 'transition', 'color_temp_kelvin', 'position')):
            raise ApprovalRequired('Numeric service data is invalid.')

    def validate_flow(self, operation, target):
        if operation == 'flow_continue':
            flow = self.rest('GET', 'config/config_entries/flow/' + target)
            handler = flow.get('handler') if isinstance(flow, dict) else None
            if not isinstance(handler, str) or handler in ('hassio', 'supervisor', 'codex_cli', 'ha_codex'):
                raise ApprovalRequired('This integration setup cannot be pre-approved.')

    def device_entry(self, target):
        rows = self.ws('config/device_registry/list')
        if not isinstance(rows, list) or len(rows) > 30000:
            raise ApprovalRequired('The device registry is unavailable; request normal approval.')
        row = next((row for row in rows if isinstance(row, dict) and row.get('id') == target), None)
        entries = row.get('config_entries', [row.get('config_entry_id')]) if row else []
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], str) or not IDENTIFIER.fullmatch(entries[0]):
            raise ApprovalRequired('Device removal requires one verified integration attachment; request normal approval.')
        return entries[0]

    def validate_operation(self, operation, target, data):
        if operation in ('update_device', 'delete_device', 'save_dashboard', 'delete_dashboard'):
            if not isinstance(target, str) or (target and not IDENTIFIER.fullmatch(target)):
                raise ApprovalRequired('Invalid registry or dashboard target.')
        if operation in ('update_entity', 'update_device'):
            allowed = {'name', 'area_id', 'labels'} if operation == 'update_entity' else {'name_by_user', 'area_id', 'labels'}
            if not data or set(data) - allowed:
                raise ApprovalRequired('Only display names, areas and labels can be organized.')
            if any(key != 'labels' and value is not None and (not isinstance(value, str) or len(value) > 128)
                   for key, value in data.items()):
                raise ApprovalRequired('Registry fields exceed their limits.')
            if 'labels' in data and (not isinstance(data['labels'], list) or len(data['labels']) > 64
                    or any(not isinstance(v, str) or not IDENTIFIER.fullmatch(v) for v in data['labels'])):
                raise ApprovalRequired('Labels must be explicit registry identifiers.')
        elif operation == 'rename_entity':
            new = data.get('new_entity_id')
            if set(data) != {'new_entity_id'} or entity(new).split('.')[0] != target.split('.')[0]:
                raise ApprovalRequired('An entity ID change must stay in its existing domain.')
        elif operation == 'save_dashboard':
            if set(data) != {'config'} or not isinstance(data['config'], dict):
                raise ApprovalRequired('Supply one UI dashboard configuration.')
            bounded_json(data, 512 * 1024)
        elif operation.startswith('delete_'):
            if data:
                raise ApprovalRequired('Delete operations do not accept extra data.')
            if operation == 'delete_device':
                self.device_entry(target)

    def references(self, target):
        # Core supplies semantic references; refuse rather than claim a complete hand-made text search.
        result = self.ws('search/related', item_type='entity', item_id=target)
        if not isinstance(result, dict):
            raise ApprovalRequired('The old-ID reference report is unavailable; request normal approval.')
        bounded_json(result)
        return {'core_related': result, 'coverage': 'Core related items; arbitrary text and external systems are not covered'}

    def backup_dashboard(self, target):
        value = self.ws('lovelace/config', **({'url_path': target} if target else {}))
        encoded = bounded_json(value, 512 * 1024)
        folder = Path(self.worker.DATA_ROOT) / 'permission-overrides-backups'
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.is_symlink():
            raise ApprovalRequired('The dashboard backup location is unsafe.')
        descriptor, name = tempfile.mkstemp(prefix='.dashboard-', dir=folder)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, folder / ('dashboard-' + uuid.uuid4().hex + '.json'))
        finally:
            Path(name).unlink(missing_ok=True)
        retained = sorted(folder.glob('dashboard-*.json'), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for old in retained[20:]:
            old.unlink()

    def execute_flow(self, operation, target, data):
        if operation == 'flow_start':
            if data:
                raise ApprovalRequired('Start the integration form first, then continue its flow.')
            value = self.rest('POST', 'config/config_entries/flow', {'handler': target})
        else:
            current = self.rest('GET', 'config/config_entries/flow/' + target)
            form = flow_result(current, data)
            if 'handoff' in form:
                return form
            names = {field['name'] for field in form['fields']}
            if set(data) - names or any(PRIVATE.search(key.lower()) for key in data):
                raise ApprovalRequired('Submit only described nonsecret fields; finish other inputs in Home Assistant.')
            value = self.rest('POST', 'config/config_entries/flow/' + target, data)
        return flow_result(value, data)

    def execute(self, plan):
        self.remaining()
        operation, target, data = plan['operation'], plan.get('target'), plan.get('data', {})
        if operation in ('service', 'reload', 'check_config', 'backup', 'restart'):
            if operation == 'service':
                service = plan['service']
            elif operation == 'reload':
                service = target + '.reload'
            else:
                service = {'check_config': 'homeassistant.check_config',
                           'backup': 'backup.create', 'restart': 'homeassistant.restart'}[operation]
            payload = {**data, **({'entity_id': target} if operation == 'service' and target else {})}
            self.rest('POST', 'services/' + service.replace('.', '/'), payload)
            return {'status': 'request accepted'}
        if operation in ('flow_start', 'flow_continue'):
            return self.execute_flow(operation, target, data)
        if operation in ('update_entity', 'rename_entity', 'delete_entity'):
            kind = 'remove' if operation == 'delete_entity' else 'update'
            self.ws('config/entity_registry/' + kind, entity_id=target, **data)
            return {'status': 'updated', **({'references': plan['references']} if 'references' in plan else {})}
        if operation in ('update_device', 'delete_device'):
            if operation == 'delete_device':
                entry = self.device_entry(target)
                self.ws('config/device_registry/remove_config_entry', device_id=target, config_entry_id=entry)
            else:
                self.ws('config/device_registry/update', device_id=target, **data)
            return {'status': 'updated'}
        if operation in ('save_dashboard', 'delete_dashboard'):
            self.backup_dashboard(target)
            if operation == 'save_dashboard':
                self.ws('lovelace/config/save', **data, **({'url_path': target} if target else {}))
            else:
                dashboards = self.ws('lovelace/dashboards/list')
                identifier = next((row['id'] for row in dashboards if row.get('url_path') == target), None)
                if not identifier:
                    raise ApprovalRequired('The dashboard could not be resolved.')
                self.ws('lovelace/dashboards/delete', dashboard_id=identifier)
            return {'status': 'updated', 'backup': 'saved privately before modification'}
        if operation in ('delete_script', 'delete_automation', 'delete_scene'):
            domain = operation[7:]
            self.rest('DELETE', f'config/{domain}/config/{self.config_id(target)}')
            return {'status': 'deleted'}
        raise ApprovalRequired('Unsupported action; request normal approval.')


class Service:
    def __init__(self, worker):
        self.worker = worker
        self.lock = threading.RLock()
        self.turns = {}

    def begin(self, task_id, capability, options):
        """Pin authority to the policy used to launch this exchange, never a later option."""
        policy = self.worker.protected_policy.effective_policy(options, self.worker.protected_policy.load_policy())
        with self.lock:
            source = getattr(self.worker, 'tasks', {}).get(task_id, {}).get('task_source', 'chat')
            authority = Authority(policy['sandbox'], policy['approval_policy'], policy['approvals_reviewer'],
                                  'worker' if source in ('chat', None) else 'unattended')
            try:
                snapshot = self.rules_snapshot()
                if snapshot != self.confirmed_rules():
                    raise ApprovalRequired('House rules need confirmation before a new exchange.')
            except ApprovalRequired:
                snapshot = None
            self.turns[task_id] = {'capability': capability, 'authority': authority,
                                   'rules': snapshot, 'changed': False}

    def end(self, task_id):
        with self.lock:
            self.turns.pop(task_id, None)

    def settings(self):
        return Settings(Path(self.worker.DATA_ROOT) / 'codex-home/house-rules/permission-overrides.json')

    def rules_path(self):
        return Path(self.worker.DATA_ROOT) / 'house-rules/confirmed.json'

    def rules_snapshot(self):
        """Bound the live file and refuse links, including replacements during the read."""
        path = Path(self.worker.CONFIG_ROOT) / 'AGENTS.md'
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError()
                raw = stream.read(65537)
            current = path.lstat()
            if len(raw) > 65536 or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise OSError()
            text = raw.decode('utf-8')
        except FileNotFoundError:
            text = None
        except (OSError, UnicodeError):
            raise ApprovalRequired('Owner guard rails could not be verified; request normal approval.') from None
        digest = hashlib.sha256(json.dumps(text).encode()).hexdigest()
        return {'text': text, 'sha256': digest}

    def confirmed_rules(self):
        path = self.rules_path()
        try:
            if path.parent.is_symlink() or path.is_symlink() or path.stat().st_size > 400000:
                raise ValueError()
            value = json.loads(path.read_text())
            if (set(value) != {'text', 'sha256'} or value['text'] is not None and not isinstance(value['text'], str)
                    or hashlib.sha256(json.dumps(value['text']).encode()).hexdigest() != value['sha256']):
                raise ValueError()
            return value
        except FileNotFoundError:
            # A fresh install with no instructions is a confirmed missing state.
            if self.rules_snapshot()['text'] is None:
                return self.rules_snapshot()
        except (OSError, ValueError, TypeError):
            pass
        raise ApprovalRequired('House rules changed since confirmation. Review and confirm them in Settings.')

    def confirm_rules(self, expected_digest=None):
        snapshot = self.rules_snapshot()
        if expected_digest is not None and expected_digest != snapshot['sha256']:
            raise ApprovalRequired('House rules changed during review. Review them again.')
        path = self.rules_path()
        if path.parent.is_symlink() or path.is_symlink():
            raise ApprovalRequired('The confirmed house-rules location is unsafe.')
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.confirmed-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(snapshot, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def guards(self):
        snapshot = self.rules_snapshot()
        if snapshot != self.confirmed_rules():
            raise ApprovalRequired('House rules changed since confirmation. Review and confirm them in Settings.')
        return snapshot['text'] or ''

    def guard_note(self):
        try:
            text = self.guards()
            rules = guard_rules(text)
            lines = [line for line in selected_guard_lines(text) if line in GUARD_LINES]
            if rules:
                notes = []
                for line in lines:
                    domains = GUARD_LINES[line]
                    if 'external' in domains:
                        affected = ('Security and people; Organize; Bigger changes; '
                                    'Apply checked changes for setup forms')
                    else:
                        affected = ('Security and people for direct services; Organize and Bigger changes '
                                    'for matching direct entity changes')
                    notes.append(line + ' Suspended groups/actions: ' + affected + '.')
                return 'Selected block limits (' + ', '.join(sorted(rules)) + '): ' + ' '.join(notes)
        except ApprovalRequired as error:
            return str(error)
        return ''

    def audit(self, task_id, event):
        recorder = getattr(self.worker, 'diagnostics', None)
        # No alternate logger can substitute for the required diagnostics recorder.
        if recorder is None:
            raise ApprovalRequired('Action diagnostics are unavailable; request normal approval.')
        try:
            from diagnostics_scrub import OVERRIDE_OPERATIONS
        except ImportError:
            raise ApprovalRequired('Typed action diagnostics are unavailable; request normal approval.') from None
        if event['operation'] not in OVERRIDE_OPERATIONS:
            raise ApprovalRequired('This action cannot be audited; request normal approval.')
        with recorder.lock:
            if task_id not in recorder.active or recorder.store.unavailable:
                raise ApprovalRequired('Active action diagnostics are unavailable; request normal approval.')
            failures = recorder.failures
            recorder.event(task_id, {'kind': 'override', **event})
            recorder.flush()
            if recorder.failures != failures or recorder.store.unavailable:
                raise ApprovalRequired('Action diagnostics could not be saved; request normal approval.')

    def dispatch(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'arguments', 'capability'}:
            return {'ok': False, 'approval_required': True, 'reason': 'Unsupported action envelope; request normal approval.'}
        worker, capability = self.worker, payload['capability']
        with self.lock:
            with worker.ha_data.lock:
                task_id = next((key for key, cap in worker.ha_data.capabilities.items()
                                if isinstance(capability, str) and capability and hmac.compare_digest(capability, cap)), None)
            if not task_id or not worker.ha_data.active(task_id, capability):
                return {'ok': False, 'approval_required': True, 'reason': 'Effective remote or expired-turn permissions cannot be verified; request normal approval.'}
            try:
                options = worker.task_options(task_id)
                if worker.protected_policy.policy_problem(options):
                    raise ApprovalRequired('The managed launch policy refuses this action.')
                policy = worker.protected_policy.effective_policy(options, worker.protected_policy.load_policy())
                current = Authority(policy['sandbox'], policy['approval_policy'], policy['approvals_reviewer'], 'worker')
                turn = self.turns.get(task_id, {})
                authority = turn.get('authority', Authority())
                if turn.get('capability') != capability or authority != current:
                    raise ApprovalRequired('Launch permissions changed; request normal approval.')
                def pinned_guards():
                    try:
                        live_rules = self.rules_snapshot()
                    except ApprovalRequired:
                        turn['changed'] = True
                        raise
                    if turn.get('changed') or live_rules != turn.get('rules'):
                        turn['changed'] = True
                        raise ApprovalRequired('House rules changed during this exchange; start a new exchange after confirming.')
                    return self.guards()
                engine = Engine(self.settings(), Core(worker), pinned_guards, lambda event: self.audit(task_id, event))
                return engine.dispatch(payload['arguments'], authority, lambda: worker.ha_data.active(task_id, capability))
            except Exception:
                return {'ok': False, 'approval_required': True, 'reason': 'Action authorization is unavailable; request normal approval.'}

    def serve(self, path):
        owner = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(20)
                raw = self.rfile.readline(1024 * 1024 + 1)
                try:
                    if len(raw) > 1024 * 1024:
                        raise ValueError()
                    value = owner.dispatch(json.loads(raw))
                except Exception:
                    value = {'ok': False, 'approval_required': True, 'reason': 'Action request unavailable; request normal approval.'}
                self.wfile.write(json.dumps(value).encode() + b'\n')
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        server.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def register(worker):
    service = Service(worker)
    @worker.app.route('/permissions-overrides', methods=['GET', 'POST'])
    @worker.require_auth
    @worker.require_ingress
    def permission_groups():
        administrator, refusal = worker.ingress_admin()
        if administrator is None:
            return jsonify({'ok': False, 'error': refusal}), 403
        try:
            with service.lock:
                if request.method == 'POST':
                    if request.content_length is None or request.content_length > 1024:
                        raise ValueError()
                    payload = request.get_json(silent=True)
                    if isinstance(payload, dict) and set(payload) == {'confirm_rules', 'expected_digest'} and payload['confirm_rules'] is True:
                        service.confirm_rules(payload['expected_digest'])
                    else:
                        service.settings().save(payload)
                        # First-use setup can establish the initial copy. A switch
                        # change must never approve edits the owner was not shown.
                        if not service.rules_path().exists() and not service.rules_path().is_symlink():
                            service.confirm_rules()
                response = jsonify({'ok': True, 'groups': service.settings().read(), 'guard_note': service.guard_note(),
                                    'rule_digest': service.rules_snapshot()['sha256'],
                                    'remote': 'Remote turns require normal approval in this version.'})
                response.headers['Cache-Control'] = 'no-store'
                return response
        except (ValueError, OSError, ApprovalRequired):
            return jsonify({'ok': False, 'error': 'Permissions settings could not be read or saved.'}), 400
    return service
