from __future__ import annotations

import os
from pathlib import Path

from conftest import write_task
from typer.testing import CliRunner

from automationctl.cli import app
from automationctl.config import load_installed


def test_install_materializes_stdin_is_independent_and_propagates_xdg(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    source = tmp_path / "source.toml"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text('JSON {"literal": true} {task}', encoding="utf-8")
    write_task(
        source, "independent", command='["/bin/sh", "-c", "cat"]', extra='stdin_file = "prompt.txt"'
    )
    result = runner.invoke(app, ["install", str(source), "--dry-run", "--diff"], env=isolated_env)
    assert result.exit_code == 0, result.output
    assert not (Path(isolated_env["XDG_CONFIG_HOME"]) / "automationctl").exists()
    result = runner.invoke(app, ["install", str(source)], env=isolated_env)
    assert result.exit_code == 0, result.output
    installed = load_installed("independent", isolated_env)
    assert installed.stdin == 'JSON {"literal": true} {task}'
    assert installed.stdin_file is None
    assert os.stat(installed.path).st_mode & 0o777 == 0o600
    service = Path(isolated_env["AUTOMATIONCTL_UNIT_DIR"]) / "automationctl-independent.service"
    text = service.read_text(encoding="utf-8")
    assert f"XDG_CONFIG_HOME={isolated_env['XDG_CONFIG_HOME']}" in text
    assert f"XDG_STATE_HOME={isolated_env['XDG_STATE_HOME']}" in text
    prompt.unlink()
    source.unlink()
    result = runner.invoke(app, ["run", "independent"], env=isolated_env)
    assert result.exit_code == 0, result.output


def test_replace_is_targeted_and_removes_stale_timer(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    first = write_task(tmp_path / "first.toml", "first", extra='schedule = "daily 01:00"')
    second = write_task(tmp_path / "second.toml", "second", extra='schedule = "daily 02:00"')
    assert runner.invoke(app, ["install", str(first)], env=isolated_env).exit_code == 0
    assert runner.invoke(app, ["install", str(second)], env=isolated_env).exit_code == 0
    write_task(first, "first")
    assert runner.invoke(app, ["install", str(first), "--replace"], env=isolated_env).exit_code == 0
    units = Path(isolated_env["AUTOMATIONCTL_UNIT_DIR"])
    assert not (units / "automationctl-first.timer").exists()
    assert (units / "automationctl-second.timer").exists()
    assert runner.invoke(app, ["install", str(second)], env=isolated_env).exit_code != 0


def test_corrupt_name_still_requires_replace(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    destination = Path(isolated_env["XDG_CONFIG_HOME"]) / "automationctl" / "tasks"
    destination.mkdir(parents=True)
    (destination / "occupied.toml").write_text("not toml = [", encoding="utf-8")
    source = write_task(tmp_path / "occupied.toml", "occupied")
    result = runner.invoke(app, ["install", str(source)], env=isolated_env)
    assert result.exit_code != 0
    assert "already installed" in result.output


def test_dangling_task_symlink_occupies_name_until_replaced(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    destination = Path(isolated_env["XDG_CONFIG_HOME"]) / "automationctl" / "tasks"
    destination.mkdir(parents=True)
    target = destination / "occupied.toml"
    target.symlink_to(tmp_path / "missing-target")
    source = write_task(tmp_path / "occupied.toml", "occupied")
    for args in ((), ("--dry-run",)):
        result = runner.invoke(app, ["install", str(source), *args], env=isolated_env)
        assert result.exit_code != 0 and "already installed" in result.output
    preview = runner.invoke(
        app, ["install", str(source), "--replace", "--dry-run"], env=isolated_env
    )
    assert preview.exit_code == 0 and target.is_symlink()
    result = runner.invoke(app, ["install", str(source), "--replace"], env=isolated_env)
    assert result.exit_code == 0 and not target.is_symlink()
    assert load_installed("occupied", isolated_env).name == "occupied"
