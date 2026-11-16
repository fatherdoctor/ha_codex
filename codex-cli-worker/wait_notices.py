"""Body-free descriptions of task waits; this module never answers a request."""
from __future__ import annotations

from datetime import datetime
import hashlib
import math
import re


def wait_minutes(value):
    """Use the shipped default for a damaged or older option."""
    return value if type(value) is int and 1 <= value <= 10080 else 60


def requested_time(value):
    """Accept only an explicit, timezone-aware request timestamp."""
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        stamp = date.timestamp() if date.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None
    return stamp if stamp is not None and math.isfinite(stamp) and stamp >= 0 else None


def task_waits(tasks, replied=()):
    """Describe every active wait, including ones below the notice threshold."""
    rows = []
    for task in tasks:
        task_id = task.get('task_id', '')
        if (not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id)
                or task.get('cancellation_requested')
                or task.get('status') not in ('running', 'waiting_for_input')):
            continue
        turn = str(task.get('current_turn_id') or 'legacy')

        def add(kind, identity, requested):
            since = requested_time(requested)
            if since is not None:
                wait_id = hashlib.sha256(f'{task_id}:{turn}:{kind}:{identity}'.encode()).hexdigest()[:32]
                rows.append({'wait_id': wait_id, 'task_id': task_id, 'kind': kind, 'waiting_since': since})

        if task.get('status') == 'waiting_for_input' and task.get('question'):
            add('question', str(task['question']), task.get('input_requested_at') or task.get('updated_at'))
        if task.get('status') == 'running':
            for approval in task.get('approvals') or []:
                if isinstance(approval, dict) and approval.get('status') == 'pending':
                    add('approval', approval.get('approval_id'), approval.get('requested_at'))
        mail = task.get('mailbox_wait')
        if (isinstance(mail, dict) and mail.get('turn_id') == task.get('current_turn_id')
                and mail.get('message_id') not in replied):
            add('mailbox', mail.get('message_id'), mail.get('requested_at'))
    return rows
