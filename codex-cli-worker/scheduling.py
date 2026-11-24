"""Durable schedules and their run log; no process or network calls."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import NamedTuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Build-time switch for scheduled runs. While it is True (the 0.4 series), the pages offer schedules, Codex gets
# the scheduling tools and saved schedules fire. Set it to False to hide all of it again: the code and the saved
# schedules then stay in place, and nothing fires.
SCHEDULED_RUNS = True

MAX_SCHEDULES = 100
MAX_PROPOSALS = 20
MAX_DAILY_RUNS = 64
MAX_SCHEDULE_DAILY_RUNS = 24
MAX_RUNS = 500
# Fields only the owner's editor may set: the chat a schedule was made in, and its model.
OWNER_FIELDS = frozenset({'chat_id', 'model'})
CHAT_ID = re.compile(r'(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127})?')
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')
class Busy(ValueError):
    pass


class Placed(NamedTuple):
    """Where the worker started a run: the chat (task id) and the exchange (turn id) in it."""
    task_id: str
    turn_id: str


ACTIVE = ('reserved', 'queued', 'running', 'waiting_for_approval', 'waiting_for_input')


def text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label} must be nonempty text, at most {limit} characters.')
    return value.strip()


def cron_fields(expression):
    expression = text(expression, 'cron', 256)
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError('Use five numeric cron fields: minute hour day month weekday (Home Assistant time).')
    fields = []
    for part, low, high in zip(parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        values = set()
        for item in part.split(','):
            base, *steps = item.split('/')
            if len(steps) > 1 or (steps and (not steps[0].isdigit() or int(steps[0]) < 1)):
                raise ValueError('Invalid cron step.')
            step = int(steps[0]) if steps else 1
            if base == '*':
                start, end = low, high
            elif '-' in base:
                edges = base.split('-')
                if len(edges) != 2 or not all(v.isdigit() for v in edges):
                    raise ValueError('Invalid cron range.')
                start, end = map(int, edges)
            elif base.isdigit():
                start = int(base); end = high if steps else start
            else:
                raise ValueError('Cron fields must use numbers, *, lists, ranges or steps.')
            if not low <= start <= end <= high or step > high - low + 1:
                raise ValueError('Cron value is out of range.')
            values.update(range(start, end + 1, step))
        fields.append(values)
    fields[4] = {0 if day == 7 else day for day in fields[4]}
    # A leading wildcard keeps the other day field restrictive, including */N.
    return fields, parts[2].startswith('*'), parts[4].startswith('*')


def local_timestamp(date, zone):
    """Use the first repeated occurrence and reject nonexistent local wall times."""
    aware = date.replace(tzinfo=ZoneInfo(zone), fold=0)
    stamp = aware.timestamp()
    if datetime.fromtimestamp(stamp, ZoneInfo(zone)).replace(tzinfo=None) != date:
        raise ValueError('This local time does not exist because of a clock change.')
    return stamp


def next_due(spec, after, zone='UTC'):
    """Cron follows Home Assistant wall time; repeated minutes run only once."""
    if spec['kind'] == 'once':
        return spec['at'] if spec['at'] > after else None
    if spec['kind'] == 'interval':
        return after + spec['seconds']
    fields, dom_any, dow_any = cron_fields(spec['expression'])
    minutes, hours, days, months, weekdays = fields
    start = datetime.fromtimestamp(after, ZoneInfo(zone)).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    for offset in range(367):
        day = start + timedelta(days=offset)
        if day.month not in months:
            continue
        dom = day.day in days; dow = (day.weekday() + 1) % 7 in weekdays
        if not ((dom and dow) if dom_any or dow_any else (dom or dow)):
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                try:
                    stamp = local_timestamp(day.replace(hour=hour, minute=minute), zone)
                except ValueError:
                    continue
                if stamp > after:
                    return stamp
    raise ValueError('Cron has no occurrence in the next 366 days.')


def starts_approved(owner, spec):
    """Return whether a schedule runs without the owner's approval.

    The owner's own schedules run as saved. A one-time wakeup that Codex
    proposes runs once, at its time, as the next message in the chat it came
    from, and that run asks for permission like any other. A repeating proposal
    waits for the owner, because it would go on running by itself.
    """
    return owner or spec['kind'] == 'once'


# A one-time schedule that ran is off with no next run; it cannot run again until the owner edits it, so it does
# not use up a place under MAX_SCHEDULES (otherwise every wakeup Codex ever set would fill the owner's room).
SPENT = "enabled=0 AND next_due IS NULL AND json_extract(spec, '$.kind')='once'"


def counted(db, approved, schedule_id=''):
    """Return how many schedules hold a place under the limit of their kind (approved or proposals)."""
    return db.execute(f'SELECT count(*) FROM schedules WHERE approved=? AND id!=? AND NOT ({SPENT})',
                      (approved, schedule_id)).fetchone()[0]


def local_text(stamp, zone):
    """Say a time in the home's time zone, for example 'Tuesday, November 24, 2026 at 6:50 PM CST'."""
    moment = datetime.fromtimestamp(stamp, ZoneInfo(zone))
    hour = moment.hour % 12 or 12
    return f'{moment:%A, %B} {moment.day}, {moment.year} at {hour}:{moment:%M %p %Z}'


