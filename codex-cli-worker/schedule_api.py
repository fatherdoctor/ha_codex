"""Owner-only schedule management, and the schedule tools Codex reaches over a local socket."""

from __future__ import annotations

import copy
from functools import wraps
import json
import os
from pathlib import Path
import socketserver
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
            raise ValueError("The configured sandbox policy refuses this task.")
        with w.lock:
            if self.busy():
                raise scheduling.Busy("The assistant is busy.")
            settings = dict(w.DEFAULT_CHAT_SETTINGS)
            if model:
                settings["model"] = model
            turn = w.new_turn(prompt)
            turn["task_source"] = "schedule"
            turn["execution_settings"] = w.resolve_chat_settings(settings, options)
            w.active_task_runners.add(task_id)
            try:
                w.open_task(task_id, name, prompt, settings, turn)
                w.update_task(
                    task_id,
                    background_mode=mode,
                    schedule_id=schedule_id,
                    task_source="schedule",
                )
                w.start_background_task(task_id, prompt)
            except Exception as exc:
                w.record_background_start_failure(task_id, exc)
                raise


PAGE_LIMIT = 20
DEFAULT_PAGE_LIMIT = 10


def page_arguments(operation, args):
    """Return (offset, limit) for a list_schedules call.

    A limit outside 1-20 is clamped rather than refused, so a model that asks
    for 50 gets the first 20 and a next_offset to continue from. A value that
    is not a whole number is refused with its reason, since guessing what the
    caller meant would be worse.
    """
    if set(args) - {"offset", "limit"}:
        raise ValueError(f"{operation} accepts only offset and limit.")
    maximum = scheduling.MAX_SCHEDULES + scheduling.MAX_PROPOSALS
    offset = args.get("offset", 0)
    limit = args.get("limit", DEFAULT_PAGE_LIMIT)
    # bool is a subclass of int; true and false are not page numbers.
    if type(offset) is not int or not 0 <= offset <= maximum:
        raise ValueError(f"offset must be a whole number from 0 to {maximum}.")
    if type(limit) is not int:
        raise ValueError(
            f"limit must be a whole number; up to {PAGE_LIMIT} items are returned per page."
        )
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
        path = Path(self.worker.DATA_ROOT) / "schedules.sqlite3"
        zone = self.worker.schedule_timezone()
        with self._lock:
            if path != self._path:
                self._engine = scheduling.Engine(
                    scheduling.Store(path, zone=zone), Worker(self.worker)
                )
                self._path = path
            elif self._engine.store.timezone != zone:
                if zone is not None:
                    self._engine.store.set_timezone(zone, time.time())
                else:
                    self._engine.store.timezone = None
            return self._engine

    def log_held_schedules(self):
        """Say once, at start, that scheduled runs are off and how many saved schedules wait."""
        if scheduling.SCHEDULED_RUNS:
            return
        try:
            held = len(self.engine().store.schedules())
        except Exception:
            print(
                "Scheduled runs are off; the saved schedules could not be counted.",
                flush=True,
            )
            return
        print(
            f"Scheduled runs are off in this version; {held} saved schedule(s) kept and not run.",
            flush=True,
        )

    def dispatch(self, payload):
        """Propose a schedule or read the list, for the chat whose running exchange the thread id names."""
        if not isinstance(payload, dict) or set(payload) != {
            "operation",
            "arguments",
            "thread_id",
        }:
            raise ValueError(
                "A scheduling request needs exactly operation, arguments and thread_id."
            )
        w = self.worker
        # Refresh cached HA metadata before taking the worker lock. Validate the
        # turn afterward, since it may expire while a slow HA request completes.
        store = self.engine().store
        with w.lock:
            task_id, _exchange = w.ha_data.exchange_for(payload["thread_id"])
            if task_id is None:
                raise ValueError("This exchange has ended.")
            operation, args = payload["operation"], payload["arguments"]
            if not isinstance(args, dict):
                raise ValueError("Arguments must be an object.")
            if operation == "propose_schedule":
                item = store.save(args, owner=False, author="codex", now=time.time())
                return {
                    "ok": True,
                    "schedule_id": item["id"],
                    "approved": False,
                    "enabled": False,
                    "message": "Waiting for owner approval on the Codex page.",
                }
            if operation == "list_schedules":
                offset, limit = page_arguments(operation, args)
                rows = store.schedules()
                page = rows[offset : offset + limit]
                for row in page:
                    for key in scheduling.OWNER_FIELDS:
                        row.pop(
                            key, None
                        )  # The owner's chat and model are not Codex's to read.
                    row["prompt"] = w.redact(row["prompt"])[:512]
                    row["name"] = w.redact(row["name"])
                return {
                    "ok": True,
                    "items": page,
                    "next_offset": offset + limit
                    if offset + limit < len(rows)
                    else None,
                    "text_limit": 512,
                }
            raise ValueError(
                "Unsupported scheduling operation; approvals are owner-only."
            )

    def answer(self, raw):
        """Run one socket request and say in plain words why it was refused.

        The ValueError and LookupError messages raised on this path are fixed
        sentences written for the caller (they never quote request or stored
        text), so they are passed on; anything else stays generic.
        """
        if len(raw) > 65536:
            return {"ok": False, "error": "The request is larger than 64 KiB."}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {"ok": False, "error": "The request is not valid JSON."}
        # The order of these branches matters. KeyError and IndexError are LookupErrors, and
        # UnicodeError is a ValueError; their first argument is a key, an index or a codec
        # name, not a sentence, so they are caught before the branch that passes it on.
        try:
            return self.dispatch(payload)
        except (KeyError, IndexError):
            return {
                "ok": False,
                "error": "Invalid, refused or expired scheduling request.",
            }
        except UnicodeError:
            # A lone surrogate, such as JSON "\ud800", cannot be encoded for storage.
            return {
                "ok": False,
                "error": "The text holds a character that cannot be stored.",
            }
        except (ValueError, LookupError) as exc:
            reason = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
            return {
                "ok": False,
                "error": reason or "Invalid, refused or expired scheduling request.",
            }
        except Exception:
            return {
                "ok": False,
                "error": "Invalid, refused or expired scheduling request.",
            }

    def serve(self, path):
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(10)
                result = service.answer(self.rfile.readline(65537))
                encoded = json.dumps(result).encode() + b"\n"
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
        self.thread = threading.Thread(
            target=self.loop, daemon=True, name="codex-scheduler"
        )
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            try:
                self.engine().tick()
            except Exception:
                # Task/prompt/credential text must not enter runtime logs here.
                print("Scheduled-run tick failed; check the saved run log.", flush=True)
            self.stop.wait(5)


