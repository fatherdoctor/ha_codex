"""Owner-only schedule management and a separately authenticated local lead API."""
from __future__ import annotations

import copy
import hmac
import json
import os
import socketserver
from functools import wraps
from pathlib import Path
import threading
import time

from flask import jsonify, request
import scheduling


class Worker:
    def __init__(self, worker):
        self.worker = worker

    def dispatch_slot(self):
        return self.worker.lock

    def busy(self):
        w = self.worker
        with w.lock:
            return bool(w._active_task_ids_locked()) or bool(w.message_queue)

    def redact(self, value):
        """Remove sensitive text before a task result reaches the mailbox."""
        return self.worker.redact(value)

    def snapshot(self, task_id):
        with self.worker.lock:
            return copy.deepcopy(self.worker.tasks.get(task_id))

    def submit(self, task_id, prompt, mode, name, schedule_id, model=None):
        """Use the normal task path; metadata is durable before the runner starts.

        model is the schedule's own model, or None to follow the add-on's.
        """
        w = self.worker
        options = w.read_options()
        problem = w.protected_policy.policy_problem(options)
        if problem:
            raise ValueError('The configured sandbox policy refuses this task.')
        with w.lock:
            if self.busy():
                raise scheduling.Busy('The assistant is busy.')
            settings = dict(w.DEFAULT_CHAT_SETTINGS)
            if model:
                settings['model'] = model
            turn = w.new_turn(prompt)
            turn['task_source'] = 'schedule' if schedule_id else 'lead'
            turn['execution_settings'] = w.resolve_chat_settings(settings, options)
            w.active_task_runners.add(task_id)
            try:
                w.open_task(task_id, name, prompt, settings, turn)
                w.update_task(task_id, background_mode=mode, schedule_id=schedule_id or '',
                              task_source='schedule' if schedule_id else 'lead')
                w.start_background_task(task_id, prompt)
            except Exception as exc:
                w.record_background_start_failure(task_id, exc)
                raise


PAGE_LIMIT = 20
DEFAULT_PAGE_LIMIT = 10


def page_arguments(operation, args):
    """Return (offset, limit) for a list_schedules or read_mailbox call.

    A limit outside 1-20 is clamped rather than refused, so a model that asks
    for 50 gets the first 20 and a next_offset to continue from. A value that
    is not a whole number is refused with its reason, since guessing what the
    caller meant would be worse.
    """
    if set(args) - {'offset', 'limit'}:
        raise ValueError(f'{operation} accepts only offset and limit.')
    if operation == 'list_schedules':
        maximum = scheduling.MAX_SCHEDULES + scheduling.MAX_PROPOSALS
    else:
        maximum = 500  # Legacy unread mail may temporarily exceed the new 128-copy cap.
    offset = args.get('offset', 0)
    limit = args.get('limit', DEFAULT_PAGE_LIMIT)
    # bool is a subclass of int; true and false are not page numbers.
    if type(offset) is not int or not 0 <= offset <= maximum:
        raise ValueError(f'offset must be a whole number from 0 to {maximum}.')
    if type(limit) is not int:
        raise ValueError(f'limit must be a whole number; up to {PAGE_LIMIT} items are returned per page.')
    return offset, min(max(limit, 1), PAGE_LIMIT)


