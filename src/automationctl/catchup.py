"""Missed-run detection.

launchd coalesces runs missed while a Mac sleeps but not across power-off, and
systemd's ``Persistent=true`` only covers calendar timers — and neither replays
an occurrence a timezone or clock jump moved past, because the substrate simply
recalculates its next elapse. Catch-up therefore lives in the wrapper layer,
where both platforms behave identically: compare each persistent task's
schedule against ``last/<task>.json``. Which events wake it is the backends'
business; this module only decides.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import records
from .config import Automations
from .schedule import previous_occurrence
from .spec import TaskSpec, effective_persistent

NO_RUN = "no recorded run"
SKIPPED_RUN = "the last run was skipped, which never covers an occurrence"


@dataclass(frozen=True)
class CatchupDecision:
    """Whether one task missed its last scheduled occurrence."""

    task: str
    due: bool
    reason: str
    missed_at: datetime | None = None


def _coverage(state_dir: Path, task: str) -> tuple[datetime | None, str]:
    """Return the start of the run that covers this task, and why there is none.

    A ``skipped`` record is an occurrence that did *not* run: the wrapper's
    implicit run lock turns a duplicate trigger into a skip, and counting one
    as coverage would let the loser of a race cancel the catch-up the missed
    occurrence still needs. The record itself is left alone — a skip is an
    informative outcome and ``list`` and ``status`` show it — because coverage
    is a catch-up question, not a reason to make the last-run pointer lie.
    """
    last = records.read_last(state_dir, task)
    if last is None:
        return None, NO_RUN
    if last.get("status") == records.STATUS_SKIPPED:
        return None, SKIPPED_RUN
    started = last.get("started_at")
    if not isinstance(started, str):
        return None, NO_RUN
    try:
        return records.parse_isoformat(started), NO_RUN
    except ValueError:
        return None, NO_RUN


def _raw_schedule_reason(backend: str | None) -> str:
    """Explain why a per-backend schedule table is not evaluated here.

    What happens to a missed occurrence then depends entirely on the backend,
    so the reason says which one is in play rather than implying every
    platform has a safety net. launchd does not.
    """
    opaque = "per-backend schedule table is opaque to catch-up"
    if backend == "systemd":
        return f"{opaque}; systemd Persistent= still replays missed runs"
    if backend == "launchd":
        return f"{opaque}, and launchd does not replay missed runs itself"
    return opaque


def decide(
    automations: Automations,
    task: TaskSpec,
    *,
    state_dir: Path,
    now: datetime | None = None,
    backend: str | None = None,
) -> CatchupDecision:
    """Decide whether ``task`` should be run now to catch up a missed occurrence."""
    now = now if now is not None else records.utcnow()
    if task.disabled:
        return CatchupDecision(task.name, False, "task is disabled")
    if task.schedule is None:
        return CatchupDecision(task.name, False, "task has no schedule")
    if not effective_persistent(automations.manifest, task):
        return CatchupDecision(task.name, False, "task is not persistent")

    last_started, uncovered = _coverage(state_dir, task.name)

    if task.schedule.kind == "interval":
        interval = task.schedule.interval_seconds
        assert interval is not None
        if last_started is None:
            return CatchupDecision(task.name, True, uncovered)
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
        return CatchupDecision(task.name, False, _raw_schedule_reason(backend))

    occurrence = previous_occurrence(task.schedule, now)
    if occurrence is None:
        return CatchupDecision(task.name, False, "no past occurrence")
    if last_started is None:
        return CatchupDecision(task.name, True, uncovered, occurrence)
    if last_started < occurrence:
        return CatchupDecision(
            task.name,
            True,
            f"last run {records.isoformat(last_started)} precedes {records.isoformat(occurrence)}",
            occurrence,
        )
    return CatchupDecision(task.name, False, "last run covers the latest occurrence")


def triggers_wanted(automations: Automations, tasks: Sequence[TaskSpec]) -> bool:
    """Whether this host has an occurrence a clock or timezone jump can lose.

    Only a persistent *calendar* task qualifies. An interval schedule measures
    elapsed time, which has no wall-clock moment a jump can move past, and its
    generated timer self-recovers through its own monotonic triggers; an
    escape-hatch schedule is opaque to catch-up, which declines it and would
    decline it just as firmly when a trigger fired. A sweep on such a host can
    still find due work — ``decide`` handles interval tasks — but nothing a
    clock or timezone jump makes more likely, so rendering triggers there
    would only duplicate coverage the substrate already provides.
    """
    return any(
        task.schedule is not None
        and task.schedule.is_calendar
        and effective_persistent(automations.manifest, task)
        for task in tasks
    )


def plan(
    automations: Automations,
    *,
    state_dir: Path,
    now: datetime | None = None,
    backend: str | None = None,
) -> list[CatchupDecision]:
    """Decide catch-up for every task this host selects.

    Decisions are evaluated against a single instant, and the caller runs the
    due tasks serially in the foreground. Serial execution is deliberate: a
    machine returning from a week offline should not start every missed agent
    job at once, and named locks would turn most of them into skips anyway.
    """
    moment = now if now is not None else records.utcnow()
    return [
        decide(automations, task, state_dir=state_dir, now=moment, backend=backend)
        for task in automations.enabled_tasks()
    ]
