"""systemd unit rendering, reconciliation, and control verbs."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXED_EXECUTABLE, FIXED_MANIFEST, Tree

from automationctl.backends import CREATE, DELETE, UNCHANGED, UPDATE
from automationctl.backends.systemd import (
    CATCHUP_BOOT_SEC,
    CATCHUP_SERVICE,
    CATCHUP_TIMER,
    CLOCK_TRIGGER_MIN_VERSION,
    SystemdBackend,
)
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.config import Automations
from automationctl.errors import BackendError

GOLDEN = Path(__file__).parent / "golden" / "systemd"

MANIFEST = """\
schema_version = 1

[defaults]
timeout = "10m"
randomized_delay = "5m"

[hosts.testhost]
tasks = [
  "calendar-task",
  "interval-task",
  "weekly-task",
  "manual-task",
  "raw-task",
  "off-task",
]
"""

TASKS = {
    "calendar-task": (
        'description = "Calendar task"\n'
        'command = ["/usr/bin/env", "true"]\n'
        'schedule = "daily 03:00"\n'
        'timeout = "45m"\n'
    ),
    "interval-task": (
        'description = "Interval task"\n'
        'command = ["/usr/bin/env", "true"]\n'
        'schedule = "every 15m"\n'
        'timeout = "2m"\n'
        'randomized_delay = "0s"\n'
    ),
    "weekly-task": (
        'description = "Weekly task"\n'
        'command = ["/usr/bin/env", "true"]\n'
        'schedule = "weekly sun 05:00"\n'
        "persistent = false\n"
    ),
    "manual-task": ('description = "Manual task"\ncommand = ["/usr/bin/env", "true"]\n'),
    "raw-task": (
        'description = "Escape hatch task"\n'
        'command = ["/usr/bin/env", "true"]\n\n'
        "[schedule]\n"
        'systemd = "Mon..Fri *-*-* 09:00:00"\n'
        "launchd = [{ Weekday = 1, Hour = 9, Minute = 0 }]\n"
    ),
    "off-task": (
        'description = "Disabled task"\n'
        'command = ["/usr/bin/env", "true"]\n'
        'schedule = "daily 01:00"\n'
        "disabled = true\n"
    ),
}


@pytest.fixture
def rendered(tree: Tree, tmp_path: Path) -> tuple[SystemdBackend, Automations, dict[str, str]]:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
        uid=1000,
    )
    return backend, automations, backend.desired_files(automations, automations.enabled_tasks())


def test_rendered_files_match_the_golden_set(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    assert sorted(files) == sorted(path.name for path in GOLDEN.iterdir())


def test_rendered_units_match_the_golden_files(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    for name, content in files.items():
        assert content == (GOLDEN / name).read_text(encoding="utf-8"), name


def test_disabled_tasks_render_nothing(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    assert not any("off-task" in name for name in files)


def test_manual_tasks_get_a_service_but_no_timer(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    assert "automationctl-manual-task.service" in files
    assert "automationctl-manual-task.timer" not in files


def test_escape_hatch_schedules_stay_persistent(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    """A per-backend calendar expression must not silently lose missed-run replay."""
    _, _, files = rendered
    timer = files["automationctl-raw-task.timer"]
    assert "OnCalendar=Mon..Fri *-*-* 09:00:00" in timer
    assert "Persistent=true" in timer


def test_interval_timers_use_the_documented_unit_form(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    timer = files["automationctl-interval-task.timer"]
    assert "OnUnitActiveSec=15m" in timer
    assert "OnBootSec=15m" in timer


def test_catchup_units_carry_the_clock_and_boot_triggers(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    """A jumped-past occurrence is only recoverable if something wakes catch-up."""
    _, _, files = rendered
    timer = files[CATCHUP_TIMER]
    assert "OnTimezoneChange=true" in timer
    assert "OnClockChange=true" in timer
    assert f"OnBootSec={CATCHUP_BOOT_SEC}" in timer
    assert "OnCalendar" not in timer
    service = files[CATCHUP_SERVICE]
    assert "ExecStart=/opt/bin/automationctl catch-up --manifest" in service
    assert "--host testhost" in service
    # Catch-up runs every missed task serially, so no invented aggregate bound.
    assert "TimeoutStartSec" not in service


def test_a_host_with_nothing_to_catch_up_renders_no_catchup_units(
    tree: Tree, tmp_path: Path
) -> None:
    """Interval and escape-hatch schedules give the triggers nothing to recover."""
    tree.write_manifest(
        "schema_version = 1\n\n[hosts.testhost]\n"
        'tasks = ["interval-task", "manual-task", "raw-task"]\n'
    )
    for name in ("interval-task", "manual-task", "raw-task"):
        tree.write_task(name, TASKS[name])
    automations = tree.load()
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
        uid=1000,
    )
    files = backend.desired_files(automations, automations.enabled_tasks())
    assert CATCHUP_SERVICE not in files
    assert CATCHUP_TIMER not in files
    backend.activate(automations, automations.enabled_tasks(), files)
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert not any(CATCHUP_TIMER in line for line in runner.transcript)


def test_a_non_persistent_calendar_task_does_not_want_catchup_units(
    tree: Tree, tmp_path: Path
) -> None:
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["weekly-task"]\n')
    tree.write_task("weekly-task", TASKS["weekly-task"])
    automations = tree.load()
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
        uid=1000,
    )
    assert CATCHUP_TIMER not in backend.desired_files(automations, automations.enabled_tasks())


def test_desired_files_never_lose_a_task_to_the_catchup_units(tree: Tree, tmp_path: Path) -> None:
    """The reserved name must refuse, not quietly overwrite the task."""
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["catchup"]\n')
    tree.write_task("catchup", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    automations = tree.load()
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
        uid=1000,
    )
    with pytest.raises(BackendError, match="reserved"):
        backend.desired_files(automations, automations.enabled_tasks())


def test_catchup_units_are_garbage_collected_like_any_other(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    """When no task wants them any more they are stale units, not orphans."""
    backend, _, files = rendered
    backend.unit_dir.mkdir(parents=True)
    backend.apply(backend.plan(files), files)
    assert (backend.unit_dir / CATCHUP_TIMER).exists()

    backend.runner = RecordingRunner()
    empty = backend.plan({})
    actions = {change.path.name: change.action for change in empty.changes}
    assert actions[CATCHUP_SERVICE] == DELETE
    assert actions[CATCHUP_TIMER] == DELETE
    backend.apply(empty, {})
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert f"systemctl --user disable --now {CATCHUP_TIMER}" in runner.transcript
    assert not (backend.unit_dir / CATCHUP_SERVICE).exists()


def test_plan_creates_updates_deletes_and_leaves_alone(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, files = rendered
    backend.unit_dir.mkdir(parents=True)
    name = "automationctl-calendar-task.service"
    (backend.unit_dir / name).write_text(files[name], encoding="utf-8")
    (backend.unit_dir / "automationctl-calendar-task.timer").write_text("stale\n", encoding="utf-8")
    (backend.unit_dir / "automationctl-gone.service").write_text("stale\n", encoding="utf-8")
    (backend.unit_dir / "hand-written.service").write_text("mine\n", encoding="utf-8")

    plan = backend.plan(files)
    actions = {change.path.name: change.action for change in plan.changes}
    assert actions[name] == UNCHANGED
    assert actions["automationctl-calendar-task.timer"] == UPDATE
    assert actions["automationctl-gone.service"] == DELETE
    assert actions["automationctl-manual-task.service"] == CREATE
    assert "hand-written.service" not in actions


def test_apply_writes_desired_and_removes_stale(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, files = rendered
    backend.unit_dir.mkdir(parents=True)
    (backend.unit_dir / "automationctl-gone.timer").write_text("stale\n", encoding="utf-8")
    (backend.unit_dir / "hand-written.service").write_text("mine\n", encoding="utf-8")

    plan = backend.plan(files)
    backend.apply(plan, files)

    on_disk = sorted(path.name for path in backend.unit_dir.iterdir())
    assert on_disk == sorted([*files, "hand-written.service"])
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert "systemctl --user disable --now automationctl-gone.timer" in runner.transcript


def test_activate_enables_scheduled_timers_and_the_catchup_timer(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    backend.activate(automations, automations.enabled_tasks())
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript == [
        "systemctl --user enable --now automationctl-calendar-task.timer",
        "systemctl --user enable --now automationctl-interval-task.timer",
        "systemctl --user enable --now automationctl-weekly-task.timer",
        "systemctl --user enable --now automationctl-raw-task.timer",
        "systemctl --user enable --now automationctl-catchup.timer",
    ]


def test_control_verbs_use_the_expected_commands(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    task = automations.tasks["calendar-task"]
    backend.submit(task)
    backend.pause(task)
    backend.resume(task)
    backend.reload()
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript == [
        "systemctl --user start automationctl-calendar-task.service",
        "systemctl --user disable --now automationctl-calendar-task.timer",
        "systemctl --user enable --now automationctl-calendar-task.timer",
        "systemctl --user daemon-reload",
    ]


def test_deactivate_disables_triggers_before_stopping_services(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, _ = rendered

    backend.deactivate(["automationctl-calendar-task.service", "automationctl-calendar-task.timer"])

    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript == [
        "systemctl --user disable --now automationctl-calendar-task.timer",
        "systemctl --user stop automationctl-calendar-task.service",
    ]


def test_enabled_reads_is_enabled_output(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    argv = ("systemctl", "--user", "is-enabled", "automationctl-calendar-task.timer")
    runner = RecordingRunner(responses={argv: CommandResult(argv, 0, stdout="enabled\n")})
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=runner,
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
    )
    assert backend.enabled(automations.tasks["calendar-task"]) is True
    assert backend.enabled(automations.tasks["manual-task"]) is None


def test_follow_argv_targets_journald(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    assert backend.follow_argv(automations.tasks["calendar-task"]) == (
        "journalctl",
        "--user",
        "-u",
        "automationctl-calendar-task.service",
        "-f",
    )


def version_response(text: str) -> dict[tuple[str, ...], CommandResult]:
    """Canned ``systemctl --version`` output, through the command seam."""
    argv: tuple[str, ...] = ("systemctl", "--user", "--version")
    return {argv: CommandResult(argv, 0, stdout=text)}


def catchup_checks(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> dict[str, tuple[bool, str]]:
    backend, automations, _ = rendered
    return {
        check.name: (check.ok, check.detail)
        for check in backend.catchup_health(automations, automations.enabled_tasks())
    }


def test_doctor_reports_missing_catchup_triggers(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, _ = rendered
    backend.runner = RecordingRunner(responses=version_response("systemd 257 (257.5-1)\n"))
    checks = catchup_checks(rendered)
    assert checks["catch-up triggers"][0] is False
    assert "is missing" in checks["catch-up triggers"][1]


def test_doctor_reports_installed_catchup_triggers(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, files = rendered
    backend.unit_dir.mkdir(parents=True)
    backend.apply(backend.plan(files), files)
    backend.runner = RecordingRunner(responses=version_response("systemd 257 (257.5-1)\n"))
    checks = catchup_checks(rendered)
    assert checks["catch-up triggers"][0] is True
    assert checks["clock triggers"] == (True, "systemd 257 supports OnTimezoneChange=")


def test_doctor_reports_a_systemd_too_old_for_timezone_triggers(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    """Below 242 the directive is ignored and only the boot backstop ever fires."""
    backend, _, _ = rendered
    old = CLOCK_TRIGGER_MIN_VERSION - 1
    backend.runner = RecordingRunner(responses=version_response(f"systemd {old} ({old}-1)\n"))
    ok, detail = catchup_checks(rendered)["clock triggers"]
    assert ok is False
    assert f"systemd {old} predates OnTimezoneChange=" in detail


def test_doctor_treats_an_unreadable_systemd_version_as_a_failure(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    """An unanswerable probe is not a pass for a trigger that fails silently."""
    backend, _, _ = rendered
    backend.runner = RecordingRunner(responses=version_response("who knows\n"))
    ok, detail = catchup_checks(rendered)["clock triggers"]
    assert ok is False
    assert "cannot determine the systemd version" in detail


def test_a_host_with_nothing_to_catch_up_reports_no_missing_trigger(
    tree: Tree, tmp_path: Path
) -> None:
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["manual-task"]\n')
    tree.write_task("manual-task", TASKS["manual-task"])
    automations = tree.load()
    runner = RecordingRunner()
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=runner,
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
        uid=1000,
    )
    checks = backend.catchup_health(automations, automations.enabled_tasks())
    assert [(check.name, check.ok) for check in checks] == [("catch-up triggers", True)]
    assert "not needed" in checks[0].detail
    # Nothing to probe means no probe: the version question does not arise.
    assert runner.calls == []


def test_health_probes_are_read_only(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, _ = rendered
    checks = backend.health()
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert [check.name for check in checks] == ["backend", "failed units", "linger"]
    for call in runner.calls:
        assert call[0] in {"systemctl", "loginctl"}
        assert not ({"start", "stop", "enable", "disable", "daemon-reload"} & set(call))


def test_doctor_reports_a_stale_catchup_timer(
    rendered: tuple[SystemdBackend, Automations, dict[str, str]],
) -> None:
    """Units left by an older tool version must not read as ok until reinstalled."""
    backend, _, files = rendered
    backend.unit_dir.mkdir(parents=True)
    backend.apply(backend.plan(files), files)
    timer = backend.unit_dir / CATCHUP_TIMER
    timer.write_text(
        timer.read_text(encoding="utf-8").replace("OnTimezoneChange=true\n", ""),
        encoding="utf-8",
    )
    backend.runner = RecordingRunner(responses=version_response("systemd 257 (257.5-1)\n"))
    checks = catchup_checks(rendered)
    assert checks["catch-up triggers"][0] is False
    assert "stale" in checks["catch-up triggers"][1]