class Service:
    def __init__(self, worker):
        self.worker = worker
        self._engine = None
        self._path = None
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def engine(self):
        path = Path(self.worker.DATA_ROOT) / 'schedules.sqlite3'
        zone = self.worker.schedule_timezone()
        with self._lock:
            if path != self._path:
                self._engine = scheduling.Engine(scheduling.Store(path, zone=zone), Worker(self.worker))
                self._path = path
            elif self._engine.store.timezone != zone:
                if zone is not None:
                    self._engine.store.set_timezone(zone, time.time())
                else:
                    self._engine.store.timezone = None
            return self._engine

    def mailbox_status(self):
        """Read mailbox metadata without refreshing Core time-zone metadata or dispatching work."""
        path = Path(self.worker.DATA_ROOT) / 'schedules.sqlite3'
        with self._lock:
            store = self._engine.store if self._path == path else scheduling.Store(path)
            return store.mail_status()

    def dispatch(self, payload):
        """Use the existing running-turn capability only for proposals and local text."""
        if not isinstance(payload, dict) or set(payload) != {'operation', 'arguments', 'capability'}:
            raise ValueError('A scheduling request needs exactly operation, arguments and capability.')
        cap = payload['capability']
        w = self.worker
        # Refresh cached HA metadata before taking the worker lock. Validate the
        # turn afterward, since it may expire while a slow HA request completes.
        store = self.engine().store
        with w.lock:
            with w.ha_data.lock:
                task_id = next((key for key, value in w.ha_data.capabilities.items() if isinstance(cap, str) and cap and hmac.compare_digest(value, cap)), None)
            if not task_id or not w.ha_data.active(task_id, cap):
                raise ValueError('This exchange has ended.')
            operation, args = payload['operation'], payload['arguments']
            if not isinstance(args, dict):
                raise ValueError('Arguments must be an object.')
            if operation == 'propose_schedule':
                item = store.save(args, owner=False, author='codex', now=time.time())
                return {'ok': True, 'schedule_id': item['id'], 'approved': False, 'enabled': False,
                        'message': 'Waiting for owner approval on the Codex page.'}
            if operation in ('list_schedules', 'read_mailbox'):
                offset, limit = page_arguments(operation, args)
                rows = store.schedules() if operation == 'list_schedules' else store.messages(reader='codex')
                page = rows[offset:offset + limit]
                if operation == 'read_mailbox':
                    store.mark_mail_read('codex', [row['id'] for row in page])
                    return {'ok': True, 'items': [{'id': row['id'], 'parent': row['parent'], 'origin': row['origin'],
                        'envelope': {**scheduling.mail_envelope(row), 'message': w.redact(row['body'])[:512]}} for row in page],
                        'next_offset': offset + limit if offset + limit < len(rows) else None, 'text_limit': 512}
                for row in page:
                    for key in scheduling.OWNER_FIELDS:
                        row.pop(key, None)  # The owner's chat and model are not Codex's to read.
                    for key in ('prompt', 'body'):
                        if key in row:
                            row[key] = w.redact(row[key])[:512]
                    if 'name' in row:
                        row['name'] = w.redact(row['name'])
                return {'ok': True, 'items': page, 'next_offset': offset + limit if offset + limit < len(rows) else None,
                        'text_limit': 512}
            if operation == 'post_mailbox':
                if set(args) - {'body', 'parent', 'to', 'due'}:
                    raise ValueError('post_mailbox accepts only body, parent, to and due.')
                parent = args.get('parent')
                if parent is not None and (not isinstance(parent, str) or len(parent) != 32):
                    raise ValueError('parent must be the 32-character id of a mailbox message.')
                turn_id = str(w.tasks[task_id]['current_turn_id'])
                turn = next((row for row in w.tasks[task_id].get('turns') or []
                             if isinstance(row, dict) and str(row.get('turn_id')) == turn_id), {})
                origin = turn.get('task_source', '')
                if origin not in ('schedule', 'lead'):
                    origin = ''
                message_id = store.mail(args.get('body'), 'codex', time.time(), parent, task_id + ':' + turn_id,
                    to=args.get('to', 'all'), due=args.get('due'), origin=origin)
                return {'ok': True, 'message_id': message_id}
            raise ValueError('Unsupported scheduling operation; approvals are owner-only.')

    def answer(self, raw):
        """Run one socket request and say in plain words why it was refused.

        The ValueError and LookupError messages raised on this path are fixed
        sentences written for the caller (they never quote request or stored
        text), so they are passed on; anything else stays generic.
        """
        if len(raw) > 65536:
            return {'ok': False, 'error': 'The request is larger than 64 KiB.'}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {'ok': False, 'error': 'The request is not valid JSON.'}
        # The order of these branches matters. KeyError and IndexError are LookupErrors, and
        # UnicodeError is a ValueError; their first argument is a key, an index or a codec
        # name, not a sentence, so they are caught before the branch that passes it on.
        try:
            return self.dispatch(payload)
        except (KeyError, IndexError):
            return {'ok': False, 'error': 'Invalid, refused or expired scheduling request.'}
        except UnicodeError:
            # A lone surrogate, such as JSON "\ud800", cannot be encoded for storage.
            return {'ok': False, 'error': 'The text holds a character that cannot be stored.'}
        except (ValueError, LookupError) as exc:
            reason = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ''
            return {'ok': False, 'error': reason or 'Invalid, refused or expired scheduling request.'}
        except Exception:
            return {'ok': False, 'error': 'Invalid, refused or expired scheduling request.'}

    def serve(self, path):
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(10)
                result = service.answer(self.rfile.readline(65537))
                encoded = json.dumps(result).encode() + b'\n'
                if len(encoded) > 65536:
                    encoded = b'{"ok":false,"error":"Response too large; use a smaller page."}\n'
                try:
                    self.wfile.write(encoded)
                except OSError:
                    pass

        Path(path).unlink(missing_ok=True)
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        server.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True, name='codex-scheduler')
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            try:
                self.engine().tick()
            except Exception:
                # Task/prompt/credential text must not enter runtime logs here.
                print('Scheduled-run tick failed; check the saved run log.', flush=True)
            self.stop.wait(5)


