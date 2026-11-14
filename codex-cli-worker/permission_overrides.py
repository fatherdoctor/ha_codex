"""Owner-selected action groups. Only worker-created Authority objects can authorize writes."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from relay_reads import ENTITY, PRIVATE

DEFAULTS = {'apply': True, 'everyday': True, 'organize': True, 'bigger': False, 'security': False}
RELOADS = frozenset(('automation', 'script', 'scene', 'template', 'group'))
SECURITY = frozenset(('lock', 'alarm_control_panel', 'cover', 'camera', 'notify', 'persistent_notification'))
SERVICES = {
    'light': {'turn_on', 'turn_off', 'toggle'},
    'switch': {'turn_on', 'turn_off', 'toggle'},
    'input_boolean': {'turn_on', 'turn_off', 'toggle'},
    'input_number': {'set_value'}, 'input_select': {'select_option'}, 'input_text': {'set_value'},
    'scene': {'turn_on'}, 'automation': {'turn_on', 'turn_off', 'toggle', 'trigger'},
    'script': {'turn_on'}, 'lock': {'lock', 'unlock', 'open'},
    'alarm_control_panel': {'alarm_disarm', 'alarm_arm_home', 'alarm_arm_away', 'alarm_arm_night',
                            'alarm_arm_vacation', 'alarm_arm_custom_bypass', 'alarm_trigger'},
    'cover': {'open_cover', 'close_cover', 'stop_cover', 'set_cover_position'},
    'camera': {'turn_on', 'turn_off', 'enable_motion_detection', 'disable_motion_detection'},
    'persistent_notification': {'create', 'dismiss'},
}
OPERATIONS = frozenset(('reload', 'check_config', 'flow_start', 'flow_continue', 'service',
                        'update_entity', 'update_device', 'rename_entity', 'save_dashboard',
                        'delete_entity', 'delete_device', 'delete_automation', 'delete_script',
                        'delete_scene', 'delete_dashboard', 'backup', 'restart'))
FIELDS = {'operation', 'target', 'data', 'service'}
MARKERS = ('<!-- BEGIN owner-selected agent guard rails -->', '<!-- END owner-selected agent guard rails -->')
GUARD_LINES = {
    '- Do not lock or unlock locks.': {'lock'},
    '- Do not arm or disarm the alarm.': {'alarm_control_panel'},
    '- Do not open or close covers, including garage doors.': {'cover'},
    '- Do not access or control cameras.': {'camera'},
    '- Do not send messages or notifications to people without my OK.': {'notify', 'persistent_notification'},
    '- Do not publish content or changes externally.': {'external'},
}


class ApprovalRequired(ValueError):
    """Fixed, worker-authored explanation; never a backend response or exception string."""


@dataclass(frozen=True)
class Authority:
    sandbox: str = 'unknown'
    approval: str = 'unknown'
    reviewer: str = 'unknown'
    source: str = 'remote'

    def mode(self):
        # Production constructs this from its protected launch policy. MCP input is never used here.
        if self.source != 'worker' or self.sandbox != 'workspace-write':
            return 'unknown'
        if self.approval == 'on-request' and self.reviewer == 'user':
            return 'default'
        # Conditional contracts for CX-PERM; today's worker policy cannot produce either.
        if self.approval == 'on-request' and self.reviewer == 'auto_review':
            return 'auto_review'
        return 'unknown'


class Settings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 1024:
                    raise ValueError()
                value = json.loads(self.path.read_text())
                self.validate(value)
                return value
            except FileNotFoundError:
                return dict(DEFAULTS)
            except (OSError, ValueError, TypeError):
                raise ApprovalRequired('Permissions settings are unavailable; request normal approval.') from None

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) != set(DEFAULTS) or any(type(v) is not bool for v in value.values()):
            raise ValueError('Supply exactly five boolean permission groups.')

    def save(self, value):
        self.validate(value)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix='.permission-overrides-', dir=self.path.parent)
            try:
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(value, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)


def entity(value):
    if not isinstance(value, str) or not ENTITY.fullmatch(value) or PRIVATE.search(value.split('.', 1)[1]):
        raise ApprovalRequired('Use an exact noncredential entity target; request normal approval otherwise.')
    return value


def bounded_json(value, maximum=65536):
    try:
        text = json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ApprovalRequired('Action data is not bounded JSON; request normal approval.') from None
    if len(text.encode()) > maximum:
        raise ApprovalRequired('Action data exceeds its size limit; request normal approval.')
    if '{{' in text or '{%' in text:
        raise ApprovalRequired('Dynamic templates require normal approval.')
    return text


def selected_guard_lines(text):
    """Read only the exact marked block; other prose remains the agent's instructions."""
    first, last = MARKERS
    if first not in text and last not in text:
        return []
    if text.count(first) != 1 or text.count(last) != 1 or text.index(last) < text.index(first):
        raise ApprovalRequired('Owner guard rails could not be verified; request normal approval.')
    selected = text.split(first, 1)[1].split(last, 1)[0]
    return [line.strip() for line in selected.splitlines() if line.strip()]


