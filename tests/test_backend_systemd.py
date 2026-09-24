"""Scoped systemd artifacts and control ordering."""

from __future__ import annotations

from pathlib import Path

from automationctl.backends.systemd import SystemdBackend
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.schedule import parse
from automationctl.spec import MachinePolicy, TaskSpec


def task(tmp_path: Path, name: str, schedule: str | None = None) -> TaskSpec:
    return TaskSpec(
        name,
        tmp_path / f"{name}.toml",
        name,
        ("/bin/true",),
        schedule=parse(schedule) if schedule else None,
    )


def backend(tmp_path: Path, runner: RecordingRunner) -> SystemdBackend:
    return SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=runner,
        executable="actl",
        config_home=tmp_path / "config space",
        state_home=tmp_path / "state",
        state_dir=tmp_path / "state" / "automationctl",
    )


def test_rendering_passes_xdg_and_uses_exec_name_only(tmp_path: Path) -> None:
    result = backend(tmp_path, RecordingRunner()).desired_task_files(
        task(tmp_path, "check", "daily 03:00")
    )
    assert "actl exec check" in result["automationctl-check.service"]
    assert "--manifest" not in result["automationctl-check.service"]
    assert "XDG_CONFIG_HOME" in result["automationctl-check.service"]
    assert "Persistent=true" in result["automationctl-check.timer"]


def test_scoped_plan_does_not_see_another_task(tmp_path: Path) -> None:
    scheduler = backend(tmp_path, RecordingRunner())
    scheduler.unit_dir.mkdir()
    (scheduler.unit_dir / "automationctl-other.service").write_text("other", encoding="utf-8")
    desired = scheduler.desired_task_files(task(tmp_path, "check"))
    plan = scheduler.plan(desired, scheduler.possible_task_filenames("check"))
    assert all("other" not in change.path.name for change in plan.changes)


def test_remove_disables_timer_before_stopping_service(tmp_path: Path) -> None:
    recorder = RecordingRunner()
    scheduler = backend(tmp_path, recorder)
    scheduler.deactivate(("automationctl-check.timer", "automationctl-check.service"))
    assert recorder.calls[0][-2:] == ("--now", "automationctl-check.timer")
    assert recorder.calls[1][-2:] == ("stop", "automationctl-check.service")


GOLDEN = Path(__file__).parent / "golden" / "systemd"
GOLDEN_ROOT = Path("/var/tmp/automationctl-fixture")


def golden_tasks() -> tuple[TaskSpec, ...]:
    def make(
        name: str, schedule: str | dict[str, object] | None = None, **kwargs: object
    ) -> TaskSpec:
        return TaskSpec(
            name,
            GOLDEN_ROOT / f"{name}.toml",
            f"{name} task",
            ("/usr/bin/env", "true"),
            schedule=parse(schedule) if schedule else None,
            **kwargs,  # type: ignore[arg-type]
        )

    return (
        make("calendar-task", "daily 03:00", timeout_seconds=2700, jitter_seconds=300),
        make("interval-task", "every 15m", timeout_seconds=120),
        make("weekly-task", "weekly sun 05:00", persistent=False),
        make("manual-task"),
        make(
            "raw-task",
            {
                "systemd": "Mon..Fri *-*-* 09:00:00",
                "launchd": [{"Weekday": 1, "Hour": 9, "Minute": 0}],
            },
        ),
    )


def golden_backend() -> SystemdBackend:
    return SystemdBackend(
        unit_dir=GOLDEN_ROOT / "units",
        runner=RecordingRunner(),
        executable="/opt/bin/automationctl",
        config_home=GOLDEN_ROOT / "config home%",
        state_home=GOLDEN_ROOT / "state home",
        state_dir=GOLDEN_ROOT / "state home" / "automationctl",
    )


def test_rendered_schema_v2_artifacts_match_systemd_goldens() -> None:
    scheduler = golden_backend()
    rendered: dict[str, str] = {}
    for spec in golden_tasks():
        rendered.update(scheduler.desired_task_files(spec))
    rendered.update(scheduler.desired_catchup_files(MachinePolicy(GOLDEN_ROOT / "config.toml")))
    assert sorted(rendered) == sorted(path.name for path in GOLDEN.iterdir())
    for name, content in rendered.items():
        assert content == (GOLDEN / name).read_text(encoding="utf-8"), name


def test_shared_catchup_reenables_after_last_task_removal_and_readd(tmp_path: Path) -> None:
    from automationctl import records
    from automationctl.backends.systemd import CATCHUP_SERVICE, CATCHUP_TIMER

    recorder = RecordingRunner()
    scheduler = backend(tmp_path, recorder)
    desired = scheduler.desired_catchup_files(MachinePolicy(tmp_path / "policy"))
    records.write_activation(scheduler.state_dir, "systemd", {"other": "accepted"})
    scheduler.activate_catchup(desired, {CATCHUP_SERVICE, CATCHUP_TIMER})
    assert CATCHUP_TIMER in records.read_activation(scheduler.state_dir, "systemd")
    scheduler.deactivate((CATCHUP_TIMER, CATCHUP_SERVICE))
    activation = records.read_activation(scheduler.state_dir, "systemd")
    assert CATCHUP_TIMER not in activation and activation["other"] == "accepted"
    scheduler.activate_catchup(desired, {CATCHUP_SERVICE, CATCHUP_TIMER})
    assert recorder.transcript.count(f"systemctl --user enable --now {CATCHUP_TIMER}") == 2


