from __future__ import annotations

from pathlib import Path

from automationctl.backends.launchd import LaunchdBackend, plist_name
from automationctl.backends.systemd import CATCHUP_TIMER, SystemdBackend
from automationctl.commands import RecordingRunner
from automationctl.spec import MachinePolicy, TaskSpec


def task(tmp_path: Path, name: str, schedule: str | None = None) -> TaskSpec:
    from automationctl.schedule import parse

    return TaskSpec(
        name=name,
        path=tmp_path / f"{name}.toml",
        description=name,
        command=("/bin/true",),
        schedule=parse(schedule) if schedule else None,
    )


def test_systemd_generated_environment_is_explicit_and_escaped(tmp_path: Path) -> None:
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable="/bin/automationctl",
        config_home=tmp_path / "config % home",
        state_home=tmp_path / "state home",
        state_dir=tmp_path / "state home" / "automationctl",
    )
    rendered = backend.desired_task_files(task(tmp_path, "one", "daily 02:00"))
    service = rendered["automationctl-one.service"]
    assert "XDG_CONFIG_HOME=" in service and "%%" in service
    assert "XDG_STATE_HOME=" in service


def test_launchd_activation_retries_failure_without_restarting_healthy_agent(
    tmp_path: Path,
) -> None:
    recorder = RecordingRunner()
    backend = LaunchdBackend(
        unit_dir=tmp_path / "units",
        runner=recorder,
        executable="automationctl",
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
        state_dir=tmp_path / "state" / "automationctl",
        uid=501,
    )
    source = task(tmp_path, "one", "daily 02:00")
    desired = backend.desired_task_files(source)
    filename = plist_name("automationctl.one")
    backend.unit_dir.mkdir()
    (backend.unit_dir / filename).write_text(desired[filename], encoding="utf-8")
    backend.activate(source, desired, {filename})
    first_bootstraps = [call for call in recorder.calls if "bootstrap" in call]
    backend.activate(source, desired, set())
    assert [call for call in recorder.calls if "bootstrap" in call] == first_bootstraps


def test_catchup_artifacts_are_removed_after_last_persistent_task(tmp_path: Path) -> None:
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable="automationctl",
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
        state_dir=tmp_path / "state" / "automationctl",
    )
    policy = MachinePolicy(tmp_path / "config.toml")
    desired = backend.desired_catchup_files(policy)
    assert CATCHUP_TIMER in desired