def register(worker):
    service = Service(worker)
    app = worker.app

    @app.after_request
    def no_cached_scheduled_work(response):
        if request.path.startswith("/schedules"):
            response.headers["Cache-Control"] = "no-store"
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
            raise ValueError("Use a JSON request no larger than 64 KiB.")
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def guarded(func):
        @wraps(func)
        def run(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except scheduling.Busy as exc:
                return jsonify(ok=False, error=str(exc)), 409
            except LookupError:
                return jsonify(ok=False, error="Record not found."), 404
            except ValueError as exc:
                return jsonify(ok=False, error=str(exc)), 400
            except Exception:
                return jsonify(
                    ok=False,
                    error="Could not save or read scheduled work. No action was approved.",
                ), 503

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

    def schedule_payload(row):
        result = clean(row)
        result["prompt_redacted"] = result["prompt"] != row["prompt"]
        return result

    def owner_schedule(data, schedule_id=None):
        """Check what only the worker knows about an owner's schedule, then return it.

        A named model must be one the chat page offers. A chat must exist when the
        schedule first names it; a schedule outlives a chat that is deleted later.
        """
        if data.get("model") is not None:
            worker.parse_chat_settings({"chat_settings": {"model": data["model"]}})
        chat_id = data.get("chat_id")
        if chat_id and isinstance(chat_id, str):
            store = service.engine().store
            saved = next(
                (
                    row["chat_id"]
                    for row in store.schedules()
                    if row["id"] == schedule_id
                ),
                "",
            )
            with worker.lock:
                known = chat_id in worker.tasks
            if chat_id != saved and not known:
                raise ValueError(
                    "That chat no longer exists. Open the schedule from another chat or from Settings."
                )
        return data

    def state():
        engine = service.engine()
        # A scheduler tick owns this lock while reserving and saving a task. A page
        # refresh must not mark that tiny in-flight gap interrupted.
        with engine.tick_lock:
            engine.reconcile()
            shown = scheduling.SCHEDULED_RUNS
            return jsonify(
                ok=True,
                scheduled_runs=shown,
                schedules=[schedule_payload(row) for row in engine.store.schedules()]
                if shown
                else [],
                runs=clean(engine.store.runs()) if shown else [],
                timezone=engine.store.timezone,
                timezone_error=""
                if engine.store.timezone
                else "Home Assistant time zone unavailable; schedule entry and dispatch are paused.",
                limits={
                    "proposals": scheduling.MAX_PROPOSALS,
                    "schedules": scheduling.MAX_SCHEDULES,
                    "daily_runs": scheduling.MAX_DAILY_RUNS,
                    "schedule_daily_runs": scheduling.MAX_SCHEDULE_DAILY_RUNS,
                },
            )

    @app.get("/schedules")
    @guarded
    @owner
    def schedules_list():
        return state()

    def require_scheduled_runs():
        if not scheduling.SCHEDULED_RUNS:
            raise LookupError()

    @app.post("/schedules")
    @guarded
    @owner
    def schedules_create():
        require_scheduled_runs()
        value = service.engine().store.save(
            owner_schedule(body()), owner=True, now=time.time()
        )
        return jsonify(ok=True, schedule=schedule_payload(value)), 201

    @app.patch("/schedules/<schedule_id>")
    @guarded
    @owner
    def schedules_edit(schedule_id):
        require_scheduled_runs()
        value = service.engine().store.save(
            owner_schedule(body(), schedule_id),
            owner=True,
            now=time.time(),
            schedule_id=schedule_id,
        )
        return jsonify(ok=True, schedule=schedule_payload(value))

    @app.post("/schedules/<schedule_id>/<action>")
    @guarded
    @owner
    def schedules_action(schedule_id, action):
        require_scheduled_runs()
        data = body()
        if set(data) - {"revision"}:
            raise ValueError("Unexpected action fields.")
        store = service.engine().store
        if action == "approve":
            schedule = next(
                (item for item in store.schedules() if item["id"] == schedule_id), None
            )
            if schedule is None:
                raise LookupError()
            if worker.redact(schedule["prompt"]) != schedule["prompt"]:
                raise ValueError(
                    "This proposal contains redacted text. Edit it to review a complete prompt before approving."
                )
        store.action(schedule_id, action, time.time(), data.get("revision"))
        return jsonify(ok=True)

    return service
