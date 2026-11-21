"""Owner-only schedule management, and the schedule tools Codex reaches over a local socket."""
from __future__ import annotations

import copy
import json
import os
import socketserver
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import partial, update_wrapper, wraps
from pathlib import Path
from typing import Any, Protocol

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue
from werkzeug.wrappers import Response

import scheduling


class PolicyCheck(Protocol):
    """The part of policy.py that a scheduled run uses."""

    def policy_problem(self, options: dict[str, Any]) -> str: ...


class ExchangeLookup(Protocol):
    """The part of ha_data.HomeAssistantData that the scheduling socket uses."""

    def exchange_for(self, thread_id: str) -> tuple[str | None, str | None]: ...


class ScheduleWorker(Protocol):
    """What this module uses of the worker: its Flask app, tasks, sign-in checks and task start.

    server.py passes an object that returns its own module globals by name, and the tests pass the server module.
    Each name that the code below reads from the worker (`self.worker`, or the `worker` argument of a route) is
    listed here, with the type that server.py gives it, so that a reader and a type checker see the whole
    dependency. tests/test_scheduling.py fails when the code reads a name that is not in this list, or when server.py
    does not have a listed name.
    """

    DATA_ROOT: Path
    DEFAULT_CHAT_SETTINGS: dict[str, Any]
    active_task_runners: set[str]
    app: Flask
    ha_data: ExchangeLookup
    # threading.RLock; the worker's one lock for `tasks`, `active_task_runners` and `message_queue`.
    lock: AbstractContextManager[Any]
    message_queue: list[dict[str, Any]]
    protected_policy: PolicyCheck
    tasks: dict[str, dict[str, Any]]

    def _active_task_ids_locked(self) -> set[str]: ...
    def ingress_admin(self, action: str = "answer permission requests") -> tuple[dict[str, str] | None, str]: ...
    def new_turn(self, message: str) -> dict[str, Any]: ...
    def open_task(self, task_id: str, title: str, prompt: str, settings: dict[str, Any],
                  turn: dict[str, Any]) -> None: ...
    def parse_chat_settings(self, payload: dict[str, Any],
                            task: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def read_options(self) -> dict[str, Any]: ...
    def record_background_start_failure(self, task_id: str, exc: Exception) -> None: ...
    def redact(self, text: str) -> str: ...
    # Wraps a route so that it runs only for an Ingress request or one with the worker API token (no hints in
    # server.py).
    def require_auth(self, func: Callable[..., ResponseReturnValue]) -> Callable[..., ResponseReturnValue]: ...
    def resolve_chat_settings(self, settings: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]: ...
    # The Home Assistant IANA time zone name, or None while Home Assistant gives none (no hints in server.py).
    def schedule_timezone(self) -> str | None: ...
    def start_background_task(self, task_id: str, prompt: str, session_id: str | None = None,
                              reply: str | None = None) -> threading.Thread: ...
    def update_task(self, task_id: str, **updates: Any) -> bool: ...


class Worker:
    """The scheduling.TaskRunner that starts a scheduled run as an ordinary task of the worker."""

    def __init__(self, worker: ScheduleWorker) -> None:
        self.worker = worker

    def dispatch_slot(self) -> AbstractContextManager[Any]:
        """Return the worker lock, which the Engine holds while it picks and starts a due run."""
        return self.worker.lock

    def busy(self) -> bool:
        """Say whether a task runs or a message waits."""
        with self.worker.lock:
            return bool(self.worker._active_task_ids_locked()) or bool(self.worker.message_queue)

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        """Return a copy of the task's record, or None when there is no such task."""
        with self.worker.lock:
            return copy.deepcopy(self.worker.tasks.get(task_id))

    def submit(self, task_id: str, prompt: str, mode: str, name: str, schedule_id: str,
               model: str | None = None) -> None:
        """Use the normal task path; metadata is durable before the runner starts.

        model is the schedule's own model, or None to follow the add-on's.
        """
        options = self.worker.read_options()
        problem = self.worker.protected_policy.policy_problem(options)
        if problem:
            raise ValueError('The configured sandbox policy refuses this task.')
        with self.worker.lock:
            if self.busy():
                raise scheduling.Busy('The assistant is busy.')
            settings = dict(self.worker.DEFAULT_CHAT_SETTINGS)
            if model:
                settings['model'] = model
            turn = self.worker.new_turn(prompt)
            turn['task_source'] = 'schedule'
            turn['execution_settings'] = self.worker.resolve_chat_settings(settings, options)
            self.worker.active_task_runners.add(task_id)
            try:
                self.worker.open_task(task_id, name, prompt, settings, turn)
                self.worker.update_task(task_id, background_mode=mode, schedule_id=schedule_id, task_source='schedule')
                self.worker.start_background_task(task_id, prompt)
            except Exception as exc:
                self.worker.record_background_start_failure(task_id, exc)
                raise


PAGE_LIMIT = 20
DEFAULT_PAGE_LIMIT = 10


def page_arguments(operation: str, args: dict[str, Any]) -> tuple[int, int]:
    """Return (offset, limit) for a list_schedules call.

    A limit outside 1-20 is clamped rather than refused, so a model that asks
    for 50 gets the first 20 and a next_offset to continue from. A value that
    is not a whole number is refused with its reason, since guessing what the
    caller meant would be worse.
    """
    if set(args) - {'offset', 'limit'}:
        raise ValueError(f'{operation} accepts only offset and limit.')
    maximum = scheduling.MAX_SCHEDULES + scheduling.MAX_PROPOSALS
    offset = args.get('offset', 0)
    limit = args.get('limit', DEFAULT_PAGE_LIMIT)
    # bool is a subclass of int; true and false are not page numbers.
    if type(offset) is not int or not 0 <= offset <= maximum:
        raise ValueError(f'offset must be a whole number from 0 to {maximum}.')
    if type(limit) is not int:
        raise ValueError(f'limit must be a whole number; up to {PAGE_LIMIT} items are returned per page.')
    return offset, min(max(limit, 1), PAGE_LIMIT)


class Service:
    """The schedules database and engine, the scheduler thread, and the socket that Codex's tools call."""

    def __init__(self, worker: ScheduleWorker) -> None:
        self.worker = worker
        self._engine: scheduling.Engine | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def engine(self) -> scheduling.Engine:
        """Return the engine for the current data folder, and keep its time zone equal to Home Assistant's."""
        path = Path(self.worker.DATA_ROOT) / 'schedules.sqlite3'
        zone = self.worker.schedule_timezone()
        with self._lock:
            if path != self._path or self._engine is None:  # The second test is for the type checker only.
                self._engine = scheduling.Engine(scheduling.Store(path, zone=zone), Worker(self.worker))
                self._path = path
            elif self._engine.store.timezone != zone:
                if zone is not None:
                    self._engine.store.set_timezone(zone, time.time())
                else:
                    self._engine.store.timezone = None
            return self._engine

    def log_held_schedules(self) -> None:
        """Say once, at start, that scheduled runs are off and how many saved schedules wait."""
        if scheduling.SCHEDULED_RUNS:
            return
        try:
            held = len(self.engine().store.schedules())
        except Exception:
            print('Scheduled runs are off; the saved schedules could not be counted.', flush=True)
            return
        print(f'Scheduled runs are off in this version; {held} saved schedule(s) kept and not run.', flush=True)

    def dispatch(self, payload: Any) -> dict[str, Any]:
        """Propose a schedule or read the list, for the chat whose running exchange the thread id names."""
        if not isinstance(payload, dict) or set(payload) != {'operation', 'arguments', 'thread_id'}:
            raise ValueError('A scheduling request needs exactly operation, arguments and thread_id.')
        # Refresh cached HA metadata before taking the worker lock. Validate the
        # turn afterward, since it may expire while a slow HA request completes.
        store = self.engine().store
        with self.worker.lock:
            task_id, _exchange = self.worker.ha_data.exchange_for(payload['thread_id'])
            if task_id is None:
                raise ValueError('This exchange has ended.')
            operation, args = payload['operation'], payload['arguments']
            if not isinstance(args, dict):
                raise ValueError('Arguments must be an object.')
            if operation == 'propose_schedule':
                item = store.save(args, owner=False, author='codex', now=time.time())
                return {'ok': True, 'schedule_id': item['id'], 'approved': False, 'enabled': False,
                        'message': 'Waiting for owner approval on the Codex page.'}
            if operation == 'list_schedules':
                offset, limit = page_arguments(operation, args)
                rows = store.schedules()
                page = rows[offset:offset + limit]
                for row in page:
                    for key in scheduling.OWNER_FIELDS:
                        row.pop(key, None)  # The owner's chat and model are not Codex's to read.
                    row['prompt'] = self.worker.redact(row['prompt'])[:512]
                    row['name'] = self.worker.redact(row['name'])
                return {'ok': True, 'items': page, 'next_offset': offset + limit if offset + limit < len(rows) else None,
                        'text_limit': 512}
            raise ValueError('Unsupported scheduling operation; approvals are owner-only.')

    def answer(self, raw: bytes | str) -> dict[str, Any]:
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

    def serve(self, path: str | os.PathLike[str]) -> socketserver.ThreadingUnixStreamServer:
        """Answer the scheduling tools on a Unix socket that only this user can open, in a background thread."""
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
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

    def start(self) -> None:
        """Start the scheduler thread, unless it already runs."""
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True, name='codex-scheduler')
        self.thread.start()

    def loop(self) -> None:
        """Run a scheduler pass every 5 seconds until `stop` is set."""
        while not self.stop.is_set():
            try:
                self.engine().tick()
            except Exception:
                # Task/prompt/credential text must not enter runtime logs here.
                print('Scheduled-run tick failed; check the saved run log.', flush=True)
            self.stop.wait(5)


