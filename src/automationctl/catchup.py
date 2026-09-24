"""Missed-occurrence decisions for installed standalone tasks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import records
from .schedule import previous_occurrence
from .spec import TaskSpec, effective_persistent


@dataclass(frozen=True)
class CatchupDecision:
    task: str
    due: bool
    reason: str
    occurrence: datetime | None = None


def _coverage(state_dir: Path, task: str) -> tuple[datetime | None, str]:
    latest = records.read_last(state_dir, task)
    if latest is None:
        return None, "no recorded run"
    if latest.get("status") == records.STATUS_SKIPPED:
        return None, "the last run was skipped, which never covers an occurrence"
    started = latest.get("started_at")
    if not isinstance(started, str):
        return None, "last run has no start time"
    try:
        return records.parse_isoformat(started), "last run has invalid start time"
    except ValueError:
        return None, "last run has invalid start time"


def decide(
    task: TaskSpec, *, state_dir: Path, now: datetime | None = None, backend: str | None = None
) -> CatchupDecision:
    if task.disabled:
        return CatchupDecision(task.name, False, "task is disabled")
    if task.schedule is None:
        return CatchupDecision(task.name, False, "task has no schedule")
    if not effective_persistent(task):
        return CatchupDecision(task.name, False, "task is not persistent")
    moment = now or records.utcnow()
    last, missing = _coverage(state_dir, task.name)
    if task.schedule.kind == "interval":
        assert task.schedule.interval_seconds is not None
        if last is None:
            return CatchupDecision(task.name, True, missing)
        elapsed = moment - last
        reason = (
            f"last run was {int(elapsed.total_seconds())}s ago, "
            f"interval is {task.schedule.interval_seconds}s"
        )
        return CatchupDecision(
            task.name,
            elapsed > timedelta(seconds=task.schedule.interval_seconds),
            reason,
        )
    if task.schedule.kind == "raw":
        return CatchupDecision(task.name, False, "per-backend schedule table is opaque to catch-up")
    occurrence = previous_occurrence(task.schedule, moment)
    if occurrence is None:
        return CatchupDecision(task.name, False, "no past occurrence")
    if last is None:
        return CatchupDecision(task.name, True, missing, occurrence)
    return CatchupDecision(
        task.name,
        last < occurrence,
        "last run covers the latest occurrence"
        if last >= occurrence
        else f"last run {records.isoformat(last)} precedes {records.isoformat(occurrence)}",
        occurrence,
    )


def triggers_wanted(tasks: Sequence[TaskSpec]) -> bool:
    return any(
        task.schedule is not None
        and task.schedule.is_calendar
        and effective_persistent(task)
        and not task.disabled
        for task in tasks
    )


def plan(
    tasks: Sequence[TaskSpec],
    *,
    state_dir: Path,
    backend: str | None = None,
    now: datetime | None = None,
) -> list[CatchupDecision]:
    moment = now or records.utcnow()
    return [
        decide(task, state_dir=state_dir, backend=backend, now=moment)
        for task in tasks
        if not task.disabled
    ]
