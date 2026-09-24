"""Manual tasks have no scheduler control surface."""

from __future__ import annotations

from pathlib import Path

from conftest import write_task
from typer.testing import CliRunner

from automationctl.cli import app


def test_pause_and_resume_reject_manual_task(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    source = write_task(tmp_path / "manual.toml", "manual")
    assert runner.invoke(app, ["install", str(source)], env=isolated_env).exit_code == 0
    assert runner.invoke(app, ["pause", "manual"], env=isolated_env).exit_code != 0
    assert runner.invoke(app, ["resume", "manual"], env=isolated_env).exit_code != 0
