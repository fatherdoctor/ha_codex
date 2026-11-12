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
            return w.assistant_paused() or bool(w._active_task_ids_locked()) or bool(w.message_queue)

    def snapshot(self, task_id):
        with self.worker.lock:
            return copy.deepcopy(self.worker.tasks.get(task_id))

    def submit(self, task_id, prompt, mode, name, schedule_id):
        """Use the normal task path; metadata is durable before the runner starts."""
        w = self.worker
        options = w.read_options()
        problem = w.protected_policy.policy_problem(options)
        if problem:
            raise ValueError('The configured sandbox policy refuses this task.')
        with w.lock:
            if self.busy():
                raise scheduling.Busy('The assistant is busy or paused.')
            settings = dict(w.DEFAULT_CHAT_SETTINGS)
            turn = w.new_turn(prompt)
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

    def dispatch(self, payload):
        """Use the existing running-turn capability only for proposals and local text."""
        if not isinstance(payload, dict) or set(payload) != {'operation', 'arguments', 'capability'}:
            raise ValueError('Invalid scheduling request.')
        cap = payload['capability']
        w = self.worker
        with w.lock:
            with w.ha_data.lock:
                task_id = next((key for key, value in w.ha_data.capabilities.items() if isinstance(cap, str) and cap and hmac.compare_digest(value, cap)), None)
            if not task_id or not w.ha_data.active(task_id, cap):
                raise ValueError('This exchange has ended.')
            operation, args = payload['operation'], payload['arguments']
            if not isinstance(args, dict):
                raise ValueError('Arguments must be an object.')
            store = self.engine().store
            if operation == 'propose_schedule':
                item = store.save(args, owner=False, author='codex', now=time.time())
                return {'ok': True, 'schedule_id': item['id'], 'approved': False, 'enabled': False,
                        'message': 'Waiting for owner approval on the Codex page.'}
            if operation in ('list_schedules', 'read_mailbox'):
                maximum = scheduling.MAX_SCHEDULES + scheduling.MAX_PROPOSALS if operation == 'list_schedules' else scheduling.MAX_MESSAGES
                offset = args.get('offset', 0); limit = args.get('limit', 10)
                if set(args) - {'offset', 'limit'} or type(offset) is not int or not 0 <= offset <= maximum or type(limit) is not int or not 1 <= limit <= 20:
                    raise ValueError('Invalid page offset.')
                rows = store.schedules() if operation == 'list_schedules' else store.messages()
                page = rows[offset:offset + limit]
                for row in page:
                    for key in ('prompt', 'body'):
                        if key in row:
                            row[key] = w.redact(row[key])[:512]
                    if 'name' in row:
                        row['name'] = w.redact(row['name'])
                return {'ok': True, 'items': page, 'next_offset': offset + limit if offset + limit < len(rows) else None,
                        'text_limit': 512}
            if operation == 'post_mailbox' and not set(args) - {'body', 'parent'}:
                parent = args.get('parent')
                if parent is not None and (not isinstance(parent, str) or len(parent) != 32):
                    raise ValueError('Invalid mailbox thread.')
                turn_id = str(w.tasks[task_id]['current_turn_id'])
                message_id = store.mail(args.get('body'), 'codex', time.time(), parent, task_id + ':' + turn_id)
                return {'ok': True, 'message_id': message_id}
            raise ValueError('Unsupported scheduling operation; approvals are owner-only.')

    def serve(self, path):
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(10)
                try:
                    raw = self.rfile.readline(65537)
                    if len(raw) > 65536:
                        raise ValueError()
                    result = service.dispatch(json.loads(raw))
                except Exception:
                    result = {'ok': False, 'error': 'Invalid, refused or expired scheduling request.'}
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

    def state():
        engine = service.engine()
        # Tick/lead dispatch owns this lock while reserving and saving a task. A page
        # refresh must not mark that tiny in-flight gap interrupted.
        with engine.tick_lock:
            engine.reconcile()
            return jsonify(ok=True, schedules=[schedule_payload(row) for row in engine.store.schedules()], runs=clean(engine.store.runs()),
                           mailbox=clean(engine.store.messages()), timezone=engine.store.timezone,
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
        value = service.engine().store.save(body(), owner=True, now=time.time())
        return jsonify(ok=True, schedule=schedule_payload(value)), 201

    @app.patch('/schedules/<schedule_id>')
    @guarded
    @owner
    def schedules_edit(schedule_id):
        value = service.engine().store.save(body(), owner=True, now=time.time(), schedule_id=schedule_id)
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
        return jsonify(ok=True, schedules=[schedule_payload(row) for row in service.engine().store.schedules()])

    @app.post('/lead/schedules')
    @guarded
    @lead
    def lead_schedules_propose():
        value = service.engine().store.save(body(), owner=False, now=time.time())
        return jsonify(ok=True, schedule=schedule_payload(value)), 201

    @app.patch('/lead/schedules/<schedule_id>')
    @guarded
    @lead
    def lead_schedules_edit(schedule_id):
        value = service.engine().store.save(body(), owner=False, now=time.time(), schedule_id=schedule_id)
        return jsonify(ok=True, schedule=schedule_payload(value))

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
        task_id = service.engine().lead_task(data.get('prompt'), data.get('mode', 'default'))
        return jsonify(ok=True, task_id=task_id, status='queued'), 202

    @app.get('/lead/tasks/<task_id>')
    @guarded
    @lead
    def lead_task_result(task_id):
        with worker.lock:
            task = worker.tasks.get(task_id)
            if task is None or task.get('task_source') not in {'lead', 'schedule'}:
                # A lead token reads background work, not unrelated human chats.
                raise LookupError()
            result = worker.task_payload(task, private=False)
            if any(a.get('status') == 'pending' for a in task.get('approvals') or []):
                result['run_status'] = 'waiting_for_approval'
        return jsonify(ok=True, task=clean(result))

    @app.get('/lead/mailbox')
    @guarded
    @lead
    def lead_mail_list():
        return jsonify(ok=True, mailbox=clean(service.engine().store.messages()))

    def mail_send(author, parent=None):
        data = body()
        fields = {'body', 'turn_id'} if author == 'lead' else {'body'}
        if set(data) != fields:
            raise ValueError('A lead message needs body and turn_id; an owner message needs body only.')
        message_id = service.engine().store.mail(data['body'], author, time.time(), parent, data.get('turn_id'))
        return jsonify(ok=True, message_id=message_id), 201

    @app.post('/lead/mailbox')
    @guarded
    @lead
    def lead_mail_send():
        return mail_send('lead')

    @app.post('/lead/mailbox/<message_id>/reply')
    @guarded
    @lead
    def lead_mail_reply(message_id):
        return mail_send('lead', message_id)

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

    @app.delete('/schedule-mailbox')
    @guarded
    @owner
    def owner_mail_clear():
        return jsonify(ok=True, removed=service.engine().store.clear_mail())

    @app.delete('/schedule-mailbox/<message_id>')
    @guarded
    @owner
    def owner_mail_delete(message_id):
        return jsonify(ok=True, removed=service.engine().store.clear_mail(parent=message_id))

    @app.delete('/lead/mailbox')
    @guarded
    @lead
    def lead_mail_clear():
        return jsonify(ok=True, removed=service.engine().store.clear_mail(author='lead'))

    @app.delete('/lead/mailbox/<message_id>')
    @guarded
    @lead
    def lead_mail_delete(message_id):
        return jsonify(ok=True, removed=service.engine().store.clear_mail(author='lead', parent=message_id))

    return service
