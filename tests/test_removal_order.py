"""Removal is target-scoped and preserves history."""

from __future__ import annotations

from pathlib import Path

from conftest import write_task
from typer.testing import CliRunner

from automationctl import records
from automationctl.cli import app


def test_remove_keeps_history_and_does_not_remove_other_task(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    first, second = write_task(tmp_path / "a.toml", "a"), write_task(tmp_path / "b.toml", "b")
    assert runner.invoke(app, ["install", str(first)], env=isolated_env).exit_code == 0
    assert runner.invoke(app, ["install", str(second)], env=isolated_env).exit_code == 0
    records.write_last(
        Path(isolated_env["XDG_STATE_HOME"]) / "automationctl", "a", {"status": "ok"}
    )
    assert runner.invoke(app, ["remove", "a"], env=isolated_env).exit_code == 0
    assert records.read_last(Path(isolated_env["XDG_STATE_HOME"]) / "automationctl", "a") == {
        "status": "ok"
    }
    assert (Path(isolated_env["XDG_CONFIG_HOME"]) / "automationctl" / "tasks" / "b.toml").exists()
