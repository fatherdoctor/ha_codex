"""Durable schedules and a bounded local-agent mailbox; no process or network calls."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MAX_SCHEDULES = 100
MAX_PROPOSALS = 20
MAX_MAIL_PER_TURN = 20
MAX_MAIL_PER_DAY = 64
MAX_DAILY_RUNS = 64
MAX_SCHEDULE_DAILY_RUNS = 24
MAX_MESSAGES = 500
MAX_RUNS = 500
class Busy(ValueError):
    pass


class RateLimit(ValueError):
    pass


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


def validate(payload, now, zone="UTC"):
    if not isinstance(payload, dict) or set(payload) - {'name', 'prompt', 'mode', 'spec'}:
        raise ValueError('A schedule accepts only name, prompt, mode and spec.')
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
    return {'name': name, 'prompt': prompt, 'mode': mode, 'spec': spec, 'next_due': next_due(spec, now, zone)}


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
                    enabled INTEGER NOT NULL, next_due REAL, updated REAL NOT NULL, revision TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, schedule_id TEXT, task_id TEXT UNIQUE NOT NULL,
                    due REAL NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS mailbox (
                    id TEXT PRIMARY KEY, parent TEXT, author TEXT NOT NULL,
                    body TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS mailbox_reads (
                    message_id TEXT NOT NULL, reader TEXT NOT NULL, PRIMARY KEY(message_id,reader));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mail_usage (author TEXT NOT NULL, day INTEGER NOT NULL,
                    turn_id TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(author,day,turn_id));
            '''
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(schedules)')}
            if 'author' not in columns:
                db.execute("ALTER TABLE schedules ADD COLUMN author TEXT NOT NULL DEFAULT 'owner'")
            db.execute('PRAGMA user_version=1')
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

    def save(self, payload, *, owner, now, schedule_id=None, author=None):
        value = validate(payload, now, self.timezone)
        author = 'owner' if owner else (author or 'lead')
        if author not in ('owner', 'lead', 'codex'):
            raise ValueError('Invalid schedule author.')
        with self.connect() as db:
            old = db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone() if schedule_id else None
            if schedule_id and (old is None or (not owner and (old['approved'] or old['author'] != author))):
                raise LookupError('Schedule not found.')
            approved = int(owner)
            cap = MAX_SCHEDULES if owner else MAX_PROPOSALS
            count = db.execute('SELECT count(*) FROM schedules WHERE approved=? AND id!=?', (approved, schedule_id or '')).fetchone()[0]
            if count >= cap:
                raise ValueError('Approved schedule limit reached.' if owner else 'Proposal limit reached.')
            schedule_id = schedule_id or uuid.uuid4().hex
            db.execute('INSERT OR REPLACE INTO schedules(id,name,prompt,mode,spec,approved,enabled,next_due,updated,revision,author) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
                schedule_id, value['name'], value['prompt'], value['mode'], json.dumps(value['spec']),
                approved, approved, value['next_due'], now, uuid.uuid4().hex, author))
            return self.schedule(db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone())

    def delete_proposal(self, schedule_id, author):
        with self.connect() as db:
            result = db.execute('DELETE FROM schedules WHERE id=? AND approved=0 AND author=?', (schedule_id, author))
            if not result.rowcount:
                raise LookupError('Proposal not found.')

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
            if action == 'approve' and not row['approved'] and db.execute('SELECT count(*) FROM schedules WHERE approved=1').fetchone()[0] >= MAX_SCHEDULES:
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

    def reserve(self, now, schedule_id=None):
        """Claim before dispatch. Missing tasks after a crash become interrupted, never retried."""
        with self.connect() as db:
            row = None
            if schedule_id:
                row = db.execute('SELECT * FROM schedules WHERE id=? AND approved=1 AND enabled=1 AND next_due<=?',
                                 (schedule_id, now)).fetchone()
                if row is None:
                    return None
                if db.execute('SELECT 1 FROM runs WHERE schedule_id=? AND status IN (?,?,?,?,?)', (schedule_id, *ACTIVE)).fetchone():
                    return None
            day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if db.execute("SELECT count(*) FROM runs WHERE status!='deferred' AND created>=? AND created<?", (day, day + 86400)).fetchone()[0] >= MAX_DAILY_RUNS:
                return None
            if schedule_id and db.execute("SELECT count(*) FROM runs WHERE schedule_id=? AND status!='deferred' AND created>=? AND created<?", (schedule_id, day, day + 86400)).fetchone()[0] >= MAX_SCHEDULE_DAILY_RUNS:
                return None
            run_id = uuid.uuid4().hex
            task_id = datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
            due = row['next_due'] if row else now
            db.execute('INSERT INTO runs(id,schedule_id,task_id,due,created,status) VALUES(?,?,?,?,?,?)',
                       (run_id, schedule_id, task_id, due, now, 'reserved'))
            if row:
                try:
                    following = next_due(json.loads(row['spec']), now, self.timezone)
                except ValueError:
                    following = None  # No next occurrence within the bounded cron horizon.
                db.execute('UPDATE schedules SET next_due=?,enabled=? WHERE id=?', (following, int(following is not None), schedule_id))
            return {'id': run_id, 'task_id': task_id, 'schedule': self.schedule(row) if row else None}

    def defer(self, run):
        """Undo a busy reservation without overwriting a concurrent owner edit."""
        with self.connect() as db:
            schedule = run['schedule']
            if schedule:
                db.execute('UPDATE schedules SET enabled=1,next_due=? WHERE id=? AND revision=?',
                           (schedule['next_due'], schedule['id'], schedule['revision']))
            db.execute("UPDATE runs SET status='deferred',error='Worker became busy; occurrence remains due.' WHERE id=?", (run['id'],))

    def finish(self, run_id, status, error=''):
        with self.connect() as db:
            db.execute('UPDATE runs SET status=?,error=? WHERE id=?', (status, error[:500], run_id))
            db.execute('''DELETE FROM runs WHERE id IN (
                SELECT id FROM runs WHERE status NOT IN ('reserved','queued','running','waiting_for_approval','waiting_for_input')
                ORDER BY created DESC, id DESC LIMIT -1 OFFSET ?)''', (MAX_RUNS,))

    def runs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY created DESC,id DESC LIMIT 600')]

    def rotate_token(self):
        token = 'cxlead_' + secrets.token_urlsafe(48)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('lead_token_hash', hashlib.sha256(token.encode()).hexdigest()))
        return token

    def token_ok(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return False
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', ('lead_token_hash',)).fetchone()
        return bool(row and hmac.compare_digest(row[0], hashlib.sha256(token.encode()).hexdigest()))

    def has_token(self):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM settings WHERE key=?', ('lead_token_hash',)).fetchone() is not None

    def mail(self, body, author, now, parent=None, turn_id=None):
        body = text(body, 'message', 8000)
        if author not in ('owner', 'lead', 'codex'):
            raise ValueError('Invalid message author.')
        with self.connect() as db:
            if parent and not db.execute('SELECT id FROM mailbox WHERE id=? AND parent IS NULL', (parent,)).fetchone():
                raise LookupError('Mailbox thread not found.')
            if author != 'owner':
                turn_id = text(turn_id, 'turn_id', 128)
                day = int(now // 86400)
                daily = db.execute('SELECT coalesce(sum(count),0) FROM mail_usage WHERE author=? AND day=?', (author, day)).fetchone()[0]
                turn = db.execute('SELECT coalesce(sum(count),0) FROM mail_usage WHERE author=? AND turn_id=?', (author, turn_id)).fetchone()[0]
                if daily >= MAX_MAIL_PER_DAY or turn >= MAX_MAIL_PER_TURN:
                    raise RateLimit('Mailbox write limit reached (20 per turn, 64 per actor per UTC day). Reading and owner clearing remain available.')
                db.execute('INSERT INTO mail_usage VALUES(?,?,?,1) ON CONFLICT(author,day,turn_id) DO UPDATE SET count=count+1', (author, day, turn_id))
                db.execute('DELETE FROM mail_usage WHERE day<?', (day - 32,))
            while db.execute('SELECT count(*) FROM mailbox').fetchone()[0] >= MAX_MESSAGES:
                root = db.execute('SELECT id FROM mailbox WHERE parent IS NULL AND id!=? ORDER BY created,id LIMIT 1', (parent or '',)).fetchone()
                if root:
                    db.execute('DELETE FROM mailbox WHERE id=? OR parent=?', (root[0], root[0]))
                else:
                    db.execute('DELETE FROM mailbox WHERE id IN (SELECT id FROM mailbox WHERE parent=? ORDER BY created,id LIMIT 1)', (parent,))
            message_id = uuid.uuid4().hex
            db.execute('DELETE FROM mailbox_reads WHERE message_id NOT IN (SELECT id FROM mailbox)')
            db.execute('INSERT INTO mailbox VALUES(?,?,?,?,?)', (message_id, parent, author, body, now))
            return message_id

    def clear_mail(self, author=None, parent=None):
        with self.connect() as db:
            roots = [row[0] for row in db.execute('SELECT id FROM mailbox WHERE parent IS NULL AND (? IS NULL OR author=?) AND (? IS NULL OR id=?)', (author, author, parent, parent))]
            if parent and not roots:
                raise LookupError('Mailbox thread not found.')
            count = 0
            for root in roots:
                count += db.execute('DELETE FROM mailbox WHERE id=? OR parent=?', (root, root)).rowcount
            db.execute('DELETE FROM mailbox_reads WHERE message_id NOT IN (SELECT id FROM mailbox)')
            return count

    def messages(self, reader=None, unread_only=False, mark_read=False):
        """Return a bounded mailbox snapshot, optionally consuming one reader's unread page."""
        if reader not in (None, 'lead', 'owner') or (unread_only or mark_read) and reader is None:
            raise ValueError('Invalid mailbox reader.')
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                'SELECT m.* FROM mailbox m WHERE (?=0 OR (m.author!=? AND NOT EXISTS '
                '(SELECT 1 FROM mailbox_reads r WHERE r.message_id=m.id AND r.reader=?))) ORDER BY m.created,m.id',
                (int(unread_only), reader, reader))]
            if mark_read:
                db.executemany('INSERT OR IGNORE INTO mailbox_reads VALUES(?,?)', [(row['id'], reader) for row in rows])
            return rows

    def mark_mail_read(self, reader, ids):
        """Acknowledge only displayed IDs; concurrent new arrivals remain unread."""
        if reader not in ('lead', 'owner') or not isinstance(ids, list) or len(ids) > MAX_MESSAGES:
            raise ValueError('Invalid mailbox acknowledgement.')
        if any(not isinstance(value, str) or len(value) != 32 for value in ids):
            raise ValueError('Invalid mailbox message ID.')
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO mailbox_reads SELECT id,? FROM mailbox WHERE id=?',
                           [(reader, value) for value in ids])

    def mail_status(self):
        """Expose counts and notification metadata only, never message bodies or task authority."""
        with self.connect() as db:
            unread = {reader: db.execute(
                'SELECT count(*) FROM mailbox m WHERE m.author!=? AND NOT EXISTS '
                '(SELECT 1 FROM mailbox_reads r WHERE r.message_id=m.id AND r.reader=?)',
                (reader, reader)).fetchone()[0] for reader in ('lead', 'owner')}
            messages = [{'message_id': row['id'], 'thread_id': row['parent'] or row['id'],
                         'author': row['author'], 'time': row['created']}
                        for row in db.execute('SELECT id,parent,author,created FROM mailbox ORDER BY created,id')]
            return {'unread': unread, 'messages': messages}



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
                    pending = any(a.get('status') == 'pending' for a in task.get('approvals') or [])
                    self.store.finish(run['id'], 'waiting_for_approval' if pending else task.get('status', 'interrupted'))

    def submit(self, run, prompt, mode, name):
        try:
            self.worker.submit(run['task_id'], prompt, mode, name, (run['schedule'] or {}).get('id'))
        except Busy:
            self.store.defer(run)
            raise
        except Exception:
            self.store.finish(run['id'], 'failed', 'Could not start the ordinary task. Not retried.')
            raise
        self.store.finish(run['id'], 'queued')
        return run['task_id']

    def tick(self, now=None):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            now = time.time() if now is None else now
            self.reconcile()
            if self.store.timezone is None:
                return
            with getattr(self.worker, 'dispatch_slot', contextlib.nullcontext)():
                self.dispatch_due(now)
        finally:
            self.tick_lock.release()

    def dispatch_due(self, now):
        if self.worker.busy():
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

    def lead_task(self, prompt, mode, now=None):
        prompt = text(prompt, 'prompt', 16000)
        if mode not in ('default', 'read-only'):
            raise ValueError('Mode must be default or read-only.')
        with self.tick_lock, getattr(self.worker, "dispatch_slot", contextlib.nullcontext)():
            if self.worker.busy():
                raise Busy('The assistant is busy or paused. Try again later.')
            run = self.store.reserve(time.time() if now is None else now)
            if run is None:
                raise ValueError('Daily unattended-run limit reached.')
            return self.submit(run, prompt, mode, 'Lead agent task')
