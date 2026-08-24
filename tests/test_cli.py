"""End-to-end CLI behaviour, with the scheduler replaced by a recorder."""

from __future__ import annotations

from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path

import pytest
from conftest import FailingRunner, Tree
from typer.testing import CliRunner, Result

from automationctl import backends, records
from automationctl.backends import Backend
from automationctl.cli import app
from automationctl.commands import RecordingRunner
from automationctl.locks import run_lock

runner = CliRunner()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingRunner]:
    """Replace every scheduler control command with a recorder."""
    recording = RecordingRunner()
    original = backends.create

    def fake_create(name: str, **kwargs: object) -> Backend:
        kwargs["runner"] = recording
        return original(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends, "create", fake_create)
    yield recording


@pytest.fixture
def cli(tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tree:
    monkeypatch.setenv("AUTOMATIONCTL_STATE_DIR", str(tree.state))
    monkeypatch.setenv("AUTOMATIONCTL_UNIT_DIR", str(tmp_path / "units"))
    monkeypatch.setenv("AUTOMATIONCTL_EXECUTABLE", "/opt/bin/automationctl")
    monkeypatch.setenv("AUTOMATIONCTL_BACKEND", "systemd")
    return tree


def invoke(cli: Tree, *args: str) -> Result:
    return runner.invoke(app, [*args, "--manifest", str(cli.manifest_path), "--host", "testhost"])


def invoke_as(cli: Tree, host: str, *args: str) -> Result:
    return runner.invoke(app, [*args, "--manifest", str(cli.manifest_path), "--host", host])


@pytest.fixture
def failing_recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingRunner]:
    """A scheduler that refuses every control command."""
    recording = FailingRunner()
    original = backends.create

    def fake_create(name: str, **kwargs: object) -> Backend:
        kwargs["runner"] = recording
        return original(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends, "create", fake_create)
    yield recording


def test_version_flag_reports_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == version("automationctl")


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "automationctl" in result.output


def test_every_designed_verb_is_present() -> None:
    result = runner.invoke(app, ["--help"])
    for verb in (
        "run",
        "exec",
        "submit",
        "install",
        "lint",
        "list",
        "status",
        "logs",
        "pause",
        "resume",
        "uninstall",
        "doctor",
        "prune",
        "catch-up",
    ):
        assert verb in result.output


def test_missing_manifest_is_a_usage_error() -> None:
    result = runner.invoke(app, ["lint", "--manifest", "/nonexistent/manifest.toml"])
    assert result.exit_code == 2
    assert "file not found" in result.output


def test_lint_passes_on_a_clean_tree(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "hi"]\n')
    result = invoke(cli, "lint")
    assert result.exit_code == 0
    assert "lint: ok" in result.output


def test_lint_fails_on_a_policy_violation(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "--danger"]\n')
    result = invoke(cli, "lint")
    assert result.exit_code == 1
    assert "forbidden entry" in result.output


def test_run_executes_and_records(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "hi"]\n')
    result = invoke(cli, "run", "hello")
    assert result.exit_code == 0
    last = records.read_last(cli.state, "hello")
    assert last is not None
    assert last["status"] == "ok"
    assert (Path(last["run_dir"]) / records.STDOUT_FILE).read_text() == "hi\n"