def owner_fields(payload):
    """Return the chat and model an owner's request names; a field left out is left out.

    Only the owner's editor sets these. The chat is where the schedule was made,
    so the page can show it there. The model is None for the add-on's own model;
    schedule_api checks a named one against the models the worker offers.
    """
    fields = {}
    if 'chat_id' in payload:
        chat_id = payload['chat_id']
        if not isinstance(chat_id, str) or CHAT_ID.fullmatch(chat_id) is None:
            raise ValueError('chat_id must be the id of a chat, or empty.')
        fields['chat_id'] = chat_id
    if 'model' in payload:
        model = payload['model']
        if model is not None and (not isinstance(model, str) or MODEL_ID.fullmatch(model) is None):
            raise ValueError('model must be the id of a model, or null for the add-on default.')
        fields['model'] = model
    return fields


def validate(payload, now, zone="UTC", *, owner=False):
    allowed = {'name', 'prompt', 'mode', 'spec'}
    if not isinstance(payload, dict) or set(payload) - (allowed | OWNER_FIELDS if owner else allowed):
        raise ValueError('A schedule accepts only name, prompt, mode, spec, model and chat_id.' if owner
                         else 'A schedule accepts only name, prompt, mode and spec.')
    if zone is None:
        raise ValueError('Home Assistant time zone is unavailable; schedule entry is paused.')
    ZoneInfo(zone)
    name = text(payload.get('name'), 'name', 80)
    prompt = text(payload.get('prompt'), 'prompt', 16000)
    if '[redacted]' in prompt:
        raise ValueError('A schedule cannot save redaction placeholders. Enter a complete new prompt.')
    mode = payload.get('mode', 'default')
    if mode not in ('default', 'read-only'):
        raise ValueError('Mode must be default or read-only.')
    spec = payload.get('spec')
    if not isinstance(spec, dict):
        raise ValueError('spec must describe interval, cron or once.')
    kind = spec.get('kind')
    if kind == 'interval' and set(spec) == {'kind', 'seconds'}:
        seconds = spec['seconds']
        if type(seconds) is not int or not 60 <= seconds <= 366 * 86400:
            raise ValueError('Interval must be an integer from 60 seconds through 366 days.')
        spec = {'kind': kind, 'seconds': seconds}
    elif kind == 'cron' and set(spec) == {'kind', 'expression'}:
        cron_fields(spec['expression'])
        spec = {'kind': kind, 'expression': ' '.join(spec['expression'].split())}
    elif kind == 'once' and set(spec) == {'kind', 'at'}:
        at = spec['at']
        if isinstance(at, str):
            try:
                date = datetime.fromisoformat(at.replace('Z', '+00:00'))
                at = local_timestamp(date, zone) if date.tzinfo is None else date.timestamp()
            except (ValueError, OverflowError):
                raise ValueError('One-off time is invalid or does not exist in the Home Assistant time zone.') from None
        if type(at) not in (int, float) or not math.isfinite(at) or not now < at <= now + 366 * 86400:
            raise ValueError('One-off time must be in the next 366 days.')
        spec = {'kind': kind, 'at': at}
    else:
        raise ValueError('Invalid schedule specification.')
    value = {'name': name, 'prompt': prompt, 'mode': mode, 'spec': spec, 'next_due': next_due(spec, now, zone)}
    value.update(owner_fields(payload))  # Empty for an agent: the check above refused these fields.
    return value


