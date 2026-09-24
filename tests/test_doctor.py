"""Doctor preserves read-only runtime diagnostics."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from automationctl.backends.systemd import SystemdBackend
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.doctor import run
from automationctl.spec import MachinePolicy, TaskSpec


def backend(tmp_path: Path) -> SystemdBackend:
    runner = RecordingRunner(
        responses={
            ("systemctl", "--user", "is-system-running"): CommandResult(
                ("systemctl", "--user", "is-system-running"), 0, stdout="running\n"
            ),
            (
                "systemctl",
                "--user",
                "list-units",
                "--state=failed",
                "--no-legend",
                "automationctl-*",
            ): CommandResult(
                (
                    "systemctl",
                    "--user",
                    "list-units",
                    "--state=failed",
                    "--no-legend",
                    "automationctl-*",
                ),
                0,
            ),
            ("loginctl", "show-user", "501", "--property=Linger", "--value"): CommandResult(
                ("loginctl", "show-user", "501", "--property=Linger", "--value"), 0, stdout="yes\n"
            ),
        }
    )
    return SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=runner,
        executable="actl",
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
        state_dir=tmp_path / "state" / "automationctl",
        uid=501,
    )


def test_absent_creatable_state_and_home_cwd_are_healthy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    task = TaskSpec("check", tmp_path / "check.toml", "check", ("/bin/true",))
    report = run(
        [task],
        [],
        backend(tmp_path),
        policy=MachinePolicy(tmp_path / "config.toml"),
        state_dir=tmp_path / "state" / "automationctl",
        env={"HOME": str(home)},
    )
    assert report.ok


def test_file_at_state_path_and_nonexecutable_binary_fail(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.write_text("not a directory", encoding="utf-8")
    binary = tmp_path / "binary"
    binary.write_text("x", encoding="utf-8")
    task = TaskSpec("check", tmp_path / "check.toml", "check", (str(binary),))
    report = run(
        [task],
        [],
        backend(tmp_path),
        policy=MachinePolicy(tmp_path / "config.toml"),
        state_dir=state,
        env={"HOME": str(tmp_path)},
    )
    assert not report.ok


def test_existing_state_dir_requires_writable_and_searchable_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from automationctl.doctor import _state_dir_check

    state = tmp_path / "state"
    state.mkdir()
    original_access = os.access

    def denied(path: Path | str, mode: int) -> bool:
        return False if Path(path) == state else original_access(path, mode)

    monkeypatch.setattr("automationctl.doctor.os.access", denied)
    check = _state_dir_check(state)
    assert check.ok is False and "not a writable directory" in check.detail


def test_binary_check_uses_wrapper_placeholder_and_tilde_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    task = TaskSpec("check", tmp_path / "check.toml", "check", ("~/{task}",))
    monkeypatch.setattr("automationctl.template.os.path.expanduser", lambda value: "/bin/true")
    report = run(
        [task],
        [],
        backend(tmp_path),
        policy=MachinePolicy(tmp_path / "config.toml"),
        state_dir=tmp_path / "state" / "automationctl",
        env={"HOME": str(home)},
    )
    assert any(
        check.name == "binary" and "/bin/true -> /bin/true" in check.detail
        for check in report.checks
    )
