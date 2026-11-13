"""Fixed Core endpoints for permission overrides; no arbitrary URL, command or retry surface."""
from __future__ import annotations
from contextlib import closing
import hmac
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
from permission_overrides import ApprovalRequired, Authority, Engine, Settings, bounded_json, entity, security_mentions_outside

IDENTIFIER = re.compile(r'[a-z0-9_-]{1,128}')


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

    def resolve(self, selector):
        if isinstance(selector, str):
            selector = [item.strip() for item in selector.split(',')]
        if isinstance(selector, list):
            if not 1 <= len(selector) <= 64:
                raise ApprovalRequired('Target count exceeds the action limit; request normal approval.')
            for target in selector:
                self.require_entity(target)
            return selector
        if not isinstance(selector, dict) or not selector or set(selector) - {'entity_id', 'device_id', 'area_id', 'label_id'}:
            raise ApprovalRequired('Targets require explicit entities, devices, areas or labels; request normal approval.')
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
                        matches.append(row['entity_id'])
                if not matches:
                    raise ApprovalRequired('A selector could not be fully resolved; request normal approval.')
                result.extend(matches)
                if len(set(result)) > 64:
                    raise ApprovalRequired('Target count exceeds the action limit; request normal approval.')
        for target in result:
            self.require_entity(target)
        return result

    def config_id(self, target):
        domain, object_id = entity(target).split('.')
        row = self.require_entity(target)
        identifier = object_id if domain == 'script' else row.get('attributes', {}).get('id')
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ApprovalRequired('The configuration identifier is unavailable; request normal approval.')
        return identifier

    def definition(self, target):
        domain = entity(target).split('.')[0]
        if domain not in ('automation', 'script', 'scene'):
            raise ApprovalRequired('This indirect action cannot be inspected; request normal approval.')
        return self.rest('GET', f'config/{domain}/config/{self.config_id(target)}')

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

    def execute(self, plan):
        self.remaining()
        operation, target, data = plan['operation'], plan.get('target'), plan.get('data', {})
        if operation in ('service', 'reload', 'check_config', 'backup', 'restart'):
            service = (plan['service'] if operation == 'service' else target + '.reload' if operation == 'reload' else
                       {'check_config': 'homeassistant.check_config', 'backup': 'backup.create', 'restart': 'homeassistant.restart'}[operation])
            payload = {**data, **({'entity_id': target} if operation == 'service' and target else {})}
            self.rest('POST', 'services/' + service.replace('.', '/'), payload)
            return {'status': 'request accepted'}
        if operation in ('flow_start', 'flow_continue'):
            if operation == 'flow_start':
                if data:
                    raise ApprovalRequired('Start the integration form first, then continue its flow.')
                value = self.rest('POST', 'config/config_entries/flow', {'handler': target})
            else:
                value = self.rest('POST', 'config/config_entries/flow/' + target, data)
            # No defaults, submitted values, credential data or backend errors are returned.
            return {key: value[key] for key in ('flow_id', 'type', 'step_id') if isinstance(value.get(key), str)}
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
            self.turns[task_id] = (capability, Authority(policy['sandbox'], policy['approval_policy'], policy['approvals_reviewer'], 'worker'))

    def end(self, task_id):
        with self.lock:
            self.turns.pop(task_id, None)

    def settings(self):
        return Settings(Path(self.worker.DATA_ROOT) / 'codex-home/house-rules/permission-overrides.json')

    def guards(self):
        path = Path(self.worker.CONFIG_ROOT) / 'AGENTS.md'
        try:
            if path.is_symlink() or path.stat().st_size > 65536:
                raise OSError()
            return path.read_text()
        except FileNotFoundError:
            return ''
        except (OSError, UnicodeError):
            raise ApprovalRequired('Owner guard rails are unavailable; request normal approval.') from None

    def guard_note(self):
        try:
            mentioned = security_mentions_outside(self.guards())
        except ApprovalRequired:
            return 'Owner guard rails could not be verified. Actions require normal approval.'
        if mentioned:
            return ('Security and people requires normal approval because the instructions file '
                    'mentions a security target outside the marked guard-rails block. '
                    'Put selected limits inside the owner-selected agent guard rails block.')
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
                pinned_capability, authority = self.turns.get(task_id, ('', Authority()))
                if pinned_capability != capability or authority != current:
                    raise ApprovalRequired('Launch permissions changed; request normal approval.')
                engine = Engine(self.settings(), Core(worker), self.guards, lambda event: self.audit(task_id, event))
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
                    service.settings().save(request.get_json(silent=True))
                response = jsonify({'ok': True, 'groups': service.settings().read(), 'guard_note': service.guard_note(),
                                    'remote': 'Remote turns require normal approval in this version.'})
                response.headers['Cache-Control'] = 'no-store'
                return response
        except (ValueError, OSError, ApprovalRequired):
            return jsonify({'ok': False, 'error': 'Permissions settings could not be read or saved.'}), 400
    return service