# The run log. task_id is the chat the run used: a run of a schedule made in a chat continues that chat, so many
# runs can share one. turn_id is the run's own exchange in it ('' for a run saved before version 4).
RUNS_COLUMNS = """
                    id TEXT PRIMARY KEY, schedule_id TEXT, task_id TEXT NOT NULL,
                    due REAL NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', turn_id TEXT NOT NULL DEFAULT ''"""


def upgrade_runs(db):
    """Upgrade a run log of schema version 3 or earlier (0.4 and before) to version 4.

    Version 3 held task_id UNIQUE, one new chat per run. SQLite cannot drop that
    constraint in place, so the table is copied once into the version 4 shape.
    A version 3 worker after a rollback still reads and writes the new table.
    """
    if 'turn_id' in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
        return
    db.execute('ALTER TABLE runs RENAME TO runs_version_3')
    db.execute(f'CREATE TABLE runs ({RUNS_COLUMNS})')
    db.execute('INSERT INTO runs(id,schedule_id,task_id,due,created,status,error) '
               'SELECT id,schedule_id,task_id,due,created,status,error FROM runs_version_3')
    db.execute('DROP TABLE runs_version_3')


def run_status(task, turn_id):
    """Return a run's status as its chat records it.

    A run that later messages followed has ended: its exchange keeps its own
    outcome, and one the owner answered or moved past counts as completed.
    Otherwise (the run is the current exchange, or the chat lost its exchange
    list) the chat's status is the run's.
    """
    turn = next((item for item in task.get('turns') or [] if item.get('turn_id') == turn_id), None) if turn_id else None
    if turn is not None and task.get('current_turn_id') != turn_id:
        status = turn.get('status', 'completed')
        return 'completed' if status in ACTIVE else status
    pending = any(a.get('status') == 'pending' for a in task.get('approvals') or [])
    return 'waiting_for_approval' if pending else task.get('status', 'interrupted')