def guard_rules(text):
    """Only exact selected limits are machine-verifiable; custom block lines ask."""
    refused = set()
    for line in selected_guard_lines(text):
        if line not in GUARD_LINES:
            raise ApprovalRequired('Custom limits in the guard-rails block require normal approval.')
        refused.update(GUARD_LINES[line])
    return refused


def service_group(service):
    if not isinstance(service, str) or not re.fullmatch(r'[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*', service):
        raise ApprovalRequired('Unsupported action; request normal approval.')
    domain, name = service.split('.')
    if domain == 'notify':
        return 'security'
    if domain == 'script':
        # Script execution is supported; these control services are not.
        if name in ('reload', 'turn_off', 'toggle'):
            raise ApprovalRequired('Unsupported action; request normal approval.')
        return 'bigger'
    if name not in SERVICES.get(domain, set()):
        raise ApprovalRequired('Unsupported action; request normal approval.')
    if domain in SECURITY:
        return 'security'
    return 'everyday'


class Engine:
    """Verify the direct action before a single write; backend failures never auto-retry."""
    def __init__(self, settings, backend, guards, observer):
        self.settings = settings
        self.backend = backend
        self.guards = guards
        self.observer = observer
        self.lock = threading.RLock()

    def dispatch(self, request, authority, still_active=lambda: True):
        operation = request.get('operation') if isinstance(request, dict) else None
        operation = operation if operation in OPERATIONS else 'unknown'
        started = time.monotonic()
        group = 'unknown'
        def event(outcome):
            return {'operation': operation, 'group': group, 'outcome': outcome,
                    'decision': 'preapproved' if outcome in ('requested', 'completed', 'unknown') else 'refused',
                    'duration_seconds': time.monotonic() - started}
        def final_event(outcome):
            try:
                self.observer(event(outcome))
            except Exception:
                pass
        with self.lock:
            started_write = False
            try:
                if authority.mode() not in ('default', 'auto_review'):
                    raise ApprovalRequired('Effective write permissions cannot be verified; request normal approval.')
                if not isinstance(request, dict) or set(request) - FIELDS or operation == 'unknown':
                    raise ApprovalRequired('Unsupported action fields; request normal approval.')
                bounded_json(request, 1024 * 1024)
                groups = self.settings.read()
                rules = guard_rules(self.guards())
                checks = set()
                plan = self.plan(request, groups, rules, checks)
                group = plan['group']
                def recheck_authorization():
                    if (not still_active() or groups != self.settings.read()
                            or rules != guard_rules(self.guards())):
                        raise ApprovalRequired('Permissions changed or the exchange ended; request normal approval.')
                # Recheck settings, confirmed owner rules and the live turn before dispatch.
                recheck_authorization()
                # A failed audit must stop BEFORE the write. The callback sees no payload or raw target.
                self.observer(event('requested'))
                recheck_authorization()
                # Verify only the entities directly named by this action after the audit.
                for target in checks:
                    self.backend.require_entity(target)
                # Target reads can block while the task or owner permissions change.
                recheck_authorization()
                started_write = True
                result = self.backend.execute(plan)
            except ApprovalRequired as exc:
                final_event('unknown' if started_write else 'refused')
                if started_write:
                    return {'ok': False, 'approval_required': False, 'do_not_retry': True,
                            'reason': 'The action outcome is unknown. Check its state before any further action; do not retry automatically.'}
                return {'ok': False, 'approval_required': True, 'reason': str(exc)}
            except Exception:
                final_event('unknown' if started_write else 'refused')
                return {'ok': False, 'approval_required': not started_write, 'do_not_retry': started_write,
                        'reason': 'The action could not be verified or completed. Its outcome may be unknown; do not retry automatically.'}
            # Observer failure after a write must never change the execution result.
            final_event('completed')
            return {'ok': True, 'performed': plan['description'], 'result': result,
                    'answer_instruction': 'Name this pre-approved action in your answer.'}


    def allowed(self, group, groups):
        if not groups[group]:
            raise ApprovalRequired('This action group is off; request normal approval.')

    def targets(self, selector, domain=None):
        rows = self.backend.resolve(selector, service_domain=domain)
        if not isinstance(rows, list) or not rows or len(rows) > 64:
            raise ApprovalRequired('Targets could not be fully resolved; request normal approval.')
        if domain and isinstance(selector, dict) and set(selector) <= {'area_id', 'device_id', 'label_id'}:
            rows = [value for value in rows if isinstance(value, str) and value.split('.')[0] == domain]
        targets = list(dict.fromkeys(entity(value) for value in rows))
        if domain and isinstance(selector, dict) and set(selector) <= {'area_id', 'device_id', 'label_id'}:
            if not targets:
                raise ApprovalRequired('No targets of the service domain were found; request normal approval.')
        for target in targets:
            self.backend.require_entity(target)
        return targets

    @staticmethod
    def guard_effect(domain, rules):
        if domain in rules or ('external' in rules and domain in SECURITY):
            raise ApprovalRequired('Owner guard rails restrict this action; request normal approval.')

    def effect(self, service, selector, data, groups, rules, checks):
        """Judge this service and its direct targets, never the owner's downstream setup."""
        bounded_json(data)
        group = service_group(service)
        self.allowed(group, groups)
        self.backend.validate_service(service, data)
        domain = service.split('.')[0]
        if domain == 'script' and service != 'script.turn_on':
            selector = [service]
        self.guard_effect(domain, rules)
        targets = [] if domain in ('notify', 'persistent_notification') else self.targets(selector, domain)
        if any(target.split('.')[0] != domain for target in targets):
            raise ApprovalRequired('The service and target domains disagree; request normal approval.')
        checks.update(targets)
        return targets

    def plan(self, request, groups, rules, checks):
        operation = request['operation']
        target, data = request.get('target'), request.get('data', {})
        if not isinstance(data, dict):
            raise ApprovalRequired('Action data must be an object; request normal approval.')
        if operation in ('reload', 'check_config', 'flow_start', 'flow_continue'):
            group = 'apply'
        elif operation in ('update_entity', 'update_device', 'save_dashboard'):
            group = 'organize'
        else:
            group = 'bigger'
        if operation == 'service':
            service = request.get('service')
            targets = self.effect(service, target, data, groups, rules, checks)
            self.backend.validate_service(service, data)
            return {'operation': operation, 'group': service_group(service), 'service': service, 'target': targets, 'data': data,
                    'description': service + (' for ' + ', '.join(targets) if targets else '')}
        if 'service' in request:
            raise ApprovalRequired('This operation does not accept a service; request normal approval.')
        self.allowed(group, groups)
        if 'external' in rules and operation not in ('reload', 'check_config', 'update_entity', 'update_device'):
            raise ApprovalRequired('The external-publication guard rail requires normal approval.')
        if operation == 'reload':
            if target not in RELOADS or data:
                raise ApprovalRequired('Unsupported reload request; request normal approval.')
        if operation in ('check_config', 'backup', 'restart') and (target is not None or data):
            raise ApprovalRequired('This operation takes no target or data; request normal approval.')
        if operation in ('flow_start', 'flow_continue'):
            if not isinstance(target, str) or not re.fullmatch(r'[a-z0-9_-]{1,128}', target):
                raise ApprovalRequired('Unsupported integration setup target; request normal approval.')
            if operation == 'flow_start' and target in ('hassio', 'supervisor', 'codex_cli', 'ha_codex'):
                raise ApprovalRequired('Supervisor and add-on settings are never available through overrides.')
            self.backend.validate_flow(operation, target)
        if operation in ('update_entity', 'rename_entity', 'delete_entity', 'delete_automation', 'delete_script', 'delete_scene'):
            target = entity(target)
            domain = target.split('.')[0]
            if domain in rules or (domain in SECURITY and not groups['security']):
                raise ApprovalRequired('The target requires normal approval.')
            self.backend.require_entity(target)
            if operation.startswith('delete_') and operation != 'delete_entity' and domain != operation[7:]:
                raise ApprovalRequired('The deletion type and target disagree; request normal approval.')
        if operation in ('update_device', 'delete_device'):
            for item in self.targets({'device_id': target}):
                domain = item.split('.')[0]
                if domain in rules or (domain in SECURITY and not groups['security']):
                    raise ApprovalRequired('A device entity requires normal approval.')
        self.backend.validate_operation(operation, target, data)
        description = operation.replace('_', ' ')
        if target is not None:
            description += ' for ' + (target or 'default dashboard')
        result = {'operation': operation, 'group': group, 'target': target,
                  'data': data, 'description': description}
        if operation == 'rename_entity':
            result['description'] += ' to ' + data['new_entity_id']
            result['references'] = self.backend.references(target)
        return result
