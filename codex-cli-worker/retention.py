"""When a chat with no message for a while is deleted, and when its page warns about it.

The rules, all in this one file so a reader can check them without the server:

* A chat with no message for `chat_retention_days` days (14 by default) is deleted with all its files.
* A pinned chat never expires.
* A scheduled run counts as activity: a schedule saved from a chat keeps that chat alive while the schedule is
  approved and on, and the time of its latest run counts as the chat's latest message.
* The chat page shows a deletion warning for the last `WARNING_DAYS` days before the deletion.
* A chat that is running or waiting is never deleted, and a chat whose last activity cannot be read is kept.

Nothing here touches the disk or the clock; the server passes in the chats, the time and the schedule facts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

DEFAULT_DAYS = 14
DAYS_RANGE = (1, 365)
WARNING_DAYS = 3
SECONDS_PER_DAY = 86400
# Statuses of a chat that is doing something now; the server also passes the ids it knows are active.
BUSY_STATUSES = frozenset({"queued", "running"})
# What a schedule counts for while it is approved and on: this chat is in use now.
ALWAYS_ACTIVE = float("inf")


def retention_days(options: Mapping[str, Any]) -> int:
    """Return the setting in days, clamped to the supported range; a missing or unreadable value is the default."""
    try:
        days = int(options.get("chat_retention_days"))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    low, high = DAYS_RANGE
    return min(high, max(low, days))


def seconds_of(value: Any) -> float | None:
    """Read a saved ISO time as seconds since the epoch; a time without a zone is UTC. None when unreadable."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def scheduled_activity(schedules: Iterable[Mapping[str, Any]], runs: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Map each chat that has a schedule to the time its schedules last kept it in use.

    A schedule that is approved and enabled keeps its chat in use for good (ALWAYS_ACTIVE). Otherwise the chat's
    time is the latest run of any of its schedules; `created` is the run's time in epoch seconds.
    """
    chat_of = {str(item.get("id")): str(item.get("chat_id") or "") for item in schedules}
    result: dict[str, float] = {}
    for item in schedules:
        chat_id = str(item.get("chat_id") or "")
        if chat_id and item.get("approved") and item.get("enabled"):
            result[chat_id] = ALWAYS_ACTIVE
    for run in runs:
        chat_id = chat_of.get(str(run.get("schedule_id")), "")
        created = run.get("created")
        if chat_id and isinstance(created, (int, float)) and not isinstance(created, bool):
            result[chat_id] = max(result.get(chat_id, 0.0), float(created))
    return result


def last_activity(task: Mapping[str, Any], scheduled: Mapping[str, float]) -> float | None:
    """Return when the chat last had a message or a scheduled run, or None when no time can be read."""
    times = [seconds_of(task.get(key)) for key in ("updated_at", "created_at")]
    times.append(scheduled.get(str(task.get("task_id") or "")))
    known = [value for value in times if value is not None]
    return max(known) if known else None


def deletion_time(task: Mapping[str, Any], days: int, scheduled: Mapping[str, float]) -> float | None:
    """Return when the chat is deleted if nothing happens (epoch seconds), or None if it is not on the way out."""
    if task.get("pinned"):
        return None
    active = last_activity(task, scheduled)
    if active is None or active == ALWAYS_ACTIVE:
        return None
    return active + days * SECONDS_PER_DAY


def is_busy(task: Mapping[str, Any], active_ids: Iterable[str] = ()) -> bool:
    """Whether the chat is running or waiting to run, and so must not be deleted."""
    return task.get("status") in BUSY_STATUSES or str(task.get("task_id") or "") in set(active_ids)


def expired_ids(
    tasks: Iterable[Mapping[str, Any]], now: float, days: int, scheduled: Mapping[str, float],
    active_ids: Iterable[str] = (),
) -> list[str]:
    """Return the ids of the chats whose deletion time has come, oldest first."""
    busy_ids = set(active_ids)
    due: list[tuple[float, str]] = []
    for task in tasks:
        when = deletion_time(task, days, scheduled)
        if when is not None and when <= now and not is_busy(task, busy_ids):
            due.append((when, str(task["task_id"])))
    return [task_id for _when, task_id in sorted(due)]


def deletion_warning(
    task: Mapping[str, Any], now: float, days: int, scheduled: Mapping[str, float],
) -> dict[str, Any] | None:
    """Return what the page shows for a chat in its last WARNING_DAYS days, or None.

    `deletes_at` is an ISO time in UTC and `days_left` counts whole days, rounded up, so the last day reads 1.
    """
    when = deletion_time(task, days, scheduled)
    if when is None or when <= now or when - now > WARNING_DAYS * SECONDS_PER_DAY:
        return None
    return {
        "deletes_at": datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds"),
        "days_left": max(1, -int(-(when - now) // SECONDS_PER_DAY)),
    }
