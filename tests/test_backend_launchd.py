"""launchd plist rendering, reconciliation, and control verbs."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from conftest import FIXED_EXECUTABLE, FIXED_MANIFEST, Tree
from test_backend_systemd import MANIFEST, TASKS

from automationctl.backends import DELETE, UNCHANGED
from automationctl.backends.launchd import CATCHUP_LABEL, LaunchdBackend
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.config import Automations

GOLDEN = Path(__file__).parent / "golden" / "launchd"


@pytest.fixture
def rendered(tree: Tree, tmp_path: Path) -> tuple[LaunchdBackend, Automations, dict[str, str]]:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    backend = LaunchdBackend(
        unit_dir=tmp_path / "agents",
        runner=RecordingRunner(),
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        uid=501,
    )
    return backend, automations, backend.desired_files(automations, automations.enabled_tasks())


def test_rendered_files_match_the_golden_set(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    assert sorted(files) == sorted(path.name for path in GOLDEN.iterdir())


def test_rendered_plists_match_the_golden_files(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    for name, content in files.items():
        assert content == (GOLDEN / name).read_text(encoding="utf-8"), name


def test_calendar_plist_carries_start_calendar_interval(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    data = plistlib.loads(files["automationctl.calendar-task.plist"].encode("utf-8"))
    assert data["Label"] == "automationctl.calendar-task"
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}
    assert data["RunAtLoad"] is False
    assert data["ProgramArguments"] == [
        FIXED_EXECUTABLE,
        "exec",
        "--manifest",
        str(FIXED_MANIFEST),
        "--jitter",
        "calendar-task",
    ]


def test_interval_plist_uses_start_interval_and_no_jitter(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    data = plistlib.loads(files["automationctl.interval-task.plist"].encode("utf-8"))
    assert data["StartInterval"] == 900
    assert "--jitter" not in data["ProgramArguments"]


def test_manual_plist_has_no_start_condition(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    data = plistlib.loads(files["automationctl.manual-task.plist"].encode("utf-8"))
    assert "StartCalendarInterval" not in data
    assert "StartInterval" not in data


def test_catchup_agent_runs_at_load(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    _, _, files = rendered
    data = plistlib.loads(files[f"{CATCHUP_LABEL}.plist"].encode("utf-8"))
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"][1] == "catch-up"


def test_plan_garbage_collects_stale_agents(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, files = rendered
    backend.unit_dir.mkdir(parents=True)
    name = "automationctl.calendar-task.plist"
    (backend.unit_dir / name).write_text(files[name], encoding="utf-8")
    (backend.unit_dir / "automationctl.gone.plist").write_text("stale\n", encoding="utf-8")
    (backend.unit_dir / "com.example.other.plist").write_text("mine\n", encoding="utf-8")

    plan = backend.plan(files)
    actions = {change.path.name: change.action for change in plan.changes}
    assert actions[name] == UNCHANGED
    assert actions["automationctl.gone.plist"] == DELETE
    assert "com.example.other.plist" not in actions

    backend.apply(plan, files)
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert "launchctl bootout gui/501/automationctl.gone" in runner.transcript
    assert (backend.unit_dir / "com.example.other.plist").exists()


def test_activate_reloads_every_agent_including_catchup(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    backend.activate(automations, automations.enabled_tasks())
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript[0] == "launchctl bootout gui/501/automationctl.calendar-task"
    assert runner.transcript[1] == (
        f"launchctl bootstrap gui/501 {backend.unit_dir}/automationctl.calendar-task.plist"
    )
    assert runner.transcript[-1].endswith("automationctl.catchup.plist")


def test_control_verbs_use_the_expected_commands(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    task = automations.tasks["calendar-task"]
    backend.submit(task)
    backend.pause(task)
    backend.resume(task)
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript == [
        "launchctl kickstart -k gui/501/automationctl.calendar-task",
        "launchctl disable gui/501/automationctl.calendar-task",
        "launchctl bootout gui/501/automationctl.calendar-task",
        "launchctl enable gui/501/automationctl.calendar-task",
        f"launchctl bootstrap gui/501 {backend.unit_dir}/automationctl.calendar-task.plist",
    ]


def test_enabled_reflects_launchctl_print(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    argv = ("launchctl", "print", "gui/501/automationctl.calendar-task")
    runner = RecordingRunner(responses={argv: CommandResult(argv, 113, stderr="not found")})
    backend = LaunchdBackend(
        unit_dir=tmp_path / "agents",
        runner=runner,
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        uid=501,
    )
    assert backend.enabled(automations.tasks["calendar-task"]) is False


def test_launchd_cannot_follow_live_logs(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    assert backend.follow_argv(automations.tasks["calendar-task"]) is None
