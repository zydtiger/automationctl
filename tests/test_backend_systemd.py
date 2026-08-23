"""systemd unit rendering, reconciliation, and control verbs."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXED_EXECUTABLE, FIXED_MANIFEST, Tree

from automationctl.backends import CREATE, DELETE, UNCHANGED, UPDATE
from automationctl.backends.systemd import SystemdBackend
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.config import Automations

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


def test_activate_enables_only_scheduled_timers(
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
