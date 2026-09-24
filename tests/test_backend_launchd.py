"""launchd lifecycle controls retain per-label acceptance state."""

from __future__ import annotations

from pathlib import Path

from automationctl import records
from automationctl.backends.launchd import CATCHUP_LABEL, LaunchdBackend, label_for, plist_name
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.schedule import parse
from automationctl.spec import MachinePolicy, TaskSpec


def task(tmp_path: Path, name: str = "check", schedule: str | None = "daily 03:00") -> TaskSpec:
    return TaskSpec(
        name,
        tmp_path / f"{name}.toml",
        name,
        ("/bin/true",),
        schedule=parse(schedule) if schedule else None,
    )


def backend(tmp_path: Path, runner: RecordingRunner) -> LaunchdBackend:
    return LaunchdBackend(
        unit_dir=tmp_path / "agents",
        runner=runner,
        executable="actl",
        config_home=tmp_path / "config space%",
        state_home=tmp_path / "state space",
        state_dir=tmp_path / "state space" / "automationctl",
        uid=501,
    )


def absent(label: str) -> dict[tuple[str, ...], CommandResult]:
    argv = ("launchctl", "print", f"gui/501/{label}")
    return {
        argv: CommandResult(argv, 113, stderr="not found"),
        ("launchctl", "print", "gui/501"): CommandResult(("launchctl", "print", "gui/501"), 0),
    }


def test_rendering_has_schedule_xdg_and_no_manifest(tmp_path: Path) -> None:
    files = backend(tmp_path, RecordingRunner()).desired_task_files(task(tmp_path))
    text = files["automationctl.check.plist"]
    assert (
        "StartCalendarInterval" in text and "XDG_CONFIG_HOME" in text and "--manifest" not in text
    )
    catchup = backend(tmp_path, RecordingRunner()).desired_catchup_files(
        MachinePolicy(tmp_path / "policy", catchup_sweep_seconds=3600)
    )
    assert (
        "WatchPaths" in catchup[plist_name(CATCHUP_LABEL)]
        and "StartInterval" in catchup[plist_name(CATCHUP_LABEL)]
    )


def test_healthy_label_is_not_restarted_and_activation_state_merges(tmp_path: Path) -> None:
    recorder = RecordingRunner()
    scheduler = backend(tmp_path, recorder)
    first = task(tmp_path, "first")
    second = task(tmp_path, "second")
    desired = {**scheduler.desired_task_files(first), **scheduler.desired_task_files(second)}
    scheduler.activate(first, desired, set())
    scheduler.activate(second, desired, set())
    activation = records.read_activation(scheduler.state_dir, "launchd")
    assert set(activation) == {label_for("first"), label_for("second")}
    next_runner = RecordingRunner()
    scheduler.runner = next_runner
    scheduler.activate(first, desired, set())
    assert not any("bootout" in x or "bootstrap" in x for x in next_runner.transcript)


def test_rewritten_or_unknown_label_is_reloaded_but_only_that_label(tmp_path: Path) -> None:
    scheduler = backend(tmp_path, RecordingRunner())
    first = task(tmp_path, "first")
    second = task(tmp_path, "second")
    desired = {**scheduler.desired_task_files(first), **scheduler.desired_task_files(second)}
    scheduler.activate(first, desired, set())
    scheduler.activate(second, desired, set())
    recorder = RecordingRunner()
    scheduler.runner = recorder
    scheduler.activate(first, desired, {plist_name(label_for("first"))})
    lines = [x for x in recorder.transcript if "bootout" in x or "bootstrap" in x]
    assert lines == [
        f"launchctl bootout gui/501/{label_for('first')}",
        f"launchctl bootstrap gui/501 {scheduler.unit_dir}/{plist_name(label_for('first'))}",
    ]


def test_failed_bootstrap_is_retried_and_does_not_claim_acceptance(tmp_path: Path) -> None:
    label = label_for("check")
    scheduler = backend(tmp_path, RecordingRunner(responses=absent(label)))
    spec = task(tmp_path)
    desired = scheduler.desired_task_files(spec)
    argv = ("launchctl", "bootstrap", "gui/501", str(scheduler.unit_dir / plist_name(label)))
    scheduler.runner = RecordingRunner(
        responses={**absent(label), argv: CommandResult(argv, 1, stderr="refused")}
    )
    assert scheduler.activate(spec, desired, set())[
        -1
    ].ok is False and label not in records.read_activation(scheduler.state_dir, "launchd")
    scheduler.runner = RecordingRunner(responses=absent(label))
    scheduler.activate(spec, desired, set())
    assert label in records.read_activation(scheduler.state_dir, "launchd")


