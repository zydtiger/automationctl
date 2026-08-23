"""Missed-run detection across power-off."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from conftest import Tree, local_tz

from automationctl import records
from automationctl.catchup import decide, plan
from automationctl.spec import effective_persistent

NOW = datetime(2026, 8, 23, 6, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_is_utc() -> Iterator[None]:
    """Schedules are local wall clock; pin local to UTC so NOW reads literally."""
    with local_tz("UTC"):
        yield


def set_last(tree: Tree, task: str, started: str) -> None:
    records.write_last(tree.state, task, {"task": task, "status": "ok", "started_at": started})


def decision(tree: Tree, task: str) -> tuple[bool, str]:
    automations = tree.load()
    result = decide(automations, automations.tasks[task], state_dir=tree.state, now=NOW)
    return result.due, result.reason


def test_never_run_calendar_task_is_due(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    assert decision(tree, "hello") == (True, "no recorded run")


def test_run_before_the_last_occurrence_is_due(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    set_last(tree, "hello", "2026-08-22T03:00:00Z")
    due, reason = decision(tree, "hello")
    assert due
    assert "precedes 2026-08-23T03:00:00Z" in reason


def test_run_after_the_last_occurrence_is_not_due(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    set_last(tree, "hello", "2026-08-23T03:00:04Z")
    assert decision(tree, "hello") == (False, "last run covers the latest occurrence")


def test_manual_task_is_never_due(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\n')
    assert decision(tree, "hello") == (False, "task has no schedule")


def test_non_persistent_task_is_never_due(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\npersistent = false\n',
    )
    assert decision(tree, "hello") == (False, "task is not persistent")


def test_disabled_task_is_never_due(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\ndisabled = true\n',
    )
    assert decision(tree, "hello") == (False, "task is disabled")


def test_interval_task_is_due_after_the_interval_elapses(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\nschedule = "every 15m"\npersistent = true\n',
    )
    set_last(tree, "hello", "2026-08-23T05:00:00Z")
    due, reason = decision(tree, "hello")
    assert due
    assert "interval is 900s" in reason


def test_interval_task_is_not_due_within_the_interval(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\nschedule = "every 15m"\npersistent = true\n',
    )
    set_last(tree, "hello", "2026-08-23T05:55:00Z")
    assert decision(tree, "hello") == (False, "interval has not elapsed")


def test_custom_schedule_is_declined_with_an_explicit_reason(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\n\n'
        '[schedule]\nsystemd = "Mon..Fri *-*-* 09:00:00"\n',
    )
    due, reason = decision(tree, "hello")
    assert due is False
    assert "opaque to catch-up" in reason


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("systemd", "systemd Persistent= still replays missed runs"),
        ("launchd", "launchd does not replay missed runs itself"),
    ],
)
def test_custom_schedule_reason_names_the_backend_in_play(
    tree: Tree, backend: str, expected: str
) -> None:
    """Only systemd has a safety net; the reason must not imply launchd has one."""
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\n\n'
        '[schedule]\nsystemd = "Mon..Fri *-*-* 09:00:00"\n'
        "launchd = [{ Weekday = 1, Hour = 9, Minute = 0 }]\n",
    )
    automations = tree.load()
    result = decide(
        automations,
        automations.tasks["hello"],
        state_dir=tree.state,
        now=NOW,
        backend=backend,
    )
    assert expected in result.reason


def test_custom_schedules_stay_persistent_by_default(tree: Tree) -> None:
    """A raw calendar expression keeps missed-run replay; only catch-up declines."""
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\n\n'
        '[schedule]\nsystemd = "Mon..Fri *-*-* 09:00:00"\n',
    )
    automations = tree.load()
    assert effective_persistent(automations.manifest, automations.tasks["hello"]) is True


@pytest.mark.parametrize(
    ("tz", "now_utc", "last_utc", "due", "why"),
    [
        # UTC+8, daily 03:00 local. "now" is 06:00 local on the 23rd, so the
        # 03:00 local occurrence sits at 19:00Z on the 22nd.
        ("Asia/Shanghai", "2026-08-22T22:00:00Z", "2026-08-22T20:00:00Z", False, "ran after it"),
        ("Asia/Shanghai", "2026-08-22T22:00:00Z", "2026-08-22T18:00:00Z", True, "ran before it"),
        # UTC-7, daily 03:00 local. "now" is 22:00 local on the 22nd, so the
        # most recent occurrence is 03:00 local that morning, at 10:00Z.
        ("America/Los_Angeles", "2026-08-23T05:00:00Z", "2026-08-22T11:00:00Z", False, "covered"),
        ("America/Los_Angeles", "2026-08-23T05:00:00Z", "2026-08-22T09:00:00Z", True, "missed"),
    ],
)
def test_catchup_decisions_follow_the_local_wall_clock(
    tree: Tree, tz: str, now_utc: str, last_utc: str, due: bool, why: str
) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    set_last(tree, "hello", last_utc)
    automations = tree.load()
    with local_tz(tz):
        result = decide(
            automations,
            automations.tasks["hello"],
            state_dir=tree.state,
            now=records.parse_isoformat(now_utc),
        )
    assert result.due is due, why


def test_plan_covers_every_selected_task(tree: Tree) -> None:
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["hello", "other"]\n')
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    tree.write_task("other", 'description = "d"\ncommand = ["true"]\n')
    tree.write_task("unselected", 'description = "d"\ncommand = ["true"]\n')
    decisions = plan(tree.load(), state_dir=tree.state, now=NOW)
    assert [item.task for item in decisions] == ["hello", "other"]
    assert [item.due for item in decisions] == [True, False]