# The owner's routes. Each handler is a module-level function that takes the worker and the service first;
# register() binds them, wraps each in `owner` and then `guarded`, and adds it to the app under the handler's name.

def no_cached_scheduled_work(response: Response) -> Response:
    """Tell the browser and any proxy not to keep a copy of a schedules answer."""
    if request.path.startswith('/schedules'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def clean(worker: ScheduleWorker, value: Any) -> Any:
    """Return a copy of the value with the worker's redaction applied to every string in it."""
    if isinstance(value, str):
        return worker.redact(value)
    if isinstance(value, list):
        return [clean(worker, v) for v in value]
    if isinstance(value, dict):
        return {k: clean(worker, v) for k, v in value.items()}
    return value


def body() -> dict[str, Any]:
    """Return the request's JSON object, or refuse a body that is missing, larger than 64 KiB or not an object."""
    if request.content_length is None or request.content_length > 65536:
        raise ValueError('Use a JSON request no larger than 64 KiB.')
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object.')
    return value


def guarded(func: Callable[..., ResponseReturnValue]) -> Callable[..., ResponseReturnValue]:
    """Turn the errors of a route into JSON answers: busy 409, not found 404, refused 400, anything else 503."""
    @wraps(func)
    def run(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        try:
            return func(*args, **kwargs)
        except scheduling.Busy as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except LookupError:
            return jsonify(ok=False, error='Record not found.'), 404
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception:
            return jsonify(ok=False, error='Could not save or read scheduled work. No action was approved.'), 503
    return run


def owner(worker: ScheduleWorker, func: Callable[..., ResponseReturnValue]) -> Callable[..., ResponseReturnValue]:
    """Run the route only with the worker API token or Ingress, and then only for a Home Assistant administrator."""
    @worker.require_auth
    @wraps(func)
    def run(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        admin, refusal = worker.ingress_admin()
        if admin is None:
            return jsonify(ok=False, error=refusal), 403
        return func(*args, **kwargs)
    return run


def schedule_payload(worker: ScheduleWorker, row: dict[str, Any]) -> dict[str, Any]:
    """Return a schedule for the page, redacted, with `prompt_redacted` true when the redaction changed its prompt."""
    result = clean(worker, row)
    result['prompt_redacted'] = result['prompt'] != row['prompt']
    return result


def owner_schedule(worker: ScheduleWorker, service: Service, data: dict[str, Any],
                   schedule_id: str | None = None) -> dict[str, Any]:
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


def state(worker: ScheduleWorker, service: Service) -> ResponseReturnValue:
    """Return the page's view: the switch, schedules, runs, time zone and limits."""
    engine = service.engine()
    # A scheduler tick owns this lock while reserving and saving a task. A page
    # refresh must not mark that tiny in-flight gap interrupted.
    with engine.tick_lock:
        engine.reconcile()
        shown = scheduling.SCHEDULED_RUNS
        return jsonify(ok=True, scheduled_runs=shown,
                       schedules=[schedule_payload(worker, row) for row in engine.store.schedules()] if shown else [],
                       runs=clean(worker, engine.store.runs()) if shown else [],
                       timezone=engine.store.timezone,
                       timezone_error='' if engine.store.timezone else 'Home Assistant time zone unavailable; schedule entry and dispatch are paused.',
                       limits={'proposals': scheduling.MAX_PROPOSALS, 'schedules': scheduling.MAX_SCHEDULES, 'daily_runs': scheduling.MAX_DAILY_RUNS,
                               'schedule_daily_runs': scheduling.MAX_SCHEDULE_DAILY_RUNS})


def require_scheduled_runs() -> None:
    """Answer 404 for a change while scheduled runs are switched off."""
    if not scheduling.SCHEDULED_RUNS:
        raise LookupError()


def schedules_list(worker: ScheduleWorker, service: Service) -> ResponseReturnValue:
    """GET /schedules: the page's view."""
    return state(worker, service)


def schedules_create(worker: ScheduleWorker, service: Service) -> ResponseReturnValue:
    """POST /schedules: save a new owner's schedule, approved and enabled."""
    require_scheduled_runs()
    value = service.engine().store.save(owner_schedule(worker, service, body()), owner=True, now=time.time())
    return jsonify(ok=True, schedule=schedule_payload(worker, value)), 201


def schedules_edit(worker: ScheduleWorker, service: Service, schedule_id: str) -> ResponseReturnValue:
    """PATCH /schedules/<schedule_id>: replace a saved schedule with the owner's edit."""
    require_scheduled_runs()
    value = service.engine().store.save(owner_schedule(worker, service, body(), schedule_id), owner=True, now=time.time(), schedule_id=schedule_id)
    return jsonify(ok=True, schedule=schedule_payload(worker, value))


def schedules_action(worker: ScheduleWorker, service: Service, schedule_id: str, action: str) -> ResponseReturnValue:
    """POST /schedules/<schedule_id>/<action>: approve, pause, resume or delete; approval refuses a redacted prompt."""
    require_scheduled_runs()
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


# (method, URL rule, handler); the handler's name is the Flask endpoint name.
ROUTES: tuple[tuple[str, str, Callable[..., ResponseReturnValue]], ...] = (
    ('GET', '/schedules', schedules_list),
    ('POST', '/schedules', schedules_create),
    ('PATCH', '/schedules/<schedule_id>', schedules_edit),
    ('POST', '/schedules/<schedule_id>/<action>', schedules_action),
)


def register(worker: ScheduleWorker) -> Service:
    """Add the owner's schedule routes to the worker's app and return the scheduling service."""
    service = Service(worker)
    app = worker.app
    app.after_request(no_cached_scheduled_work)
    for method, rule, handler in ROUTES:
        bound = update_wrapper(partial(handler, worker, service), handler)
        app.add_url_rule(rule, endpoint=handler.__name__, view_func=guarded(owner(worker, bound)), methods=[method])
    return service
