"""Desired-state and scheduler-state reporting for ``automationctl list``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import Tree
from typer.testing import CliRunner, Result

from automationctl import backends
from automationctl.backends import Backend
from automationctl.cli import app
from automationctl.commands import CommandResult, RecordingRunner

runner = CliRunner()
ListCli = tuple[Tree, Path, dict[tuple[str, ...], CommandResult]]


@pytest.fixture
def list_cli(tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ListCli]:
    unit_dir = tmp_path / "units"
    responses: dict[tuple[str, ...], CommandResult] = {}
    recording = RecordingRunner(responses=responses)
    original = backends.create

    def fake_create(name: str, **kwargs: object) -> Backend:
        kwargs["runner"] = recording
        return original(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends, "create", fake_create)
    monkeypatch.setattr("automationctl.paths.state_dir", lambda env=None: tree.state)
    monkeypatch.setenv("AUTOMATIONCTL_UNIT_DIR", str(unit_dir))
    monkeypatch.setenv("AUTOMATIONCTL_EXECUTABLE", "/opt/bin/automationctl")
    monkeypatch.setenv("AUTOMATIONCTL_BACKEND", "systemd")
    yield tree, unit_dir, responses


def invoke(tree: Tree, *args: str) -> Result:
    return runner.invoke(
        app,
        [*args, "--manifest", str(tree.manifest_path), "--host", "testhost"],
    )


def scheduled_task(*, disabled: bool = False) -> str:
    return 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n' + (
        "disabled = true\n" if disabled else ""
    )


def test_list_separates_desired_and_substrate_state(list_cli: ListCli) -> None:
    tree, _, _ = list_cli
    tree.write_task("hello", scheduled_task())

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "DESIRED" in result.output
    assert "SUBSTRATE" in result.output
    assert "enabled" in result.output
    assert "not installed" in result.output


def test_disabled_desired_state_still_reports_an_active_timer(list_cli: ListCli) -> None:
    tree, _, responses = list_cli
    tree.write_task("hello", scheduled_task())
    assert invoke(tree, "install").exit_code == 0
    tree.write_task("hello", scheduled_task(disabled=True))
    argv = ("systemctl", "--user", "is-enabled", "automationctl-hello.timer")
    responses[argv] = CommandResult(argv, 0, stdout="enabled\n")

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "disabled" in result.output
    assert "active" in result.output


def test_disabled_task_with_changed_schedule_reports_stale(list_cli: ListCli) -> None:
    tree, _, _ = list_cli
    tree.write_task("hello", scheduled_task())
    assert invoke(tree, "install").exit_code == 0
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/true"]\n'
        'schedule = "daily 04:00"\ndisabled = true\n',
    )

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "disabled" in result.output
    assert "stale" in result.output


def test_list_reports_a_partial_generated_set(list_cli: ListCli) -> None:
    tree, unit_dir, _ = list_cli
    tree.write_task("hello", scheduled_task())
    unit_dir.mkdir()
    (unit_dir / "automationctl-hello.timer").write_text("partial\n", encoding="utf-8")

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "partial" in result.output


def test_list_reports_stale_generated_content(list_cli: ListCli) -> None:
    tree, unit_dir, _ = list_cli
    tree.write_task("hello", scheduled_task())
    assert invoke(tree, "install").exit_code == 0
    service = unit_dir / "automationctl-hello.service"
    service.write_text(service.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "stale" in result.output


def test_list_reports_a_leftover_timer_after_schedule_removal(list_cli: ListCli) -> None:
    tree, _, _ = list_cli
    tree.write_task("hello", scheduled_task())
    assert invoke(tree, "install").exit_code == 0
    tree.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "stale" in result.output


def test_disabled_manual_task_reports_a_leftover_timer_as_stale(list_cli: ListCli) -> None:
    tree, _, _ = list_cli
    tree.write_task("hello", scheduled_task())
    assert invoke(tree, "install").exit_code == 0
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/true"]\ndisabled = true\n',
    )

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "disabled" in result.output
    assert "stale" in result.output


def test_list_reports_unknown_when_a_reserved_task_cannot_render(list_cli: ListCli) -> None:
    tree, unit_dir, _ = list_cli
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["catchup"]\n')
    tree.write_task("catchup", scheduled_task())
    unit_dir.mkdir()
    (unit_dir / "automationctl-catchup.service").write_text("old\n", encoding="utf-8")
    (unit_dir / "automationctl-catchup.timer").write_text("old\n", encoding="utf-8")

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "unknown" in result.output


@pytest.mark.parametrize("invalid_hour", ['"bad"', "[]"])
def test_list_reports_unknown_when_a_launchd_schedule_cannot_render(
    list_cli: ListCli,
    monkeypatch: pytest.MonkeyPatch,
    invalid_hour: str,
) -> None:
    tree, unit_dir, _ = list_cli
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/true"]\n\n'
        "[schedule]\n"
        f"launchd = {{ Hour = {invalid_hour} }}\n",
    )
    unit_dir.mkdir()
    (unit_dir / "automationctl.hello.plist").write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("AUTOMATIONCTL_BACKEND", "launchd")

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert "unknown" in result.output


def test_launchd_manual_task_reports_installed_not_active(
    list_cli: ListCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree, _, _ = list_cli
    tree.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    monkeypatch.setenv("AUTOMATIONCTL_BACKEND", "launchd")
    assert invoke(tree, "install").exit_code == 0

    result = invoke(tree, "list")

    assert result.exit_code == 0
    task_row = result.output.splitlines()[1]
    assert "installed" in task_row
    assert "active" not in task_row


def test_systemd_manual_task_reports_installed_not_active(list_cli: ListCli) -> None:
    tree, _, _ = list_cli
    tree.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    assert invoke(tree, "install").exit_code == 0

    result = invoke(tree, "list")

    assert result.exit_code == 0
    task_row = result.output.splitlines()[1]
    assert "installed" in task_row
    assert "active" not in task_row


@pytest.mark.parametrize(
    ("scheduler_output", "substrate"),
    [("disabled\n", "inactive"), ("", "unknown")],
)
def test_scheduled_task_reports_the_scheduler_probe_state(
    list_cli: ListCli,
    scheduler_output: str,
    substrate: str,
) -> None:
    tree, _, responses = list_cli
    tree.write_task("hello", scheduled_task())
    assert invoke(tree, "install").exit_code == 0
    argv = ("systemctl", "--user", "is-enabled", "automationctl-hello.timer")
    responses[argv] = CommandResult(argv, 0, stdout=scheduler_output)

    result = invoke(tree, "list")

    assert result.exit_code == 0
    assert substrate in result.output.splitlines()[1]