def test_run_propagates_a_failing_exit_code(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/sh", "-c", "exit 7"]\n')
    assert invoke(cli, "run", "hello").exit_code == 7


def test_exec_is_the_substrate_entrypoint(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    assert invoke(cli, "exec", "hello").exit_code == 0
    assert records.latest_run(cli.state, "hello") is not None


def test_unknown_task_is_a_usage_error(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    result = invoke(cli, "run", "ghost")
    assert result.exit_code == 2
    assert "unknown task: ghost" in result.output


def test_list_reports_schedule_and_last_outcome(cli: Tree) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    invoke(cli, "run", "hello")
    result = invoke(cli, "list")
    assert "daily 03:00" in result.output
    assert "not installed" in result.output
    assert "ok (" in result.output


def test_install_dry_run_writes_nothing(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    result = invoke(cli, "install", "--dry-run", "--diff")
    assert result.exit_code == 0
    assert "create:" in result.output
    assert "OnCalendar=*-*-* 03:00:00" in result.output
    assert not (tmp_path / "units").exists()
    assert recorder.calls == []


def test_install_reconciles_and_enables(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    """The catch-up units are rendered and enabled alongside the task's own."""
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    assert invoke(cli, "install").exit_code == 0
    units = sorted(path.name for path in (tmp_path / "units").iterdir())
    assert units == [
        "automationctl-catchup.service",
        "automationctl-catchup.timer",
        "automationctl-hello.service",
        "automationctl-hello.timer",
    ]
    assert recorder.transcript == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now automationctl-hello.timer",
        "systemctl --user enable --now automationctl-catchup.timer",
    ]


def test_install_renders_no_catchup_units_without_a_persistent_calendar_task(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    """Triggers a host has nothing to use would only produce no-op sweeps."""
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "every 15m"\n'
    )
    assert invoke(cli, "install").exit_code == 0
    units = sorted(path.name for path in (tmp_path / "units").iterdir())
    assert units == ["automationctl-hello.service", "automationctl-hello.timer"]
    assert not any("catchup" in line for line in recorder.transcript)


def test_install_garbage_collects_catchup_units_a_host_stops_wanting(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    """They flow through the managed-prefix path like any other generated unit."""
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    invoke(cli, "install")
    assert (tmp_path / "units" / "automationctl-catchup.timer").exists()

    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    assert invoke(cli, "install").exit_code == 0
    assert not (tmp_path / "units" / "automationctl-catchup.timer").exists()
    assert not (tmp_path / "units" / "automationctl-catchup.service").exists()
    assert "systemctl --user disable --now automationctl-catchup.timer" in recorder.transcript


def test_uninstall_all_removes_the_catchup_units(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    invoke(cli, "install")
    assert invoke(cli, "uninstall", "--all").exit_code == 0
    assert list((tmp_path / "units").iterdir()) == []
    assert "systemctl --user disable --now automationctl-catchup.timer" in recorder.transcript


def test_install_refuses_a_repository_that_fails_lint(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "--danger"]\n')
    assert invoke(cli, "install").exit_code == 1
    assert not (tmp_path / "units").exists()
    assert recorder.calls == []


def test_install_garbage_collects_stale_units(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / "automationctl-old.timer").write_text("stale\n", encoding="utf-8")
    (unit_dir / "keep-me.service").write_text("mine\n", encoding="utf-8")
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    invoke(cli, "install")
    assert not (unit_dir / "automationctl-old.timer").exists()
    assert (unit_dir / "keep-me.service").exists()
    assert "systemctl --user disable --now automationctl-old.timer" in recorder.transcript


def test_install_refuses_an_undeclared_host_and_removes_nothing(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    """A typo in --host must not garbage-collect the whole installation."""
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    invoke(cli, "install")
    installed = sorted(path.name for path in (tmp_path / "units").iterdir())
    assert "automationctl-hello.timer" in installed

    result = invoke_as(cli, "typohost", "install")
    assert result.exit_code != 0
    assert "is not declared" in result.output
    assert sorted(path.name for path in (tmp_path / "units").iterdir()) == installed


def test_install_gate_ignores_specs_belonging_to_another_host(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    """A launchd-only escape hatch on the other host must not block this one."""
    cli.write_manifest(
        "schema_version = 1\n\n"
        "[hosts.testhost]\n"
        'tasks = ["mine"]\n\n'
        "[hosts.otherhost]\n"
        'tasks = ["theirs"]\n'
    )
    cli.write_task("mine", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    cli.write_task(
        "theirs",
        'description = "d"\ncommand = ["/usr/bin/true"]\n\n'
        "[schedule]\n"
        "launchd = [{ Weekday = 1, Hour = 9, Minute = 0 }]\n",
    )
    result = invoke(cli, "install")
    assert result.exit_code == 0
    assert (tmp_path / "units" / "automationctl-mine.service").exists()
    # The full-repository scan still reports the other host's problem.
    assert invoke(cli, "lint").exit_code == 1


def test_install_exits_non_zero_when_the_scheduler_refuses(
    cli: Tree, tmp_path: Path, failing_recorder: RecordingRunner
) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    result = invoke(cli, "install")
    assert result.exit_code == 1
    assert "scheduler command(s) failed" in result.output
    # The files are still written: the desired state is on disk either way.
    assert (tmp_path / "units" / "automationctl-hello.timer").exists()


def test_install_does_not_claim_success_after_a_refused_command(
    cli: Tree, failing_recorder: RecordingRunner
) -> None:
    """The last line a reader sees must not be a false 'installed'."""
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    result = invoke(cli, "install")
    assert result.exit_code == 1
    assert "installed 1 task(s)" not in result.output
    assert "scheduler command(s) failed" in result.output


def test_uninstall_all_is_permitted_for_an_undeclared_host(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    """Cleaning up a host the manifest no longer declares is what --all is for."""
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    invoke(cli, "install")
    assert list((tmp_path / "units").iterdir())

    result = invoke_as(cli, "decommissioned", "uninstall", "--all")
    assert result.exit_code == 0
    assert list((tmp_path / "units").iterdir()) == []


@pytest.mark.parametrize("verb", ["pause", "resume", "submit"])
def test_task_verbs_exit_non_zero_when_the_scheduler_refuses(
    cli: Tree, failing_recorder: RecordingRunner, verb: str
) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    assert invoke(cli, verb, "hello").exit_code == 1


def test_uninstall_exits_non_zero_when_the_scheduler_refuses(
    cli: Tree, tmp_path: Path, failing_recorder: RecordingRunner
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / "automationctl-hello.timer").write_text("stale\n", encoding="utf-8")
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    assert invoke(cli, "uninstall", "--all").exit_code == 1


def test_follow_reports_a_missing_log_program_cleanly(
    cli: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')

    def missing(argv: list[str]) -> int:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr("automationctl.cli.subprocess.call", missing)
    result = invoke(cli, "logs", "hello", "--follow")
    assert result.exit_code == 2
    assert "cannot follow logs" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_pause_and_resume_are_substrate_only(cli: Tree, recorder: RecordingRunner) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    invoke(cli, "pause", "hello")
    invoke(cli, "resume", "hello")
    assert recorder.transcript == [
        "systemctl --user disable --now automationctl-hello.timer",
        "systemctl --user enable --now automationctl-hello.timer",
    ]


def test_submit_starts_the_service(cli: Tree, recorder: RecordingRunner) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    assert invoke(cli, "submit", "hello").exit_code == 0
    assert recorder.transcript == ["systemctl --user start automationctl-hello.service"]


def test_uninstall_all_removes_managed_files(
    cli: Tree, tmp_path: Path, recorder: RecordingRunner
) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    invoke(cli, "install")
    assert invoke(cli, "uninstall", "--all").exit_code == 0
    assert list((tmp_path / "units").iterdir()) == []


def test_uninstall_requires_exactly_one_target(cli: Tree, recorder: RecordingRunner) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    assert invoke(cli, "uninstall").exit_code == 2


def test_status_and_logs_read_the_run_records(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "recorded"]\n')
    invoke(cli, "run", "hello")
    assert "ok" in invoke(cli, "status", "hello").output
    assert "recorded" in invoke(cli, "logs", "hello").output


def test_status_prints_the_catch_up_decision(cli: Tree) -> None:
    """A schedule catch-up cannot evaluate has to say so somewhere visible."""
    cli.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/true"]\n\n'
        "[schedule]\n"
        'systemd = "Mon..Fri *-*-* 09:00:00"\n',
    )
    output = invoke(cli, "status", "hello").output
    assert "catch-up: not due" in output
    assert "opaque to catch-up" in output
    assert "systemd Persistent=" in output


def test_catch_up_dry_run_reports_decisions(cli: Tree) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    result = invoke(cli, "catch-up", "--dry-run")
    assert result.exit_code == 0
    assert "due  hello: no recorded run" in result.output
    assert records.latest_run(cli.state, "hello") is None


def test_catch_up_runs_missed_tasks(cli: Tree) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    assert invoke(cli, "catch-up").exit_code == 0
    assert records.latest_run(cli.state, "hello") is not None


def test_prune_trims_run_records(cli: Tree) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    for _ in range(3):
        invoke(cli, "run", "hello")
    assert runner.invoke(app, ["prune", "--keep-runs", "1"]).exit_code == 0
    assert len(records.run_dirs(cli.state, "hello")) == 1


def test_doctor_reports_checks(cli: Tree, recorder: RecordingRunner) -> None:
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "hi"]\n')
    result = invoke(cli, "doctor")
    assert "manifest:" in result.output
    assert "/bin/echo" in result.output
    for call in recorder.calls:
        assert call[0] in {"systemctl", "loginctl"}


def test_doctor_reports_catch_up_trigger_coverage(cli: Tree, recorder: RecordingRunner) -> None:
    cli.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    missing = invoke(cli, "doctor").output
    assert "FAIL catch-up triggers: automationctl-catchup.timer is missing" in missing

    invoke(cli, "install")
    assert "ok   catch-up triggers: automationctl-catchup.timer installed" in (
        invoke(cli, "doctor").output
    )


def test_run_reports_the_skip_reason(cli: Tree) -> None:
    """The implicit lock makes skips routine, so a silent no-op is not enough."""
    cli.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "hi"]\n')
    with run_lock(cli.state / "locks", "hello"):
        result = invoke(cli, "run", "hello")
    assert result.exit_code == 0
    assert "skipped" in result.output
    assert "already running" in result.output
