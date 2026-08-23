"""Missed-run detection across power-off."""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import Tree

from automationctl import records
from automationctl.catchup import decide, plan

NOW = datetime(2026, 8, 23, 6, 0, tzinfo=UTC)


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


def test_custom_schedule_cannot_be_evaluated(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\npersistent = true\n\n'
        '[schedule]\nsystemd = "Mon..Fri *-*-* 09:00:00"\n',
    )
    assert decision(tree, "hello") == (False, "custom schedule cannot be evaluated")


def test_plan_covers_every_selected_task(tree: Tree) -> None:
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["hello", "other"]\n')
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    tree.write_task("other", 'description = "d"\ncommand = ["true"]\n')
    tree.write_task("unselected", 'description = "d"\ncommand = ["true"]\n')
    decisions = plan(tree.load(), state_dir=tree.state, now=NOW)
    assert [item.task for item in decisions] == ["hello", "other"]
    assert [item.due for item in decisions] == [True, False]