def register(worker):
    service = Service(worker)
    app = worker.app

    @app.after_request
    def no_cached_scheduled_work(response):
        if request.path.startswith(('/schedules', '/lead/', '/lead-token', '/schedule-mailbox')):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def clean(value):
        if isinstance(value, str):
            return worker.redact(value)
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value

    def body():
        if request.content_length is None or request.content_length > 65536:
            raise ValueError('Use a JSON request no larger than 64 KiB.')
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object.')
        return value

    def guarded(func):
        @wraps(func)
        def run(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except scheduling.RateLimit as exc:
                return jsonify(ok=False, error=str(exc)), 429
            except scheduling.MailboxFull as exc:
                return jsonify(ok=False, error=str(exc)), 409
            except scheduling.Busy as exc:
                return jsonify(ok=False, error=str(exc)), 409
            except LookupError:
                return jsonify(ok=False, error='Record not found.'), 404
            except ValueError as exc:
                return jsonify(ok=False, error=str(exc)), 400
            except Exception:
                return jsonify(ok=False, error='Could not save or read scheduled work. No action was approved.'), 503
        return run

    def owner(func):
        @worker.require_auth
        @wraps(func)
        def run(*args, **kwargs):
            admin, refusal = worker.ingress_admin()
            if admin is None:
                return jsonify(ok=False, error=refusal), 403
            return func(*args, **kwargs)
        return run

    def lead(func):
        @wraps(func)
        def run(*args, **kwargs):
            auth = request.headers.get('Authorization', '')
            supplied = auth[7:] if auth.startswith('Bearer ') else ''
            if not service.engine().store.token_ok(supplied):
                return jsonify(ok=False, error='Unauthorized lead token.'), 401
            return func(*args, **kwargs)
        return run

    def schedule_payload(row):
        result = clean(row)
        result['prompt_redacted'] = result['prompt'] != row['prompt']
        return result

    def for_agent(row):
        """A schedule as the lead and Codex see it: without the owner's chat and model."""
        return {key: value for key, value in schedule_payload(row).items() if key not in scheduling.OWNER_FIELDS}

    def owner_schedule(data, schedule_id=None):
        """Check what only the worker knows about an owner's schedule, then return it.

        A named model must be one the chat page offers. A chat must exist when the
        schedule first names it; a schedule outlives a chat that is deleted later.
        """
        if data.get('model') is not None:
            worker.parse_chat_settings({'chat_settings': {'model': data['model']}})
        chat_id = data.get('chat_id')
        if chat_id and isinstance(chat_id, str):
            store = service.engine().store
            saved = next((row['chat_id'] for row in store.schedules() if row['id'] == schedule_id), '')
            with worker.lock:
                known = chat_id in worker.tasks
            if chat_id != saved and not known:
                raise ValueError('That chat no longer exists. Open the schedule from another chat or from Settings.')
        return data

    def state():
        engine = service.engine()
        # Tick/lead dispatch owns this lock while reserving and saving a task. A page
        # refresh must not mark that tiny in-flight gap interrupted.
        with engine.tick_lock:
            engine.reconcile()
            reader = mailbox_reader()
            mailbox = engine.store.messages(reader=reader, limit=scheduling.MAX_MESSAGES + 1)
            if reader == 'owner' and len(mailbox) > scheduling.MAX_MESSAGES:
                # Settings acknowledges its visible owner page. Prefer unread
                # legacy copies so that refresh reaches the next page without
                # deleting read history to advance it.
                unread = engine.store.messages(reader=reader, unread_only=True, limit=scheduling.MAX_MESSAGES)
                mailbox = unread or mailbox
            return jsonify(ok=True, schedules=[schedule_payload(row) for row in engine.store.schedules()], runs=clean(engine.store.runs()),
                           mailbox=[mail_payload(row, engine.store.timezone) for row in
                                    mailbox[:scheduling.MAX_MESSAGES]],
                           mailbox_reader=reader, mailbox_status=engine.store.mail_status(), timezone=engine.store.timezone,
                           timezone_error='' if engine.store.timezone else 'Home Assistant time zone unavailable; schedule entry and dispatch are paused.',
                           lead_token_configured=engine.store.has_token(),
                           limits={'proposals': scheduling.MAX_PROPOSALS, 'mail_per_turn': scheduling.MAX_MAIL_PER_TURN,
                                   'mail_per_day': scheduling.MAX_MAIL_PER_DAY, 'schedules': scheduling.MAX_SCHEDULES, 'daily_runs': scheduling.MAX_DAILY_RUNS,
                                   'schedule_daily_runs': scheduling.MAX_SCHEDULE_DAILY_RUNS})

    @app.get('/schedules')
    @guarded
    @owner
    def schedules_list():
        return state()

    @app.post('/schedules')
    @guarded
    @owner
    def schedules_create():
        value = service.engine().store.save(owner_schedule(body()), owner=True, now=time.time())
        return jsonify(ok=True, schedule=schedule_payload(value)), 201

    @app.patch('/schedules/<schedule_id>')
    @guarded
    @owner
    def schedules_edit(schedule_id):
        value = service.engine().store.save(owner_schedule(body(), schedule_id), owner=True, now=time.time(), schedule_id=schedule_id)
        return jsonify(ok=True, schedule=schedule_payload(value))

    @app.post('/schedules/<schedule_id>/<action>')
    @guarded
    @owner
    def schedules_action(schedule_id, action):
        data = body()
        if set(data) - {'revision'}:
            raise ValueError('Unexpected action fields.')
        store = service.engine().store
        if action == 'approve':
            schedule = next((item for item in store.schedules() if item['id'] == schedule_id), None)
            if schedule is None:
                raise LookupError()
            if worker.redact(schedule['prompt']) != schedule['prompt']:
                raise ValueError('This proposal contains redacted text. Edit it to review a complete prompt before approving.')
        store.action(schedule_id, action, time.time(), data.get('revision'))
        return jsonify(ok=True)

    @app.post('/lead-token')
    @guarded
    @owner
    def lead_token():
        if body() != {'confirm': True}:
            raise ValueError('Confirm token creation or rotation.')
        response = jsonify(ok=True, token=service.engine().store.rotate_token())
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/lead/schedules')
    @guarded
    @lead
    def lead_schedules_list():
        return jsonify(ok=True, schedules=[for_agent(row) for row in service.engine().store.schedules()])

    @app.post('/lead/schedules')
    @guarded
    @lead
    def lead_schedules_propose():
        value = service.engine().store.save(body(), owner=False, now=time.time())
        return jsonify(ok=True, schedule=for_agent(value)), 201

    @app.patch('/lead/schedules/<schedule_id>')
    @guarded
    @lead
    def lead_schedules_edit(schedule_id):
        value = service.engine().store.save(body(), owner=False, now=time.time(), schedule_id=schedule_id)
        return jsonify(ok=True, schedule=for_agent(value))

    @app.delete('/lead/schedules/<schedule_id>')
    @guarded
    @lead
    def lead_schedules_delete(schedule_id):
        service.engine().store.delete_proposal(schedule_id, 'lead')
        return jsonify(ok=True)

    @app.post('/lead/tasks')
    @guarded
    @lead
    def lead_task_submit():
        data = body()
        if set(data) - {'prompt', 'mode'}:
            raise ValueError('A task accepts only prompt and mode.')
        engine = service.engine()
        task_id = engine.lead_task(data.get('prompt'), data.get('mode', 'default'))
        pending = engine.store.pending_lead()
        return jsonify(ok=True, task_id=task_id, status='waiting' if pending and pending['task_id'] == task_id else 'queued'), 202

    @app.get('/lead/tasks/<task_id>')
    @guarded
    @lead
    def lead_task_result(task_id):
        pending = service.engine().store.pending_lead()
        if pending and pending['task_id'] == task_id:
            return jsonify(ok=True, task={'task_id': task_id, 'task_source': 'lead', 'status': 'waiting',
                                         'background_mode': pending['mode'],
                                         'summary': 'Waiting for a free worker slot and the daily run limit.'})
        with worker.lock:
            task = worker.tasks.get(task_id)
            if task is None or task.get('task_source') not in {'lead', 'schedule'}:
                # A lead token reads background work, not unrelated human chats.
                raise LookupError()
            result = worker.task_payload(task, private=False)
            if any(a.get('status') == 'pending' for a in task.get('approvals') or []):
                result['run_status'] = 'waiting_for_approval'
        return jsonify(ok=True, task=clean(result))

    def mail_payload(row, zone, *, excerpt=False):
        """Return a redacted envelope and optional plain-text view."""
        envelope = scheduling.mail_envelope(row)
        envelope['message'] = worker.redact(envelope['message'])[:512] if excerpt else worker.redact(envelope['message'])
        result = {'id': row['id'], 'parent': row['parent'], 'envelope': envelope, 'origin': row['origin']}
        if not excerpt:
            result['text'] = worker.redact(scheduling.mail_plain(row, zone))
        return result

    def mailbox_reader():
        """Accept only a named mailbox reader."""
        reader = request.args.get('mailbox', 'owner')
        if reader not in scheduling.READERS:
            raise ValueError('mailbox must name owner, lead or codex.')
        return reader

    def mail_page(reader, *, mark_read):
        """Return a bounded reader page and acknowledge only that page when permitted."""
        allowed = {'offset', 'limit', 'unread_only'}
        if not (reader == 'lead' and request.path.startswith('/lead/')):
            allowed.add('mailbox')
        if set(request.args) - allowed:
            raise ValueError('Mailbox reads accept only offset, limit and unread_only (administrators may select mailbox).')
        unread = request.args.get('unread_only', 'false')
        if unread not in ('true', 'false'):
            raise ValueError('unread_only must be true or false.')
        args = {}
        for key in ('offset', 'limit'):
            if key in request.args:
                try:
                    args[key] = int(request.args[key])
                except (ValueError, TypeError):
                    raise ValueError(key+' must be a whole number.') from None
        offset, limit = page_arguments('read_mailbox', args)
        engine = service.engine()
        # Store.messages acknowledges only the selected page atomically. The
        # separate count is a continuation hint, not a shared transaction.
        rows = engine.store.messages(reader=reader, unread_only=unread == 'true')
        page = engine.store.messages(reader=reader, unread_only=unread == 'true', mark_read=mark_read, offset=offset, limit=limit)
        following = offset + len(page) < len(rows)
        return jsonify(ok=True, mailbox=[mail_payload(row, engine.store.timezone) for row in page],
                       next_offset=(0 if unread == 'true' and mark_read else offset + limit) if following else None,
                       timezone=engine.store.timezone or 'UTC')

    @app.get('/lead/mailbox')
    @guarded
    @lead
    def lead_mail_list():
        return mail_page('lead', mark_read=True)

    def mail_fields(author, parent=None, *, allow_task=False):
        """Validate message fields and permit attached tasks only on the dedicated lead route."""
        data = body()
        allowed = {'body', 'message', 'to', 'due', 'turn_id'} if author == 'lead' else {'body', 'message', 'to', 'due'}
        if allow_task:
            allowed.add('task')
        if set(data) - allowed or ('message' in data) == ('body' in data):
            raise ValueError('Use one message field (body is a legacy alias), to and optional due; lead mail also needs turn_id.')
        if author == 'lead' and 'turn_id' not in data:
            raise ValueError('A lead message needs turn_id.')
        return data, {'message': data.get('message', data.get('body')), 'to': data.get('to', 'all'),
                      'due': data.get('due'), 'turn_id': data.get('turn_id')}

    def mail_send(author, parent=None, *, allow_task=False):
        data, fields = mail_fields(author, parent, allow_task=allow_task)
        engine = service.engine()
        attached = data.get('task')
        if 'task' in data:
            if not isinstance(attached, dict) or set(attached) - {'prompt', 'mode'}:
                raise ValueError('task accepts only prompt and mode.')
            fields['to'] = data.get('to', 'codex')
            if fields['to'] not in ('codex', 'all'):
                raise ValueError('A task-carrying message must be addressed to codex or all.')
            task_id = engine.lead_task(attached.get('prompt'), attached.get('mode', 'default'), mail=fields)
            with engine.store.connect() as db:
                message_id = db.execute('SELECT thread_id FROM lead_threads WHERE task_id=?', (task_id,)).fetchone()[0]
            pending = engine.store.pending_lead()
            return jsonify(ok=True, message_id=message_id, task_id=task_id,
                           status='waiting' if pending and pending['task_id'] == task_id else 'queued'), 202
        message_id = engine.store.mail(fields['message'], author, time.time(), parent, fields['turn_id'],
                                      to=fields['to'], due=fields['due'])
        return jsonify(ok=True, message_id=message_id), 201

    @app.post('/lead/mailbox')
    @guarded
    @lead
    def lead_mail_send():
        return mail_send('lead', allow_task=True)

    @app.post('/lead/mailbox/<message_id>/reply')
    @guarded
    @lead
    def lead_mail_reply(message_id):
        return mail_send('lead', message_id)

    @app.patch('/lead/mailbox/<message_id>')
    @guarded
    @lead
    def lead_mail_edit(message_id):
        """Edit only the lead's own message through its authenticated route."""
        data = body()
        if set(data) - {'message', 'due'} or 'message' not in data:
            raise ValueError('An edit needs message and optional due.')
        service.engine().store.edit_mail(message_id, 'lead', data['message'], data.get('due'), time.time())
        return jsonify(ok=True)

    @app.get('/schedule-mailbox')
    @guarded
    @owner
    def owner_mail_list():
        """Let an administrator inspect a mailbox without acknowledging its reader copies."""
        return mail_page(mailbox_reader(), mark_read=False)

    @app.post('/schedule-mailbox/read')
    @guarded
    @owner
    def owner_mail_read():
        if mailbox_reader() != 'owner':
            raise ValueError("Only the owner's mailbox can be acknowledged here.")
        data = body()
        if set(data) != {'ids'}:
            raise ValueError('A mailbox acknowledgement needs ids only.')
        service.engine().store.mark_mail_read('owner', data['ids'])
        return jsonify(ok=True)

    @app.post('/schedule-mailbox')
    @guarded
    @owner
    def owner_mail_send():
        return mail_send('owner')

    @app.post('/schedule-mailbox/<message_id>/reply')
    @guarded
    @owner
    def owner_mail_reply(message_id):
        return mail_send('owner', message_id)

    @app.patch('/schedule-mailbox/<message_id>')
    @guarded
    @owner
    def owner_mail_edit(message_id):
        """Apply an authenticated administrator edit without executing its body."""
        data = body()
        if set(data) - {'message', 'due'} or 'message' not in data:
            raise ValueError('An edit needs message and optional due.')
        service.engine().store.edit_mail(message_id, 'owner', data['message'], data.get('due'), time.time())
        return jsonify(ok=True)

    @app.delete('/schedule-mailbox')
    @guarded
    @owner
    def owner_mail_clear():
        if body() != {'confirm': True}:
            raise ValueError('Confirm clearing this mailbox.')
        return jsonify(ok=True, removed=service.engine().store.clear_mail(reader=mailbox_reader()))

    @app.delete('/schedule-mailbox/<message_id>')
    @guarded
    @owner
    def owner_mail_delete(message_id):
        return jsonify(ok=True, removed=service.engine().store.clear_mail(reader=mailbox_reader(), parent=message_id))

    @app.delete('/lead/mailbox')
    @guarded
    @lead
    def lead_mail_clear():
        if request.args or body() != {'confirm': True}:
            raise ValueError('Confirm clearing the lead mailbox only.')
        return jsonify(ok=True, removed=service.engine().store.clear_mail(reader='lead'))

    @app.delete('/lead/mailbox/<message_id>')
    @guarded
    @lead
    def lead_mail_delete(message_id):
        if request.args:
            raise ValueError('The lead may clear only its own mailbox.')
        return jsonify(ok=True, removed=service.engine().store.clear_mail(reader='lead', parent=message_id))

    return service
