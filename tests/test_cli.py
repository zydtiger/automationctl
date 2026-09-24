"""End-to-end standalone CLI behavior with recording scheduler commands."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest
from conftest import write_task
from typer.testing import CliRunner, Result

from automationctl.backends import Backend
from automationctl.backends.systemd import SystemdBackend
from automationctl.cli import app, prune
from automationctl.commands import CommandResult, RecordingRunner


def install(runner: CliRunner, env: dict[str, str], source: Path, *args: str) -> Result:
    return runner.invoke(app, ["install", str(source), *args], env=env)


def test_version_help_and_supported_verbs(runner: CliRunner) -> None:
    assert runner.invoke(app, ["--version"]).output.strip() == version("automationctl")
    assert "automationctl" in runner.invoke(app, []).output
    help = runner.invoke(app, ["--help"]).output
    for verb in (
        "add",
        "install",
        "remove",
        "run",
        "exec",
        "submit",
        "lint",
        "list",
        "status",
        "logs",
        "pause",
        "resume",
        "doctor",
        "prune",
        "catch-up",
    ):
        assert verb in help
    assert "uninstall" not in help


def test_add_preserves_empty_and_literal_argv_and_expands_cwd(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    result = runner.invoke(
        app,
        ["add", "argv", "--cwd", "~/project", "--", "/bin/echo", "", "$literal", ";"],
        env=isolated_env,
    )
    assert result.exit_code == 0, result.output
    from automationctl.config import load_installed

    spec = load_installed("argv", isolated_env)
    assert spec.command[-3:] == ("", "$literal", ";") and spec.cwd == str(
        Path(isolated_env["HOME"]) / "project"
    )


def test_dry_run_has_zero_mutations_and_source_install_is_self_contained(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    prompt = tmp_path / "prompt"
    prompt.write_text("hello {task}", encoding="utf-8")
    source = write_task(
        tmp_path / "source.toml", "one", command='["/bin/cat"]', extra='stdin_file = "prompt"'
    )
    result = install(runner, isolated_env, source, "--dry-run", "--diff")
    assert result.exit_code == 0 and "create:" in result.output
    assert (
        not (Path(isolated_env["XDG_CONFIG_HOME"]) / "automationctl").exists()
        and not Path(isolated_env["AUTOMATIONCTL_UNIT_DIR"]).exists()
    )
    assert install(runner, isolated_env, source).exit_code == 0
    prompt.unlink()
    source.unlink()
    assert runner.invoke(app, ["run", "one"], env=isolated_env).exit_code == 0


def test_install_replace_is_targeted_and_shared_catchup_reconciles(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    first = write_task(tmp_path / "first.toml", "first", extra='schedule = "daily 01:00"')
    second = write_task(tmp_path / "second.toml", "second", extra='schedule = "daily 02:00"')
    assert (
        install(runner, isolated_env, first).exit_code == 0
        and install(runner, isolated_env, second).exit_code == 0
    )
    units = Path(isolated_env["AUTOMATIONCTL_UNIT_DIR"])
    assert (units / "automationctl-catchup.timer").exists()
    write_task(first, "first")
    assert install(runner, isolated_env, first, "--replace").exit_code == 0
    assert (
        not (units / "automationctl-first.timer").exists()
        and (units / "automationctl-second.timer").exists()
        and (units / "automationctl-catchup.timer").exists()
    )
    assert (
        runner.invoke(app, ["remove", "second"], env=isolated_env).exit_code == 0
        and not (units / "automationctl-catchup.timer").exists()
    )


def test_corrupt_occupied_target_needs_replace_but_remove_does_not_parse_it(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    target = Path(isolated_env["XDG_CONFIG_HOME"]) / "automationctl" / "tasks"
    target.mkdir(parents=True)
    (target / "occupied.toml").write_text("bad = [", encoding="utf-8")
    source = write_task(tmp_path / "occupied.toml", "occupied")
    assert "already installed" in install(runner, isolated_env, source).output
    assert (
        runner.invoke(app, ["remove", "occupied"], env=isolated_env).exit_code == 0
        and not (target / "occupied.toml").exists()
    )


def test_run_exec_status_logs_and_stream_selection(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    source = write_task(
        tmp_path / "task.toml",
        "task",
        command='["/bin/sh", "-c", "echo out; echo err >&2; exit 7"]',
    )
    assert install(runner, isolated_env, source).exit_code == 0
    assert runner.invoke(app, ["run", "task"], env=isolated_env).exit_code == 7
    assert "failed" in runner.invoke(app, ["status", "task"], env=isolated_env).output
    assert "err" in runner.invoke(app, ["logs", "task"], env=isolated_env).output
    assert "out" in runner.invoke(app, ["logs", "task", "--stdout"], env=isolated_env).output
    assert "out" in runner.invoke(app, ["logs", "task", "--both"], env=isolated_env).output
    assert (
        runner.invoke(app, ["logs", "task", "--stdout", "--stderr"], env=isolated_env).exit_code
        == 2
    )


def test_pause_resume_submit_and_scheduler_failure_are_reported(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_task(tmp_path / "task.toml", "task", extra='schedule = "daily 03:00"')
    assert install(runner, isolated_env, source).exit_code == 0
    assert (
        runner.invoke(app, ["pause", "task"], env=isolated_env).exit_code == 0
        and runner.invoke(app, ["resume", "task"], env=isolated_env).exit_code == 0
        and runner.invoke(app, ["submit", "task"], env=isolated_env).exit_code == 0
    )

    def failed_backend(
        name: str | None, unit_dir: Path | None, env: dict[str, str]
    ) -> tuple[str, Backend]:
        argv = ("systemctl", "--user", "enable", "--now", "automationctl-task.timer")
        return (
            "systemd",
            SystemdBackend(
                unit_dir=Path(env["AUTOMATIONCTL_UNIT_DIR"]),
                runner=RecordingRunner(responses={argv: CommandResult(argv, 1, stderr="refused")}),
                executable="automationctl",
                config_home=Path(env["XDG_CONFIG_HOME"]),
                state_home=Path(env["XDG_STATE_HOME"]),
                state_dir=Path(env["XDG_STATE_HOME"]) / "automationctl",
            ),
        )

    monkeypatch.setattr("automationctl.cli._backend", failed_backend)
    result = install(runner, isolated_env, source, "--replace")
    assert (
        result.exit_code == 1
        and "scheduler command" in result.output
        and "installed task" not in result.output
    )


def test_follow_rejects_stream_selection_and_missing_program(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_task(tmp_path / "task.toml", "task")
    assert install(runner, isolated_env, source).exit_code == 0
    assert (
        runner.invoke(app, ["logs", "task", "--follow", "--stdout"], env=isolated_env).exit_code
        == 2
    )

    def missing(argv: tuple[str, ...]) -> int:
        raise FileNotFoundError(2, "missing", argv[0])

    monkeypatch.setattr("automationctl.cli.subprocess.call", missing)
    assert (
        "cannot follow logs"
        in runner.invoke(app, ["logs", "task", "--follow"], env=isolated_env).output
    )


def test_logs_labels_both_streams_and_reports_fallback_and_other_stream(
    tmp_path: Path, runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    source = write_task(
        tmp_path / "task.toml",
        "task",
        command='["/bin/sh", "-c", "echo out; echo err >&2; exit 7"]',
    )
    assert install(runner, isolated_env, source).exit_code == 0
    assert runner.invoke(app, ["run", "task"], env=isolated_env).exit_code == 7
    both = runner.invoke(app, ["logs", "task", "--both"], env=isolated_env).output
    auto = runner.invoke(app, ["logs", "task"], env=isolated_env)
    assert "# " in both and "stdout.log" in both and "stderr.log" in both
    assert (
        "auto-selected: run failed" in auto.output and "also captured; use --stdout" in auto.output
    )


def test_status_and_logs_reject_unsafe_record_names(
    runner: CliRunner, isolated_env: dict[str, str]
) -> None:
    assert runner.invoke(app, ["status", "../../bad"], env=isolated_env).exit_code == 2
    assert runner.invoke(app, ["logs", "../../bad"], env=isolated_env).exit_code == 2


def test_prune_default_retains_fifty_runs() -> None:
    import inspect

    assert inspect.signature(prune).parameters["keep_runs"].default == 50


def test_legacy_interfaces_are_rejected_and_manifest_env_cwd_are_ignored(
    tmp_path: Path,
    runner: CliRunner,
    isolated_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_task(tmp_path / "task.toml", "task")
    legacy_env = {**isolated_env, "AUTOMATIONCTL_MANIFEST": "/does/not/exist"}
    monkeypatch.chdir(tmp_path)
    (tmp_path / "manifest.toml").write_text("not relevant = [", encoding="utf-8")
    assert runner.invoke(app, ["lint"], env=legacy_env).exit_code == 0
    assert runner.invoke(app, ["lint", "--host", "legacy"], env=legacy_env).exit_code == 2
    assert (
        runner.invoke(
            app, ["install", str(source), "--manifest", "manifest.toml"], env=legacy_env
        ).exit_code
        == 2
    )
    assert runner.invoke(app, ["uninstall", "task"], env=legacy_env).exit_code == 2