def test_failed_shared_catchup_activation_is_not_accepted_and_retries(tmp_path: Path) -> None:
    from automationctl import records
    from automationctl.backends.systemd import CATCHUP_TIMER

    failed_argv = ("systemctl", "--user", "enable", "--now", CATCHUP_TIMER)
    scheduler = backend(
        tmp_path,
        RecordingRunner(responses={failed_argv: CommandResult(failed_argv, 1, stderr="refused")}),
    )
    desired = scheduler.desired_catchup_files(MachinePolicy(tmp_path / "policy"))
    assert scheduler.activate_catchup(desired, set())[0].ok is False
    assert CATCHUP_TIMER not in records.read_activation(scheduler.state_dir, "systemd")
    retry = RecordingRunner()
    scheduler.runner = retry
    scheduler.activate_catchup(desired, set())
    assert f"systemctl --user enable --now {CATCHUP_TIMER}" in retry.transcript


def test_systemd_catchup_sweep_policy_is_a_noop(tmp_path: Path) -> None:
    scheduler = backend(tmp_path, RecordingRunner())
    assert scheduler.desired_catchup_files(
        MachinePolicy(tmp_path / "policy")
    ) == scheduler.desired_catchup_files(
        MachinePolicy(tmp_path / "policy", catchup_sweep_seconds=3600)
    )


def test_systemd_health_and_catchup_probes_are_read_only(tmp_path: Path) -> None:
    from automationctl.backends.systemd import CLOCK_TRIGGER_MIN_VERSION

    task_spec = task(tmp_path, "check", "daily 03:00")
    probe = RecordingRunner(
        responses={
            ("systemctl", "--user", "is-system-running"): CommandResult(
                ("systemctl", "--user", "is-system-running"), 1, stdout="degraded\n"
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
                stdout="automationctl-check.service failed\n",
            ),
            ("loginctl", "show-user", "501", "--property=Linger", "--value"): CommandResult(
                ("loginctl", "show-user", "501", "--property=Linger", "--value"), 0, stdout="no\n"
            ),
            ("systemctl", "--user", "--version"): CommandResult(
                ("systemctl", "--user", "--version"),
                0,
                stdout=f"systemd {CLOCK_TRIGGER_MIN_VERSION}\n",
            ),
        }
    )
    scheduler = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=probe,
        executable="actl",
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
        state_dir=tmp_path / "state" / "automationctl",
        uid=501,
    )
    desired = scheduler.desired_catchup_files(MachinePolicy(tmp_path / "policy"))
    scheduler.unit_dir.mkdir()
    for name, content in desired.items():
        (scheduler.unit_dir / name).write_text(content, encoding="utf-8")
    checks = {
        check.name: check
        for check in [
            *scheduler.health(),
            *scheduler.catchup_health([task_spec], MachinePolicy(tmp_path / "policy")),
        ]
    }
    assert (
        checks["backend"].ok and checks["failed units"].ok is False and checks["linger"].ok is False
    )
    assert checks["catch-up triggers"].ok and checks["clock triggers"].ok
    assert not any(
        {"start", "stop", "enable", "disable", "daemon-reload"} & set(call) for call in probe.calls
    )


def test_systemd_catchup_health_rejects_old_or_unknown_manager_version(tmp_path: Path) -> None:
    task_spec = task(tmp_path, "check", "daily 03:00")
    for output, expected in (("systemd 241\n", "predates"), ("unknown\n", "cannot determine")):
        runner = RecordingRunner(
            responses={
                ("systemctl", "--user", "--version"): CommandResult(
                    ("systemctl", "--user", "--version"), 0, stdout=output
                )
            }
        )
        scheduler = backend(tmp_path, runner)
        desired = scheduler.desired_catchup_files(MachinePolicy(tmp_path / "policy"))
        scheduler.unit_dir.mkdir(exist_ok=True)
        for name, content in desired.items():
            (scheduler.unit_dir / name).write_text(content, encoding="utf-8")
        checks = {
            check.name: check
            for check in scheduler.catchup_health([task_spec], MachinePolicy(tmp_path / "policy"))
        }
        assert checks["clock triggers"].ok is False and expected in checks["clock triggers"].detail


def test_environment_keeps_literal_dollars_while_execstart_escapes_them(tmp_path: Path) -> None:
    scheduler = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable="actl",
        config_home=tmp_path / "config $literal",
        state_home=tmp_path / "state $literal",
        state_dir=tmp_path / "state" / "automationctl",
    )
    service = scheduler.desired_task_files(task(tmp_path, "check"))["automationctl-check.service"]
    assert "XDG_CONFIG_HOME=" in service and "config $literal" in service
    assert "config $$literal" not in service and "state $$literal" not in service
