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
MAX_MESSAGES = 128
READERS = ("owner", "lead", "codex")
MAX_RUNS = 500
class Busy(ValueError):
    pass


class MailboxFull(ValueError):
    pass


class RateLimit(ValueError):
    pass


ACTIVE = ('reserved', 'queued', 'running', 'waiting_for_approval', 'waiting_for_input')


def text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label} must be nonempty text, at most {limit} characters.')
    return value.strip()


def mail_time(value):
    """Normalize a UTC timestamp for mailbox envelopes."""
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def mail_due(value):
    """Accept only a timestamp with an explicit time zone."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('due must be an ISO 8601 timestamp with a time zone.')
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            raise ValueError()
        return mail_time(date.timestamp())
    except (ValueError, OverflowError, OSError):
        raise ValueError('due must be an ISO 8601 timestamp with a time zone.') from None


def mail_modified(previous, now):
    """Advance the visible modification time even when the clock is tied."""
    stamp = mail_time(now)
    if previous and stamp <= previous:
        date = datetime.fromisoformat(previous.replace('Z', '+00:00')) + timedelta(milliseconds=1)
        return mail_time(date.timestamp())
    return stamp


def mail_envelope(row):
    """Expose the mailbox fields in their documented order."""
    result = {'to': row['recipient'], 'from': row['author'], 'sent': row['sent'], 'last_modified': row['last_modified']}
    if row['due'] is not None:
        result['due'] = row['due']
    result['message'] = row['body']
    return result


def mail_plain(row, zone=None):
    """Render a plain-text envelope in the selected time zone."""
    zone = zone or 'UTC'
    def local(value):
        """Format one stored UTC timestamp in the selected local zone."""
        date = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(ZoneInfo(zone))
        return date.strftime('%Y-%m-%d %H:%M:%S') + ' (' + zone + ')'
    envelope = mail_envelope(row)
    lines = ['To: '+envelope['to'], 'From: '+envelope['from'], 'Sent: '+local(envelope['sent']),
             'Last modified: '+local(envelope['last_modified'])]
    if envelope.get('due'):
        lines.append('Due: '+local(envelope['due']))
    return '\n'.join(lines)+'\n\n'+envelope['message']


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
            read_columns = {row['name'] for row in db.execute('PRAGMA table_info(mailbox_reads)')}
            if 'read_at' not in read_columns:
                db.execute('ALTER TABLE mailbox_reads ADD COLUMN read_at REAL')
            # Old read marks have no timestamp. Start one upgrade grace period
            # rather than guess when a reader saw those messages.
            db.execute('UPDATE mailbox_reads SET read_at=? WHERE read_at IS NULL', (time.time(),))
            columns = {row['name'] for row in db.execute('PRAGMA table_info(schedules)')}
            if 'author' not in columns:
                db.execute("ALTER TABLE schedules ADD COLUMN author TEXT NOT NULL DEFAULT 'owner'")
            mail_columns = {row['name'] for row in db.execute('PRAGMA table_info(mailbox)')}
            for name, definition in (('recipient', "TEXT NOT NULL DEFAULT 'all'"), ('sent', "TEXT NOT NULL DEFAULT ''"),
                                     ('last_modified', "TEXT NOT NULL DEFAULT ''"), ('due', 'TEXT'),
                                     ('origin', "TEXT NOT NULL DEFAULT ''")):
                if name not in mail_columns:
                    db.execute('ALTER TABLE mailbox ADD COLUMN '+name+' '+definition)
            db.execute('''CREATE TABLE IF NOT EXISTS mailbox_deliveries
                (message_id TEXT NOT NULL,reader TEXT NOT NULL,PRIMARY KEY(message_id,reader))''')
            db.execute('CREATE TABLE IF NOT EXISTS mailbox_threads (id TEXT PRIMARY KEY,participants TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS lead_pending (slot INTEGER PRIMARY KEY CHECK(slot=1),
                task_id TEXT UNIQUE NOT NULL,prompt TEXT NOT NULL,mode TEXT NOT NULL,created REAL NOT NULL,thread_id TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS lead_threads (task_id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,
                started INTEGER NOT NULL DEFAULT 0,finished INTEGER NOT NULL DEFAULT 0)''')
            for row in db.execute("SELECT * FROM mailbox WHERE sent='' ").fetchall():
                stamp = mail_time(row['created'])
                db.execute('UPDATE mailbox SET sent=?,last_modified=? WHERE id=?', (stamp, stamp, row['id']))
                db.executemany('INSERT OR IGNORE INTO mailbox_deliveries VALUES(?,?)', [(row['id'], r) for r in READERS])
                db.execute('INSERT OR IGNORE INTO mailbox_reads VALUES(?,?,?)',
                           (row['id'], row['author'], row['created']))
                db.execute('INSERT OR IGNORE INTO mailbox_threads VALUES(?,?)', (row['parent'] or row['id'], json.dumps(READERS)))
            db.execute('PRAGMA user_version=3')
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

    def reserve(self, now, schedule_id=None, task_id=None):
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
            if task_id:
                db.execute("DELETE FROM runs WHERE task_id=? AND status='deferred'", (task_id,))
            run_id = uuid.uuid4().hex
            task_id = task_id or datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
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
                AND NOT EXISTS (SELECT 1 FROM lead_threads t WHERE t.task_id=runs.task_id AND t.finished=0)
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

    def _prune_mail(self, db, now):
        # Expiration belongs to a delivery, not a thread: an unread copy in
        # another mailbox must survive a reader clearing or aging its copy.
        """Expire only eligible reader copies and release finished, empty thread links."""
        db.execute('''DELETE FROM mailbox_deliveries WHERE reader IN ('lead','codex')
            AND EXISTS (SELECT 1 FROM mailbox_reads r WHERE r.message_id=mailbox_deliveries.message_id
                        AND r.reader=mailbox_deliveries.reader AND r.read_at<?)''', (now - 48 * 3600,))
        for reader in READERS:
            excess = db.execute('SELECT max(0,count(*)-?) FROM mailbox_deliveries WHERE reader=?', (MAX_MESSAGES, reader)).fetchone()[0]
            if excess:
                db.execute('''DELETE FROM mailbox_deliveries WHERE reader=? AND message_id IN (
                    SELECT d.message_id FROM mailbox_deliveries d JOIN mailbox m ON m.id=d.message_id
                    JOIN mailbox_reads r ON r.message_id=d.message_id AND r.reader=d.reader
                    WHERE d.reader=? ORDER BY m.created,m.rowid LIMIT ?)''', (reader, reader, excess))
        db.execute('''DELETE FROM mailbox_reads WHERE NOT EXISTS (SELECT 1 FROM mailbox_deliveries d
            WHERE d.message_id=mailbox_reads.message_id AND d.reader=mailbox_reads.reader)''')
        db.execute('DELETE FROM mailbox WHERE NOT EXISTS (SELECT 1 FROM mailbox_deliveries d WHERE d.message_id=mailbox.id)')
        db.execute('''DELETE FROM mailbox_threads WHERE NOT EXISTS
            (SELECT 1 FROM mailbox m WHERE coalesce(m.parent,m.id)=mailbox_threads.id)
            AND NOT EXISTS (SELECT 1 FROM lead_threads t WHERE t.thread_id=mailbox_threads.id AND t.finished=0)''')
        db.execute('''DELETE FROM lead_threads WHERE finished=1 AND NOT EXISTS
            (SELECT 1 FROM mailbox_threads t WHERE t.id=lead_threads.thread_id)''')

    def _make_mail_room(self, db, recipients):
        """Evict read copies only, or refuse the entire delivery when a recipient is full."""
        for reader in sorted(recipients):
            while db.execute('SELECT count(*) FROM mailbox_deliveries WHERE reader=?', (reader,)).fetchone()[0] >= MAX_MESSAGES:
                oldest = db.execute('''SELECT d.message_id FROM mailbox_deliveries d JOIN mailbox m ON m.id=d.message_id
                    JOIN mailbox_reads r ON r.message_id=d.message_id AND r.reader=d.reader
                    WHERE d.reader=? ORDER BY m.created,m.rowid LIMIT 1''', (reader,)).fetchone()
                if not oldest:
                    raise MailboxFull(f'The {reader} mailbox is full of unread messages. Read or clear it before sending more.')
                db.execute('DELETE FROM mailbox_deliveries WHERE reader=? AND message_id=?', (reader, oldest[0]))
                db.execute('DELETE FROM mailbox_reads WHERE reader=? AND message_id=?', (reader, oldest[0]))

    def _count_mail(self, db, author, now, turn_id):
        """Charge an ordinary agent write without resetting its durable limits on clearing."""
        turn_id = text(turn_id, 'turn_id', 128)
        day = int(now // 86400)
        daily = db.execute('SELECT coalesce(sum(count),0) FROM mail_usage WHERE author=? AND day=?', (author, day)).fetchone()[0]
        turn = db.execute('SELECT coalesce(sum(count),0) FROM mail_usage WHERE author=? AND turn_id=?', (author, turn_id)).fetchone()[0]
        if daily >= MAX_MAIL_PER_DAY or turn >= MAX_MAIL_PER_TURN:
            raise RateLimit('Mailbox write limit reached (20 per turn, 64 per actor per UTC day). '
                            'Reading and owner clearing remain available.')
        db.execute('''INSERT INTO mail_usage VALUES(?,?,?,1)
            ON CONFLICT(author,day,turn_id) DO UPDATE SET count=count+1''', (author, day, turn_id))
        db.execute('DELETE FROM mail_usage WHERE day<?', (day - 32,))

    def mail_replied(self, message_id):
        """Check a later owner/lead reply without reading or acknowledging any mailbox."""
        with self.connect() as db:
            return db.execute('''SELECT 1 FROM mailbox question JOIN mailbox reply
                ON reply.parent=coalesce(question.parent, question.id)
                JOIN mailbox_deliveries delivery ON delivery.message_id=reply.id AND delivery.reader='codex'
                WHERE question.id=? AND reply.author IN ('owner','lead')
                AND reply.rowid>question.rowid LIMIT 1''', (message_id,)).fetchone() is not None

    def _mail(self, db, body, author, now, parent=None, turn_id=None, *, to='all', due=None, origin='', system=False):
        """Validate and save a message and all recipient copies in the caller transaction."""
        body = text(body, 'message', 8000)
        if author not in READERS or to not in (*READERS, 'all'):
            raise ValueError('Mailbox to and from must name owner, lead or codex (to may also be all).')
        due = mail_due(due)
        if origin not in ('', 'schedule', 'lead'):
            raise ValueError('Invalid message source.')
        self._prune_mail(db, now)
        thread = db.execute('SELECT participants FROM mailbox_threads WHERE id=?', (parent,)).fetchone() if parent else None
        if parent and (thread is None or author != 'owner' and author not in json.loads(thread[0])):
            raise LookupError('Mailbox thread not found.')
        recipients = set(READERS if to == 'all' else (to,)) | {author}
        if thread:
            recipients.update(json.loads(thread[0]))
        self._make_mail_room(db, recipients)
        if author != 'owner' and not system:
            self._count_mail(db, author, now, turn_id)
        message_id = uuid.uuid4().hex
        stamp = mail_time(now)
        db.execute('INSERT INTO mailbox(id,parent,author,body,created,recipient,sent,last_modified,due,origin) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (message_id, parent, author, body, now, to, stamp, stamp, due, origin))
        root = parent or message_id
        db.execute('INSERT OR REPLACE INTO mailbox_threads VALUES(?,?)', (root, json.dumps(sorted(recipients))))
        db.executemany('INSERT INTO mailbox_deliveries VALUES(?,?)', [(message_id, r) for r in recipients])
        db.execute('INSERT OR IGNORE INTO mailbox_reads VALUES(?,?,?)', (message_id, author, now))
        if parent:
            previous = db.execute('''SELECT max(last_modified) FROM mailbox
                WHERE (id=? OR parent=?) AND id!=?''', (parent, parent, message_id)).fetchone()[0]
            db.execute('UPDATE mailbox SET last_modified=? WHERE id=?', (mail_modified(previous, now), parent))
        self._prune_mail(db, now)
        return message_id

    def mail(self, body, author, now, parent=None, turn_id=None, *, to='all', due=None, origin=''):
        with self.connect() as db:
            return self._mail(db, body, author, now, parent, turn_id, to=to, due=due, origin=origin)

    def edit_mail(self, message_id, author, message, due, now):
        """Edit only an authorized message without changing its accepted task prompt."""
        message = text(message, 'message', 8000)
        due = mail_due(due)
        with self.connect() as db:
            row = db.execute('SELECT * FROM mailbox WHERE id=?', (message_id,)).fetchone()
            if row is None or author != 'owner' and row['author'] != author:
                raise LookupError('Mailbox message not found.')
            db.execute('UPDATE mailbox SET body=?,due=?,last_modified=? WHERE id=?',
                       (message, due, mail_modified(row['last_modified'], now), message_id))

    def clear_mail(self, author=None, parent=None, *, reader=None):
        if reader not in (None, *READERS):
            raise ValueError('Invalid mailbox reader.')
        with self.connect() as db:
            if reader is not None:
                ids = [r[0] for r in db.execute('''SELECT d.message_id FROM mailbox_deliveries d JOIN mailbox m ON m.id=d.message_id
                    WHERE d.reader=? AND (? IS NULL OR coalesce(m.parent,m.id)=?)''', (reader, parent, parent))]
                if parent and not ids:
                    raise LookupError('Mailbox thread not found.')
                db.executemany('DELETE FROM mailbox_deliveries WHERE reader=? AND message_id=?', [(reader, i) for i in ids])
                db.executemany('DELETE FROM mailbox_reads WHERE reader=? AND message_id=?', [(reader, i) for i in ids])
            else:
                ids = [r[0] for r in db.execute('''SELECT m.id FROM mailbox m WHERE (? IS NULL OR m.author=?)
                    AND (? IS NULL OR coalesce(m.parent,m.id)=?)''', (author, author, parent, parent))]
                if parent and not ids:
                    raise LookupError('Mailbox thread not found.')
                db.executemany('DELETE FROM mailbox_deliveries WHERE message_id=?', [(i,) for i in ids])
            self._prune_mail(db, time.time())
            return len(ids)

    def messages(self, reader=None, unread_only=False, mark_read=False, *, offset=0, limit=None, now=None):
        """Return a bounded mailbox snapshot, optionally consuming one reader's unread page."""
        if reader not in (None, *READERS) or (unread_only or mark_read) and reader is None:
            raise ValueError('Invalid mailbox reader.')
        now = time.time() if now is None else now
        with self.connect() as db:
            self._prune_mail(db, now)
            rows = [dict(row) for row in db.execute('''SELECT m.* FROM mailbox m WHERE
                (? IS NULL OR EXISTS (SELECT 1 FROM mailbox_deliveries d WHERE d.message_id=m.id AND d.reader=?))
                AND (?=0 OR NOT EXISTS (SELECT 1 FROM mailbox_reads r WHERE r.message_id=m.id AND r.reader=?))
                ORDER BY m.created,m.rowid''', (reader, reader, int(unread_only), reader))]
            rows = rows[offset:offset + limit] if limit is not None else rows[offset:]
            if mark_read:
                db.executemany('INSERT OR IGNORE INTO mailbox_reads VALUES(?,?,?)',
                               [(row['id'], reader, now) for row in rows])
            return rows

    def mark_mail_read(self, reader, ids, *, now=None):
        """Acknowledge only displayed IDs; concurrent new arrivals remain unread."""
        if reader not in READERS or not isinstance(ids, list) or len(ids) > MAX_MESSAGES:
            raise ValueError('Invalid mailbox acknowledgement.')
        if any(not isinstance(value, str) or len(value) != 32 for value in ids):
            raise ValueError('Invalid mailbox message ID.')
        now = time.time() if now is None else now
        with self.connect() as db:
            db.executemany('''INSERT OR IGNORE INTO mailbox_reads
                SELECT message_id,reader,? FROM mailbox_deliveries WHERE message_id=? AND reader=?''',
                           [(now, value, reader) for value in ids])

    def mail_status(self, now=None):
        """Expose counts and notification metadata only, never message bodies or task authority."""
        now = time.time() if now is None else now
        with self.connect() as db:
            self._prune_mail(db, now)
            unread, counts, full, due = {}, {}, {}, {}
            for reader in READERS:
                counts[reader] = db.execute('SELECT count(*) FROM mailbox_deliveries WHERE reader=?', (reader,)).fetchone()[0]
                rows = db.execute('''SELECT m.id,m.due FROM mailbox m JOIN mailbox_deliveries d ON d.message_id=m.id
                    WHERE d.reader=? AND NOT EXISTS
                    (SELECT 1 FROM mailbox_reads r WHERE r.message_id=m.id AND r.reader=?)''', (reader, reader)).fetchall()
                unread[reader] = len(rows)
                full[reader] = counts[reader] >= MAX_MESSAGES and unread[reader] == counts[reader]
                due[reader] = [{'message_id': r['id'], 'due': r['due'], 'overdue': r['due'] <= mail_time(now)} for r in rows if r['due']]
            messages = [{'message_id': row['id'], 'thread_id': row['parent'] or row['id'],
                         'author': row['author'], 'time': row['created']}
                        for row in db.execute('SELECT id,parent,author,created FROM mailbox ORDER BY created,rowid')]
            pending = db.execute('SELECT count(*) FROM lead_threads WHERE finished=0').fetchone()[0]
            return {'unread': unread, 'counts': counts, 'full': full, 'due': due,
                    'messages': messages, 'pending_task_replies': pending}

    def pending_lead(self):
        """Return the single durable waiting task, if any."""
        with self.connect() as db:
            row = db.execute('SELECT * FROM lead_pending WHERE slot=1').fetchone()
            return dict(row) if row else None

    def hold_lead(self, prompt, mode, now, mail=None):
        """Reserve one waiting task and its optional note atomically."""
        with self.connect() as db:
            if db.execute('SELECT 1 FROM lead_pending').fetchone():
                raise Busy('One lead task is already waiting. Wait for it to start before submitting another.')
            task_id = datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
            message_id = None
            if mail is not None:
                message_id = self._mail(db, mail['message'], 'lead', now, turn_id=mail['turn_id'],
                                        to=mail.get('to', 'codex'), due=mail.get('due'))
                db.execute('INSERT INTO lead_threads(task_id,thread_id) VALUES(?,?)', (task_id, message_id))
            db.execute('INSERT INTO lead_pending VALUES(1,?,?,?,?,?)', (task_id, prompt, mode, now, message_id))
            return task_id, message_id

    def report_lead(self, task_id, stage, message, now):
        """Save each task-stage reply at most once, retaining it for retry on capacity errors."""
        if stage not in ('started', 'finished'):
            raise ValueError('Invalid task reply stage.')
        with self.connect() as db:
            link = db.execute('SELECT * FROM lead_threads WHERE task_id=?', (task_id,)).fetchone()
            if link is None or link[stage]:
                return
            self._mail(db, message, 'codex', now, link['thread_id'], to='lead', origin='lead', system=True)
            if stage == 'started':
                db.execute('UPDATE lead_threads SET started=1 WHERE task_id=?', (task_id,))
            else:
                db.execute('UPDATE lead_threads SET finished=1 WHERE task_id=?', (task_id,))



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
            self._report_lead(now)
            with getattr(self.worker, 'dispatch_slot', contextlib.nullcontext)():
                self._start_pending_lead(now)
                if self.store.timezone is not None:
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

    def _report_lead(self, now):
        """Publish saved task stages without rerunning completed work."""
        with self.store.connect() as db:
            runs = [dict(row) for row in db.execute('SELECT r.* FROM runs r JOIN lead_threads t ON t.task_id=r.task_id WHERE t.finished=0')]
        for run in runs:
            task = self.worker.snapshot(run['task_id']) or {}
            try:
                if run['status'] not in ('reserved', 'deferred'):
                    self.store.report_lead(run['task_id'], 'started', 'Task '+run['task_id']+
                        (' could not start.' if run['error'] else ' started.'), now)
                if run['status'] not in ACTIVE and run['status'] != 'deferred':
                    content = '\n'.join(str(task.get(key) or '') for key in ('summary', 'question', 'error')).strip() or run['error']
                    content = getattr(self.worker, 'redact', lambda value: value)(content)[:7400]
                    self.store.report_lead(run['task_id'], 'finished',
                        'Task '+run['task_id']+' '+run['status']+'.'+ ('\n'+content if content else ''), now)
            except MailboxFull:
                # Status remains durable; retry the reply after its mailbox has room.
                pass

    def _start_pending_lead(self, now):
        """Dispatch the waiting task once when pause, capacity, and daily limits allow."""
        pending = self.store.pending_lead()
        if pending is None or getattr(self.worker, 'paused', lambda: False)() or self.worker.busy():
            return
        with self.store.connect() as db:
            prior = db.execute("SELECT status FROM runs WHERE task_id=?", (pending['task_id'],)).fetchone()
            if prior is not None and prior['status'] != 'deferred':
                # A crash can leave the handoff slot after reserving the run.
                # Reconcile reports the interruption; never dispatch it again.
                db.execute('DELETE FROM lead_pending WHERE task_id=?', (pending['task_id'],))
                return
        run = self.store.reserve(now, task_id=pending['task_id'])
        if run is None:
            return
        try:
            self.submit(run, pending['prompt'], pending['mode'], 'Lead agent task')
        except Busy:
            return
        except Exception:
            with self.store.connect() as db:
                db.execute('DELETE FROM lead_pending WHERE task_id=?', (pending['task_id'],))
            self._report_lead(now)
            raise
        with self.store.connect() as db:
            db.execute('DELETE FROM lead_pending WHERE task_id=?', (pending['task_id'],))
        self._report_lead(now)

    def lead_task(self, prompt, mode, now=None, *, mail=None):
        prompt = text(prompt, 'prompt', 16000)
        if mode not in ('default', 'read-only'):
            raise ValueError('Mode must be default or read-only.')
        now = time.time() if now is None else now
        with self.tick_lock, getattr(self.worker, 'dispatch_slot', contextlib.nullcontext)():
            if getattr(self.worker, 'paused', lambda: False)():
                raise Busy('The assistant is paused. Resume it before submitting lead work.')
            # A durable single slot is also the handoff record for an immediate
            # launch. Reserve daily usage only at dispatch, never while waiting.
            task_id, message_id = self.store.hold_lead(prompt, mode, now, mail)
            self._start_pending_lead(now)
            return task_id
