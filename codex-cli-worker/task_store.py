"""The chats the worker keeps: the records in memory, the lock that guards them, and their files on disk.

Each chat (task) has a folder under the task root with its task.json; the task index holds every chat, without its
prompt, reply history and turns, in one file. A TaskStore owns the dictionary of chats, the lock, and the set of chats
that have a runner thread, and it is the one place that writes those files.

The store takes what it needs from the rest of the worker as arguments: where the files go, what a restart does to a
saved chat, and what happens when a chat stops waiting on its owner.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable

# A turn's id: 32 lower-case hex digits (uuid4().hex).
TURN_ID_RE = re.compile(r"[0-9a-f]{32}")

# The result of an exchange. The current turn keeps a copy of these task-level fields.
TURN_RESULT_FIELDS = (
    "status",
    "summary",
    "details",
    "question",
    "choices",
    "started_at",
    "completed_at",
    "returncode",
    "changes",
    "validation_errors",
    "lovelace_results",
    "error",
    "attachments",
    "config_check",
    "recovery_files",
    "backups",
    "verification",
    "verification_attachments",
    "approvals",
)

# Kept in task.json only: the task index leaves them out to stay small.
INDEX_EXCLUDED_FIELDS = frozenset({"prompt", "reply_history", "turns"})


def atomic_json_write(path: Path, value: Any) -> None:
    """Replace metadata only after the complete new document has been written."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def task_turns(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt legacy records without inventing missing intermediate responses."""
    if "turns" in task:
        return copy.deepcopy(task["turns"])
    turns = [
        {
            "turn_id": "legacy",
            "message": task.get("prompt", ""),
            "created_at": task.get("created_at", ""),
            "legacy": True,
        }
    ]
    for index, reply in enumerate(task.get("reply_history") or []):
        turns.append(
            {
                "turn_id": f"legacy-{index + 1}",
                "message": reply.get("reply", ""),
                "created_at": reply.get("at", ""),
                "legacy": True,
            }
        )
    # The task index still knows the latest turn id when task.json was lost; with it, the
    # recovered chat finds that turn's saved activity.
    current_turn = task.get("current_turn_id")
    if isinstance(current_turn, str) and TURN_ID_RE.fullmatch(current_turn):
        turns[-1]["turn_id"] = current_turn
    turns[-1].update(
        {key: copy.deepcopy(task[key]) for key in TURN_RESULT_FIELDS if key in task}
    )
    return turns


def sync_current_turn(task: dict[str, Any], now: str) -> None:
    """Keep this exchange's outcome alongside the compatible task-level result."""
    turns = task.get("turns") or []
    if turns and turns[-1].get("turn_id") == task.get("current_turn_id"):
        turns[-1].update(
            {key: copy.deepcopy(task[key]) for key in TURN_RESULT_FIELDS if key in task}
        )
        turns[-1]["updated_at"] = task.get("updated_at", now)


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
        self.tasks: dict[str, dict[str, Any]] = {}
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

    def write_task(self, task_id: str, task: dict[str, Any]) -> None:
        """Write one chat's task.json, making its folder when needed."""
        task_dir = self.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(task_dir / "task.json", task)

    def save_index(self) -> None:
        """Write every task, without its prompt, reply history, and turns, to the task index file while holding the lock."""
        with self.lock:
            slim = {
                task_id: {
                    key: value
                    for key, value in task.items()
                    if key not in INDEX_EXCLUDED_FIELDS
                }
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
                    print(
                        f"Could not load task metadata from {task_file}: {exc}",
                        flush=True,
                    )
        index_file = self.index_file()
        if index_file.exists():
            try:
                loaded = json.loads(index_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for task_id, task in loaded.items():
                        if task_id not in self.tasks:
                            secrets_forgotten = (
                                restore(task_id, task) or secrets_forgotten
                            )
                        self.tasks.setdefault(task_id, task)
                    # The index also holds each task's approvals; a diff it saved for a secret file goes too.
                    if any(
                        holds_secret_diff(task)
                        for task in loaded.values()
                        if isinstance(task, dict)
                    ):
                        secrets_forgotten = True
            except Exception as exc:
                print(f"Could not load task index: {exc}", flush=True)
        if secrets_forgotten:
            try:
                self.save_index()
            except OSError as exc:
                print(f"Could not save the task index: {redact(str(exc))}", flush=True)

    def update(self, task_id: str, updates: dict[str, Any], *, touch: bool) -> bool:
        """Apply changes under the lock and persist them.

        With touch, a missing task is created and updated_at moves; without it, a missing task is left alone and
        False is returned. Once cancellation was requested, nothing is changed and False is returned, unless the
        update sets the cancelled status or clears the request.
        """
        with self.lock:
            if not touch and task_id not in self.tasks:
                return False
            task = self.tasks.setdefault(task_id, {"task_id": task_id})
            if (
                task.get("cancellation_requested")
                and updates.get("status") != "cancelled"
                and updates.get("cancellation_requested") is not False
            ):
                return False
            waited = self.waits_on_owner(task)
            task.update(updates)
            wait_ended = waited and not self.waits_on_owner(task)
            if touch:
                task["updated_at"] = self.now()
            sync_current_turn(task, self.now())
            self.write_task(task_id, task)
        self.save_index()
        if wait_ended:
            self.wait_ended(task_id)
        return True

    def cancellation_requested(self, task_id: str) -> bool:
        """Return whether cancellation won the task's terminal-state race."""
        with self.lock:
            return bool(self.tasks.get(task_id, {}).get("cancellation_requested"))
