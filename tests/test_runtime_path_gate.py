"""Runtime paths cannot be process-relative."""

from __future__ import annotations

from pathlib import Path

from automationctl.lint import lint_task
from automationctl.spec import MachinePolicy, TaskSpec


def test_relative_cwd_env_file_and_path_prepend_are_rejected(tmp_path: Path) -> None:
    task = TaskSpec(
        "check",
        tmp_path / "check.toml",
        "check",
        ("/bin/true",),
        cwd="work",
        env_files=("env",),
        path_prepend=("bin",),
    )
    messages = [
        item.message
        for item in lint_task(task, MachinePolicy(tmp_path / "config.toml"), "systemd").errors
    ]
    assert len([message for message in messages if "must be an absolute" in message]) == 3
