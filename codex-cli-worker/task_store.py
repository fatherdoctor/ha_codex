"""The chats the worker keeps: the records in memory, the lock that guards them, and their files on disk.

Each chat (task) has a folder under the task root with its task.json; the task index holds every chat, without its
prompt, reply history and turns, in one file. A TaskStore owns the dictionary of chats, the lock, and the set of chats
that have a runner thread, and it is the one place that writes those files.

A chat is a list of turns, one per exchange. The result of an exchange belongs to its turn: record_result writes it
there, and the chat's task-level result fields keep a copy of what was written to the latest turn (see Task).

The store takes what it needs from the rest of the worker as arguments: where the files go, what a restart does to a
saved chat, and what happens when a chat stops waiting on its owner.
"""
from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, TypedDict

# A turn's id: 32 lower-case hex digits (uuid4().hex).
TURN_ID_RE = re.compile(r"[0-9a-f]{32}")

# The result of an exchange. The turn owns these fields; the task-level fields of the same names copy what was written
# to the latest turn.
TURN_RESULT_FIELDS = (
    "status", "summary", "details", "question", "choices", "started_at", "completed_at",
    "returncode", "changes", "validation_errors", "lovelace_results", "error",
    "attachments", "config_check", "recovery_files", "backups", "verification", "verification_attachments", "approvals",
)

# Kept in task.json only: the task index leaves them out to stay small.
INDEX_EXCLUDED_FIELDS = frozenset({"prompt", "reply_history", "turns"})


class TurnResult(TypedDict, total=False):
    """The result of one exchange: the fields of TURN_RESULT_FIELDS."""

    status: str
    summary: str
    details: str
    question: str
    choices: list[str]
    started_at: str
    completed_at: str
    returncode: int | None
    changes: dict[str, list[str]]
    validation_errors: list[Any]
    lovelace_results: list[Any]
    error: str
    attachments: list[dict[str, Any]]
    config_check: dict[str, str]
    recovery_files: list[Any]
    backups: list[Any]
    verification: list[Any]
    verification_attachments: list[dict[str, Any]]
    approvals: list[dict[str, Any]]


class Turn(TurnResult, total=False):
    """One exchange of a chat: the owner's message, how it ran, and its result.

    A turn from a version before chat history is rebuilt by task_turns and carries legacy.
    """

    turn_id: str
    message: str
    created_at: str
    updated_at: str
    legacy: bool
    prompt_attachments: list[dict[str, Any]]
    permission_mode: str
    execution_settings: dict[str, Any]


class Task(TurnResult, total=False):
    """A chat as task.json holds it: its turns and the keys the store reads.

    The TurnResult fields at task level are deprecated: they copy what was written to the latest turn, so that the
    task index, the many readers of task["status"] and the like, and a rollback to 0.5.x, which reads only them, keep
    working. A later release drops them with its own change. The rest of the worker adds its own keys (title, session_id,
    chat_settings, pinned and others); a record from a version before chat history has no turns.
    """

    task_id: str
    created_at: str
    updated_at: str
    prompt: str
    reply_history: list[dict[str, str]]
    current_turn_id: str
    turns: list[Turn]
    cancellation_requested: bool


def atomic_json_write(path: Path, value: Any) -> None:
    """Replace metadata only after the complete new document has been written."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def task_turns(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt legacy records without inventing missing intermediate responses."""
    if "turns" in task:
        return copy.deepcopy(task["turns"])
    turns = [{
        "turn_id": "legacy", "message": task.get("prompt", ""),
        "created_at": task.get("created_at", ""), "legacy": True,
    }]
    for index, reply in enumerate(task.get("reply_history") or []):
        turns.append({
            "turn_id": f"legacy-{index + 1}", "message": reply.get("reply", ""),
            "created_at": reply.get("at", ""), "legacy": True,
        })
    # The task index still knows the latest turn id when task.json was lost; with it, the
    # recovered chat finds that turn's saved activity.
    current_turn = task.get("current_turn_id")
    if isinstance(current_turn, str) and TURN_ID_RE.fullmatch(current_turn):
        turns[-1]["turn_id"] = current_turn
    turns[-1].update({key: copy.deepcopy(task[key]) for key in TURN_RESULT_FIELDS if key in task})
    return turns


def current_turn(task: Task) -> Turn | None:
    """Return the chat's current turn, or None when it has none (a record from before chat history)."""
    turns = task.get("turns") or []
    if turns and turns[-1].get("turn_id") == task.get("current_turn_id"):
        return turns[-1]
    return None


def record_result(task: Task, result: TurnResult, updated_at: str) -> None:
    """Write an exchange's result to the current turn, and a copy of the same fields to the task-level fields.

    Nothing goes from the task-level fields to the turn. updated_at stamps the turn even when result is empty. A record
    with no current turn keeps the result at task level only, where versions before chat history kept it.
    """
    turn = current_turn(task)
    if turn is not None:
        turn.update(copy.deepcopy(result))
        turn["updated_at"] = updated_at
    task.update(result)


