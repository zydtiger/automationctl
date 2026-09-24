"""Standalone catch-up decisions preserve persistence semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from automationctl import catchup, records
from automationctl.schedule import parse
from automationctl.spec import TaskSpec


def task(tmp_path: Path, **kwargs: object) -> TaskSpec:
    fields: dict[str, object] = {
        "name": "check",
        "path": tmp_path / "check.toml",
        "description": "check",
        "command": ("/bin/true",),
        "schedule": parse("daily 03:00"),
    }
    fields.update(kwargs)
    return TaskSpec(**fields)  # type: ignore[arg-type]


def test_calendar_task_is_due_then_covered_by_a_record(tmp_path: Path) -> None:
    spec = task(tmp_path)
    now = datetime(2026, 8, 23, 4, tzinfo=UTC)
    assert catchup.decide(spec, state_dir=tmp_path, now=now).due
    records.write_last(tmp_path, "check", {"started_at": "2026-08-23T03:30:00Z", "status": "ok"})
    assert not catchup.decide(spec, state_dir=tmp_path, now=now).due


def test_disabled_manual_and_nonpersistent_tasks_do_not_catch_up(tmp_path: Path) -> None:
    assert not catchup.decide(task(tmp_path, disabled=True), state_dir=tmp_path).due
    assert not catchup.decide(task(tmp_path, schedule=None), state_dir=tmp_path).due
    assert not catchup.decide(task(tmp_path, persistent=False), state_dir=tmp_path).due


def test_skipped_run_never_covers_a_missed_occurrence(tmp_path: Path) -> None:
    spec = task(tmp_path)
    now = datetime(2026, 8, 23, 4, tzinfo=UTC)
    records.write_last(
        tmp_path,
        "check",
        {"started_at": "2026-08-23T03:30:00Z", "status": records.STATUS_SKIPPED},
    )
    decision = catchup.decide(spec, state_dir=tmp_path, now=now)
    assert decision.due and "skipped" in decision.reason