class Store:
    """Transactions protect occurrence reservation and owner approval from concurrent edits."""
    def __init__(self, path, zone="UTC"):
        self.timezone = zone
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connect() as db:
            schema = '''
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
                    mode TEXT NOT NULL, spec TEXT NOT NULL, approved INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, next_due REAL, updated REAL NOT NULL, revision TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT 'owner', chat_id TEXT NOT NULL DEFAULT '', model TEXT);
                CREATE TABLE IF NOT EXISTS runs ({RUNS_COLUMNS});
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            '''.replace('{RUNS_COLUMNS}', RUNS_COLUMNS)
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)
            # The author, chat_id and model columns are in the table from the start. A schedules table made
            # before 0.3.6 without them is not upgraded here (only test installs ever held one).
            # A file from version 0.3 or earlier also holds the tables of the lead agent channel and the
            # mailbox (mailbox, mailbox_reads, mailbox_deliveries, mailbox_threads, mail_usage, lead_pending,
            # lead_threads) and the lead token's hash in settings. Nothing reads them any more. They are
            # left in place, so an older version still finds its messages after a rollback.
            upgrade_runs(db)
            db.execute('PRAGMA user_version=4')
        if zone is not None:
            self.set_timezone(zone, time.time())

    def set_timezone(self, zone, now):
        ZoneInfo(zone)
        with self.connect() as db:
            previous = db.execute("SELECT value FROM settings WHERE key='ha_timezone'").fetchone()
            if (previous is None and zone != 'UTC') or (previous is not None and previous[0] != zone):
                for row in db.execute('SELECT id,spec FROM schedules WHERE enabled=1'):
                    spec = json.loads(row['spec'])
                    if spec['kind'] == 'cron':
                        due = next_due(spec, now, zone)
                        db.execute('UPDATE schedules SET next_due=? WHERE id=?', (due, row['id']))
            db.execute("INSERT OR REPLACE INTO settings VALUES('ha_timezone',?)", (zone,))
        self.timezone = zone

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA journal_mode').fetchone()[0] != 'persist':
            db.execute('PRAGMA journal_mode=PERSIST')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def schedule(row):
        value = dict(row); value['spec'] = json.loads(value['spec'])
        value['approved'] = bool(value['approved']); value['enabled'] = bool(value['enabled'])
        return value

    def schedules(self):
        with self.connect() as db:
            return [self.schedule(row) for row in db.execute('SELECT * FROM schedules ORDER BY updated DESC, id')]

    def save(self, payload, *, owner, now, schedule_id=None, author=None, made_in=None):
        """Save a schedule; made_in is the chat a Codex proposal came from.

        The worker sets made_in from the exchange that sent the proposal, never
        from Codex's arguments, so the run of a proposal continues that chat as
        a schedule the owner makes in a chat does.
        """
        value = validate(payload, now, self.timezone, owner=owner)
        if made_in is not None:
            value['chat_id'] = made_in
        author = 'owner' if owner else author
        if author not in ('owner', 'codex'):
            raise ValueError('Invalid schedule author.')
        with self.connect() as db:
            # Codex only proposes new schedules (propose_schedule); an edit of a saved one is the owner's.
            if schedule_id and not owner:
                raise LookupError('Schedule not found.')
            old = db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone() if schedule_id else None
            if schedule_id and old is None:
                raise LookupError('Schedule not found.')
            approved = int(starts_approved(owner, value['spec']))
            cap = MAX_SCHEDULES if approved else MAX_PROPOSALS
            if counted(db, approved, schedule_id or '') >= cap:
                raise ValueError('Approved schedule limit reached.' if approved else 'Proposal limit reached.')
            schedule_id = schedule_id or uuid.uuid4().hex
            # An edit that does not name the chat or the model keeps the saved one.
            chat_id = value.get('chat_id', old['chat_id'] if old else '')
            model = value.get('model', old['model'] if old else None)
            db.execute('INSERT OR REPLACE INTO schedules(id,name,prompt,mode,spec,approved,enabled,next_due,updated,revision,author,chat_id,model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                schedule_id, value['name'], value['prompt'], value['mode'], json.dumps(value['spec']),
                approved, approved, value['next_due'], now, uuid.uuid4().hex, author, chat_id, model))
            return self.schedule(db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone())

    def action(self, schedule_id, action, now, revision=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
            if row is None:
                raise LookupError('Schedule not found.')
            if action == 'delete':
                db.execute('DELETE FROM schedules WHERE id=?', (schedule_id,)); return
            if action not in ('approve', 'pause', 'resume'):
                raise ValueError('Unknown schedule action.')
            if action == 'approve' and revision != row['revision']:
                raise ValueError('This proposal changed. Refresh and review it before approving.')
            if action == 'approve' and not row['approved'] and counted(db, 1) >= MAX_SCHEDULES:
                raise ValueError('Approved schedule limit reached.')
            approved = bool(row['approved']) or action == 'approve'
            if action == 'resume' and not approved:
                raise ValueError('Approve this proposal before enabling it.')
            due = row['next_due']
            if action != 'pause':
                due = next_due(json.loads(row['spec']), now, self.timezone)
                if due is None:
                    raise ValueError('This one-off time has passed. Edit it before enabling it.')
            db.execute('UPDATE schedules SET approved=?,enabled=?,next_due=?,updated=?,revision=? WHERE id=?',
                       (int(approved), int(action != 'pause'), due, now, uuid.uuid4().hex, schedule_id))

    def reserve(self, now, schedule_id):
        """Claim before dispatch. Missing tasks after a crash become interrupted, never retried."""
        with self.connect() as db:
            row = db.execute('SELECT * FROM schedules WHERE id=? AND approved=1 AND enabled=1 AND next_due<=?',
                             (schedule_id, now)).fetchone()
            if row is None:
                return None
            if db.execute('SELECT 1 FROM runs WHERE schedule_id=? AND status IN (?,?,?,?,?)', (schedule_id, *ACTIVE)).fetchone():
                return None
            day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if db.execute("SELECT count(*) FROM runs WHERE status!='deferred' AND created>=? AND created<?", (day, day + 86400)).fetchone()[0] >= MAX_DAILY_RUNS:
                return None
            if db.execute("SELECT count(*) FROM runs WHERE schedule_id=? AND status!='deferred' AND created>=? AND created<?", (schedule_id, day, day + 86400)).fetchone()[0] >= MAX_SCHEDULE_DAILY_RUNS:
                return None
            run_id = uuid.uuid4().hex
            task_id = datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
            db.execute('INSERT INTO runs(id,schedule_id,task_id,due,created,status) VALUES(?,?,?,?,?,?)',
                       (run_id, schedule_id, task_id, row['next_due'], now, 'reserved'))
            try:
                following = next_due(json.loads(row['spec']), now, self.timezone)
            except ValueError:
                following = None  # No next occurrence within the bounded cron horizon.
            db.execute('UPDATE schedules SET next_due=?,enabled=? WHERE id=?', (following, int(following is not None), schedule_id))
            return {'id': run_id, 'task_id': task_id, 'schedule': self.schedule(row)}

    def defer(self, run):
        """Undo a busy reservation without overwriting a concurrent owner edit."""
        with self.connect() as db:
            schedule = run['schedule']
            db.execute('UPDATE schedules SET enabled=1,next_due=? WHERE id=? AND revision=?',
                       (schedule['next_due'], schedule['id'], schedule['revision']))
            db.execute("UPDATE runs SET status='deferred',error='Worker became busy; occurrence remains due.' WHERE id=?", (run['id'],))

    def started(self, run_id, placed):
        """Record the chat and exchange the worker started a reserved run in."""
        with self.connect() as db:
            db.execute("UPDATE runs SET task_id=?,turn_id=?,status='queued' WHERE id=?",
                       (placed.task_id, placed.turn_id, run_id))

    def follow_chat(self, schedule_id, old_chat, new_chat):
        """Move a schedule to the chat that replaced its own, unless the owner changed the chat meanwhile."""
        with self.connect() as db:
            db.execute('UPDATE schedules SET chat_id=? WHERE id=? AND chat_id=?', (new_chat, schedule_id, old_chat))

    def finish(self, run_id, status, error=''):
        with self.connect() as db:
            db.execute('UPDATE runs SET status=?,error=? WHERE id=?', (status, error[:500], run_id))
            db.execute('''DELETE FROM runs WHERE id IN (
                SELECT id FROM runs WHERE status NOT IN ('reserved','queued','running','waiting_for_approval','waiting_for_input')
                ORDER BY created DESC, id DESC LIMIT -1 OFFSET ?)''', (MAX_RUNS,))

    def runs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY created DESC,id DESC LIMIT 600')]

class Engine:
    """One scheduler tick dispatches at most one ordinary task, never native decisions."""
    def __init__(self, store, worker):
        self.store = store; self.worker = worker
        self.tick_lock = threading.Lock()

    def reconcile(self):
        for run in self.store.runs():
            if run['status'] in ACTIVE:
                task = self.worker.snapshot(run['task_id'])
                if task is None:
                    self.store.finish(run['id'], 'interrupted', 'Worker restarted before this task was saved; not retried.')
                else:
                    self.store.finish(run['id'], run_status(task, run.get('turn_id', '')))

    def submit(self, run, prompt, mode, name):
        """Start a reserved run; return the chat it runs in.

        A schedule made in a chat runs in that chat. When the worker had to use a
        new chat instead (the old one was deleted, archived or cannot go on), the
        schedule follows the new chat, so its later runs continue there.
        """
        schedule = run['schedule']
        # A schedule with no model or chat of its own starts exactly as before.
        own = {key: schedule[key] for key in ('model', 'chat_id') if schedule.get(key)}
        try:
            placed = self.worker.submit(run['task_id'], prompt, mode, name, schedule['id'], **own)
        except Busy:
            self.store.defer(run)
            raise
        except ValueError as error:
            # The worker refused the request and its text says why, for example that the schedule's model is no
            # longer the pinned one. The owner needs that reason in the run line to fix the schedule.
            self.store.finish(run['id'], 'failed', f'{str(error)[:500]} Not retried.')
            raise
        except Exception:
            self.store.finish(run['id'], 'failed', 'Could not start the ordinary task. Not retried.')
            raise
        self.store.started(run['id'], placed)
        if own.get('chat_id') and placed.task_id != own['chat_id']:
            self.store.follow_chat(schedule['id'], own['chat_id'], placed.task_id)
        return placed.task_id

    def tick(self, now=None):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            now = time.time() if now is None else now
            self.reconcile()
            with getattr(self.worker, 'dispatch_slot', contextlib.nullcontext)():
                if self.store.timezone is not None:
                    self.dispatch_due(now)
        finally:
            self.tick_lock.release()

    def dispatch_due(self, now):
        if not SCHEDULED_RUNS or self.worker.busy():
            return
        for schedule in sorted(self.store.schedules(), key=lambda s: s['next_due'] or float('inf')):
            if schedule['enabled'] and schedule['approved'] and schedule['next_due'] is not None and schedule['next_due'] <= now:
                run = self.store.reserve(now, schedule['id'])
                if run:
                    claimed = run['schedule']
                    try:
                        self.submit(run, claimed['prompt'], claimed['mode'], claimed['name'])
                    except Busy:
                        pass
                    return
