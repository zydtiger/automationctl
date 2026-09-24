"""Inspection can read records after a task definition is removed."""

from __future__ import annotations

from pathlib import Path

from conftest import write_task
from typer.testing import CliRunner

from automationctl.cli import app


def test_status_and_logs_remain_available_after_remove(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    source = write_task(tmp_path / "task.toml", "task", command='["/bin/echo", "history"]')
    assert runner.invoke(app, ["install", str(source)], env=isolated_env).exit_code == 0
    assert runner.invoke(app, ["run", "task"], env=isolated_env).exit_code == 0
    assert runner.invoke(app, ["remove", "task"], env=isolated_env).exit_code == 0
    assert "history" in runner.invoke(app, ["logs", "task"], env=isolated_env).output
    assert "task:" in runner.invoke(app, ["status", "task"], env=isolated_env).output


def test_installed_task_observability_includes_desired_substrate_and_catchup(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    source = write_task(tmp_path / "task.toml", "task", extra='schedule = "daily 03:00"')
    assert runner.invoke(app, ["install", str(source)], env=isolated_env).exit_code == 0
    listed = runner.invoke(app, ["list"], env=isolated_env).output
    status = runner.invoke(app, ["status", "task"], env=isolated_env).output
    assert "SUBSTRATE" in listed and "enabled" in listed
    assert "desired: enabled" in status and "substrate:" in status and "catch-up:" in status