class TaskStore:
    """The worker's chats in memory and on disk, behind one lock.

    root and index_file return the task root folder and the task index file; they are read at each use, so a
    changed setting takes effect at once. now returns the time to stamp. waits_on_owner says whether a chat waits
    for its owner, and wait_ended is called, outside the lock, when an update ends that wait.
    """

    def __init__(
        self,
        *,
        root: Callable[[], Path],
        index_file: Callable[[], Path],
        now: Callable[[], str],
        waits_on_owner: Callable[[dict[str, Any]], bool],
        wait_ended: Callable[[str], None],
    ) -> None:
        self.root = root
        self.index_file = index_file
        self.now = now
        self.waits_on_owner = waits_on_owner
        self.wait_ended = wait_ended
        self.lock = threading.RLock()
        self.tasks: dict[str, Task] = {}
        # Chats that have a runner thread, from its start until it has published the outcome.
        self.active_runners: set[str] = set()

    def task_dir(self, task_id: str) -> Path:
        """Return the folder that holds a task's files."""
        return self.root() / task_id

    def run_dir(self, task_id: str) -> Path:
        """Keep each exchange's prompts, output and snapshot in its own directory."""
        with self.lock:
            turn_id = self.tasks.get(task_id, {}).get("current_turn_id")
        root = self.task_dir(task_id)
        return root / "turns" / turn_id if turn_id else root

    def write_task(self, task_id: str, task: Task) -> None:
        """Write one chat's task.json, making its folder when needed."""
        task_dir = self.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(task_dir / "task.json", task)

    def save_index(self) -> None:
        """Write every task, without its prompt, reply history, and turns, to the task index file while holding the lock."""
        with self.lock:
            slim = {
                task_id: {key: value for key, value in task.items() if key not in INDEX_EXCLUDED_FIELDS}
                for task_id, task in self.tasks.items()
            }
            atomic_json_write(self.index_file(), slim)

    def load(
        self,
        restore: Callable[[str, dict[str, Any]], bool],
        holds_secret_diff: Callable[[dict[str, Any]], bool],
        redact: Callable[[str], str],
    ) -> None:
        """Load the saved tasks into memory at startup.

        Each task's own task.json is read first; the task index only adds the tasks that have none. restore prepares
        each saved task for this run and returns whether it emptied a stored diff of a secret file; the files that
        held such a diff are written again, so no copy stays on disk. holds_secret_diff finds such a diff in the index.
        """
        root = self.root()
        secrets_forgotten = False
        if root.exists():
            for task_file in root.glob("*/task.json"):
                try:
                    task = json.loads(task_file.read_text(encoding="utf-8"))
                    task_id = str(task.get("task_id") or task_file.parent.name)
                    task["task_id"] = task_id
                    if restore(task_id, task):
                        secrets_forgotten = True
                        atomic_json_write(task_file, task)
                    self.tasks[task_id] = task
                except Exception as exc:
                    print(f"Could not load task metadata from {task_file}: {exc}", flush=True)
        index_file = self.index_file()
        if index_file.exists():
            try:
                loaded = json.loads(index_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for task_id, task in loaded.items():
                        if task_id not in self.tasks:
                            secrets_forgotten = restore(task_id, task) or secrets_forgotten
                        self.tasks.setdefault(task_id, task)
                    # The index also holds each task's approvals; a diff it saved for a secret file goes too.
                    if any(holds_secret_diff(task) for task in loaded.values() if isinstance(task, dict)):
                        secrets_forgotten = True
            except Exception as exc:
                print(f"Could not load task index: {exc}", flush=True)
        if secrets_forgotten:
            try:
                self.save_index()
            except OSError as exc:
                print(f"Could not save the task index: {redact(str(exc))}", flush=True)

    def update(self, task_id: str, updates: dict[str, Any], *, touch: bool) -> bool:
        """Apply changes under the lock and persist them; the result fields among them go to the current turn.

        With touch, a missing task is created and updated_at moves; without it, a missing task is left alone and
        False is returned. Once cancellation was requested, nothing is changed and False is returned, unless the
        update sets the cancelled status or clears the request.
        """
        with self.lock:
            if not touch and task_id not in self.tasks:
                return False
            task = self.tasks.setdefault(task_id, {"task_id": task_id})
            if (task.get("cancellation_requested") and updates.get("status") != "cancelled"
                    and updates.get("cancellation_requested") is not False):
                return False
            waited = self.waits_on_owner(task)
            # First the chat's own keys, which can open a new turn, then the result, which lands on that turn.
            task.update({key: value for key, value in updates.items() if key not in TURN_RESULT_FIELDS})
            if touch:
                task["updated_at"] = self.now()
            result = {key: value for key, value in updates.items() if key in TURN_RESULT_FIELDS}
            record_result(task, result, task.get("updated_at") or self.now())
            wait_ended = waited and not self.waits_on_owner(task)
            self.write_task(task_id, task)
        self.save_index()
        if wait_ended:
            self.wait_ended(task_id)
        return True

    def cancellation_requested(self, task_id: str) -> bool:
        """Return whether cancellation won the task's terminal-state race."""
        with self.lock:
            return bool(self.tasks.get(task_id, {}).get("cancellation_requested"))
