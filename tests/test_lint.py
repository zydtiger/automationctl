"""Task-local lint and machine policy checks."""

from __future__ import annotations

from pathlib import Path

from automationctl.lint import lint_task
from automationctl.spec import LintPolicy, MachinePolicy, NotifyTransport, TaskSpec


def test_deny_list_paths_and_task_notification_references(tmp_path: Path) -> None:
    policy = MachinePolicy(tmp_path / "config.toml", LintPolicy(("danger",)))
    task = TaskSpec(
        "check",
        tmp_path / "check.toml",
        "check",
        ("tool", "danger"),
        cwd="relative",
        on_failure=("notify:missing",),
    )
    messages = [item.message for item in lint_task(task, policy, "systemd").errors]
    assert any("forbidden" in message for message in messages)
    assert any("cwd must" in message for message in messages)
    assert any("undefined task notification" in message for message in messages)


def test_allow_full_access_is_a_warning_and_task_notify_resolves(tmp_path: Path) -> None:
    policy = MachinePolicy(tmp_path / "config.toml", LintPolicy(("danger",)))
    task = TaskSpec(
        "check",
        tmp_path / "check.toml",
        "check",
        ("tool", "danger"),
        allow_full_access=True,
        on_failure=("notify:hook",),
        notify={"hook": NotifyTransport("hook", "command", command=("/bin/true",))},
    )
    report = lint_task(task, policy, "systemd")
    assert report.ok and report.diagnostics[0].level == "warning"