def test_deactivate_unknown_or_failed_keeps_correct_acceptance_state(tmp_path: Path) -> None:
    scheduler = backend(tmp_path, RecordingRunner())
    spec = task(tmp_path)
    desired = scheduler.desired_task_files(spec)
    scheduler.activate(spec, desired, set())
    label = label_for("check")
    unknown = ("launchctl", "print", f"gui/501/{label}")
    scheduler.runner = RecordingRunner(responses={unknown: CommandResult(unknown, 5, stderr="bad")})
    assert scheduler.deactivate((plist_name(label),))[0].argv == (
        "launchctl",
        "bootout",
        f"gui/501/{label}",
    )
    scheduler.activate(spec, desired, set())
    scheduler.runner = RecordingRunner(
        responses={
            unknown: CommandResult(unknown, 5),
            ("launchctl", "bootout", f"gui/501/{label}"): CommandResult(
                ("launchctl", "bootout", f"gui/501/{label}"), 1
            ),
        }
    )
    scheduler.deactivate((plist_name(label),))
    assert label in records.read_activation(scheduler.state_dir, "launchd")


def test_pause_resume_are_idempotent_and_stop_on_refusal(tmp_path: Path) -> None:
    spec = task(tmp_path)
    label = label_for("check")
    absent_runner = RecordingRunner(responses=absent(label))
    scheduler = backend(tmp_path, absent_runner)
    scheduler.pause(spec)
    assert not any("bootout" in x for x in absent_runner.transcript)
    refused = ("launchctl", "disable", f"gui/501/{label}")
    refused_runner = RecordingRunner(responses={refused: CommandResult(refused, 1)})
    scheduler.runner = refused_runner
    assert scheduler.pause(spec)[0].ok is False and refused_runner.transcript == [
        f"launchctl disable gui/501/{label}"
    ]
    loaded_runner = RecordingRunner()
    scheduler.runner = loaded_runner
    scheduler.resume(spec)
    assert loaded_runner.transcript == [
        f"launchctl enable gui/501/{label}",
        f"launchctl print gui/501/{label}",
    ]
    resume_runner = RecordingRunner(responses=absent(label))
    scheduler.runner = resume_runner
    scheduler.resume(spec)
    assert any("bootstrap" in x for x in resume_runner.transcript)


def test_enabled_and_follow_reflect_launchd_capabilities(tmp_path: Path) -> None:
    spec = task(tmp_path)
    label = label_for("check")
    scheduler = backend(tmp_path, RecordingRunner(responses=absent(label)))
    assert scheduler.enabled(spec) is False and scheduler.follow_argv(spec) is None
    unknown = ("launchctl", "print", f"gui/501/{label}")
    scheduler.runner = RecordingRunner(responses={unknown: CommandResult(unknown, 1)})
    assert scheduler.enabled(spec) is None


GOLDEN = Path(__file__).parent / "golden" / "launchd"
SWEEP_GOLDEN = Path(__file__).parent / "golden" / "launchd-sweep"
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


def golden_backend() -> LaunchdBackend:
    return LaunchdBackend(
        unit_dir=GOLDEN_ROOT / "units",
        runner=RecordingRunner(),
        executable="/opt/bin/automationctl",
        config_home=GOLDEN_ROOT / "config home%",
        state_home=GOLDEN_ROOT / "state home",
        state_dir=GOLDEN_ROOT / "state home" / "automationctl",
        uid=501,
    )


def test_rendered_schema_v2_artifacts_match_launchd_goldens() -> None:
    scheduler = golden_backend()
    rendered: dict[str, str] = {}
    for spec in golden_tasks():
        rendered.update(scheduler.desired_task_files(spec))
    rendered.update(scheduler.desired_catchup_files(MachinePolicy(GOLDEN_ROOT / "config.toml")))
    assert sorted(rendered) == sorted(path.name for path in GOLDEN.iterdir())
    for name, content in rendered.items():
        assert content == (GOLDEN / name).read_text(encoding="utf-8"), name
    sweep = scheduler.desired_catchup_files(
        MachinePolicy(GOLDEN_ROOT / "config.toml", catchup_sweep_seconds=21600)
    )
    assert sorted(sweep) == sorted(path.name for path in SWEEP_GOLDEN.iterdir())
    for name, content in sweep.items():
        assert content == (SWEEP_GOLDEN / name).read_text(encoding="utf-8"), name
