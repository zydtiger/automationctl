"""launchd plist rendering, reconciliation, and control verbs."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from conftest import FIXED_EXECUTABLE, FIXED_MANIFEST, FailingRunner, Tree
from test_backend_systemd import MANIFEST, TASKS

from automationctl import records
from automationctl.backends import DELETE, UNCHANGED
from automationctl.backends.launchd import CATCHUP_LABEL, LaunchdBackend
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.config import Automations
from automationctl.errors import BackendError

GOLDEN = Path(__file__).parent / "golden" / "launchd"


def make_backend(
    tree: Tree, tmp_path: Path, runner: RecordingRunner | None = None
) -> LaunchdBackend:
    return LaunchdBackend(
        unit_dir=tmp_path / "agents",
        runner=runner if runner is not None else RecordingRunner(),
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
        uid=501,
    )


def not_loaded(*labels: str) -> dict[tuple[str, ...], CommandResult]:
    """Canned probe replies: these labels are definitely not bootstrapped."""
    responses: dict[tuple[str, ...], CommandResult] = {}
    for label in labels:
        argv: tuple[str, ...] = ("launchctl", "print", f"gui/501/{label}")
        responses[argv] = CommandResult(argv, 113, stderr="Could not find service")
    return responses


@pytest.fixture
def rendered(tree: Tree, tmp_path: Path) -> tuple[LaunchdBackend, Automations, dict[str, str]]:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    backend = make_backend(tree, tmp_path)
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


def test_activate_leaves_already_activated_agents_running(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """A no-op install must not bootout agents, which would kill running jobs."""
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    backend.activate(automations, tasks, files)  # first install records the hashes

    second = RecordingRunner()
    backend.runner = second
    backend.activate(automations, tasks, files)

    assert not any("bootout" in line for line in second.transcript)
    assert not any("bootstrap" in line for line in second.transcript)
    assert "launchctl enable gui/501/automationctl.calendar-task" in second.transcript
    assert "launchctl enable gui/501/automationctl.catchup" in second.transcript


def test_activate_reloads_only_the_agent_whose_definition_changed(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    backend.activate(automations, tasks, files)

    revised = dict(files)
    revised["automationctl.calendar-task.plist"] += "\n<!-- edited -->\n"
    second = RecordingRunner()
    backend.runner = second
    backend.activate(automations, tasks, revised)

    booted_out = [line for line in second.transcript if "bootout" in line]
    assert booted_out == ["launchctl bootout gui/501/automationctl.calendar-task"]
    assert (
        f"launchctl bootstrap gui/501 {backend.unit_dir}/automationctl.calendar-task.plist"
        in second.transcript
    )


def test_activate_retries_a_label_whose_previous_activation_failed(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """An install that wrote a plist but failed to load it must not look done."""
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    label = "automationctl.calendar-task"
    bootstrap = ("launchctl", "bootstrap", "gui/501", f"{backend.unit_dir}/{label}.plist")

    first = RecordingRunner(
        responses={
            **not_loaded(label),
            bootstrap: CommandResult(bootstrap, 5, stderr="input/output error"),
        }
    )
    backend.runner = first
    backend.activate(automations, tasks, files)
    assert any("bootstrap" in line and label in line for line in first.transcript)

    # Nothing recorded for the label that failed, so the next install retries it.
    activated = records.read_activation(backend.state_dir, "launchd")
    assert label not in activated
    assert "automationctl.catchup" in activated

    second = RecordingRunner(responses=not_loaded(label))
    backend.runner = second
    backend.activate(automations, tasks, files)
    assert f"launchctl bootstrap gui/501 {backend.unit_dir}/{label}.plist" in second.transcript
    assert label in records.read_activation(backend.state_dir, "launchd")


def test_activate_enables_before_bootstrap_so_install_restores_a_pause(
    tree: Tree, tmp_path: Path
) -> None:
    """`pause` leaves a persistent disable override; `install` has to clear it."""
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    label = "automationctl.calendar-task"
    runner = RecordingRunner(responses=not_loaded(label))
    backend = make_backend(tree, tmp_path, runner)
    backend.activate(automations, [automations.tasks["calendar-task"]])

    relevant = [line for line in runner.transcript if label in line and "print" not in line]
    assert relevant == [
        f"launchctl enable gui/501/{label}",
        f"launchctl bootstrap gui/501 {backend.unit_dir}/{label}.plist",
    ]


def test_desired_files_never_lose_a_task_to_the_catchup_agent(tree: Tree, tmp_path: Path) -> None:
    """The reserved label must refuse, not quietly overwrite the task."""
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["catchup"]\n')
    tree.write_task("catchup", 'description = "d"\ncommand = ["/bin/true"]\n')
    automations = tree.load()
    backend = make_backend(tree, tmp_path)
    with pytest.raises(BackendError, match="reserved"):
        backend.desired_files(automations, automations.enabled_tasks())


def test_deactivate_boots_out_when_the_probe_cannot_answer(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """An unreachable launchctl means "unknown", never "nothing to stop"."""
    backend, _, _ = rendered
    backend.runner = FailingRunner()
    results = backend.deactivate(["automationctl.calendar-task.plist"])

    assert [result.argv for result in results] == [
        ("launchctl", "bootout", "gui/501/automationctl.calendar-task")
    ]
    assert not any(result.ok for result in results)


def test_deactivate_skips_only_a_definitely_absent_agent(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, _ = rendered
    label = "automationctl.calendar-task"
    runner = RecordingRunner(responses=not_loaded(label))
    backend.runner = runner
    assert backend.deactivate([f"{label}.plist"]) == []
    assert not any("bootout" in line for line in runner.transcript)


def test_deactivate_forgets_the_activation_record(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    backend.activate(automations, tasks, files)
    assert "automationctl.calendar-task" in records.read_activation(backend.state_dir, "launchd")

    backend.deactivate(["automationctl.calendar-task.plist"])
    activated = records.read_activation(backend.state_dir, "launchd")
    assert "automationctl.calendar-task" not in activated
    assert "automationctl.catchup" in activated


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
    label = "automationctl.calendar-task"
    backend = make_backend(tree, tmp_path, RecordingRunner(responses=not_loaded(label)))
    assert backend.enabled(automations.tasks["calendar-task"]) is False


def test_enabled_is_unknown_when_launchctl_cannot_answer(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    backend = make_backend(tree, tmp_path, FailingRunner())
    assert backend.enabled(automations.tasks["calendar-task"]) is None


def test_launchd_cannot_follow_live_logs(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    assert backend.follow_argv(automations.tasks["calendar-task"]) is None
