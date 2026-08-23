"""Missed-run detection.

launchd coalesces runs missed while a Mac sleeps but not across power-off, and
systemd's ``Persistent=true`` only covers calendar timers. Catch-up therefore
lives in the wrapper layer, where both platforms behave identically: compare
each persistent task's schedule against ``last/<task>.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import records
from .config import Automations
from .schedule import previous_occurrence
from .spec import TaskSpec, effective_persistent


@dataclass(frozen=True)
class CatchupDecision:
    """Whether one task missed its last scheduled occurrence."""

    task: str
    due: bool
    reason: str
    missed_at: datetime | None = None


def _last_started(state_dir: Path, task: str) -> datetime | None:
    last = records.read_last(state_dir, task)
    if last is None:
        return None
    started = last.get("started_at")
    if not isinstance(started, str):
        return None
    try:
        return records.parse_isoformat(started)
    except ValueError:
        return None


def decide(
    automations: Automations,
    task: TaskSpec,
    *,
    state_dir: Path,
    now: datetime | None = None,
) -> CatchupDecision:
    """Decide whether ``task`` should be run now to catch up a missed occurrence."""
    now = now if now is not None else records.utcnow()
    if task.disabled:
        return CatchupDecision(task.name, False, "task is disabled")
    if task.schedule is None:
        return CatchupDecision(task.name, False, "task has no schedule")
    if not effective_persistent(automations.manifest, task):
        return CatchupDecision(task.name, False, "task is not persistent")

    last_started = _last_started(state_dir, task.name)

    if task.schedule.kind == "interval":
        interval = task.schedule.interval_seconds
        assert interval is not None
        if last_started is None:
            return CatchupDecision(task.name, True, "no recorded run")
        elapsed = now - last_started
        if elapsed > timedelta(seconds=interval):
            return CatchupDecision(
                task.name,
                True,
                f"last run was {int(elapsed.total_seconds())}s ago, interval is {interval}s",
                last_started + timedelta(seconds=interval),
            )
        return CatchupDecision(task.name, False, "interval has not elapsed")

    if task.schedule.kind == "raw":
        return CatchupDecision(
            task.name,
            False,
            "per-backend schedule table is opaque to catch-up; "
            "the backend's own missed-run handling applies",
        )

    occurrence = previous_occurrence(task.schedule, now)
    if occurrence is None:
        return CatchupDecision(task.name, False, "no past occurrence")
    if last_started is None:
        return CatchupDecision(task.name, True, "no recorded run", occurrence)
    if last_started < occurrence:
        return CatchupDecision(
            task.name,
            True,
            f"last run {records.isoformat(last_started)} precedes {records.isoformat(occurrence)}",
            occurrence,
        )
    return CatchupDecision(task.name, False, "last run covers the latest occurrence")


def plan(
    automations: Automations,
    *,
    state_dir: Path,
    now: datetime | None = None,
) -> list[CatchupDecision]:
    """Decide catch-up for every task this host selects.

    Decisions are evaluated against a single instant, and the caller runs the
    due tasks serially in the foreground. Serial execution is deliberate: a
    machine returning from a week offline should not start every missed agent
    job at once, and named locks would turn most of them into skips anyway.
    """
    moment = now if now is not None else records.utcnow()
    return [
        decide(automations, task, state_dir=state_dir, now=moment)
        for task in automations.enabled_tasks()
    ]
