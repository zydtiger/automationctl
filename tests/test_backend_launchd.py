"""launchd plist rendering, reconciliation, and control verbs."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from conftest import FIXED_EXECUTABLE, FIXED_MANIFEST, FailingRunner, Tree
from test_backend_systemd import MANIFEST, TASKS

from automationctl import records
from automationctl.backends import DELETE, UNCHANGED
from automationctl.backends.launchd import CATCHUP_LABEL, LOCALTIME_PATH, LaunchdBackend
from automationctl.commands import CommandResult, RecordingRunner
from automationctl.config import Automations
from automationctl.errors import BackendError

GOLDEN = Path(__file__).parent / "golden" / "launchd"
#: The catch-up agent as rendered with the optional sweep configured. It lives
#: outside GOLDEN because that directory is compared as a complete rendered set.
SWEEP_GOLDEN = Path(__file__).parent / "golden" / "launchd-sweep"


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
        "--host",
        "testhost",
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


def test_catchup_agent_runs_at_load_and_watches_the_timezone(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """A timezone change re-points /etc/localtime; WatchPaths is the only signal."""
    _, _, files = rendered
    data = plistlib.loads(files[f"{CATCHUP_LABEL}.plist"].encode("utf-8"))
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"][1] == "catch-up"
    assert data["ProgramArguments"][-2:] == ["--host", "testhost"]
    assert data["WatchPaths"] == [LOCALTIME_PATH]
    assert "StartInterval" not in data


def test_the_catchup_sweep_is_off_unless_the_manifest_asks_for_it(
    tree: Tree, tmp_path: Path
) -> None:
    """launchd cannot observe a clock step; the bounded fallback is opt-in."""
    tree.write_manifest(
        MANIFEST.replace('randomized_delay = "5m"', 'randomized_delay = "5m"\ncatchup_sweep = "6h"')
    )
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    backend = make_backend(tree, tmp_path)
    rendered = backend.desired_files(automations, automations.enabled_tasks())[
        f"{CATCHUP_LABEL}.plist"
    ]
    assert rendered == (SWEEP_GOLDEN / f"{CATCHUP_LABEL}.plist").read_text(encoding="utf-8")
    data = plistlib.loads(rendered.encode("utf-8"))
    assert data["StartInterval"] == 21600
    assert data["WatchPaths"] == [LOCALTIME_PATH]


@pytest.mark.parametrize(
    "spec",
    [
        'description = "d"\ncommand = ["true"]\n',
        'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\npersistent = false\n',
        'description = "d"\ncommand = ["true"]\nschedule = "every 15m"\npersistent = true\n',
        'description = "d"\ncommand = ["true"]\n\n[schedule]\n'
        "launchd = [{ Weekday = 1, Hour = 9, Minute = 0 }]\n",
    ],
)
def test_a_host_without_persistent_calendar_work_gets_no_catchup_agent(
    tree: Tree, tmp_path: Path, spec: str
) -> None:
    tree.write_task("hello", spec)
    automations = tree.load()
    backend = make_backend(tree, tmp_path)
    tasks = automations.enabled_tasks()

    files = backend.desired_files(automations, tasks)
    assert f"{CATCHUP_LABEL}.plist" not in files

    backend.activate(automations, tasks, files)
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert not any(CATCHUP_LABEL in line for line in runner.transcript)


def test_reconcile_removes_catchup_agent_when_it_is_no_longer_wanted(
    tree: Tree, tmp_path: Path
) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    automations = tree.load()
    backend = make_backend(tree, tmp_path)
    initial = backend.desired_files(automations, automations.enabled_tasks())
    backend.unit_dir.mkdir(parents=True)
    backend.apply(backend.plan(initial), initial)
    catchup_path = backend.unit_dir / f"{CATCHUP_LABEL}.plist"
    assert catchup_path.exists()

    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\npersistent = false\n',
    )
    revised = tree.load()
    desired = backend.desired_files(revised, revised.enabled_tasks())
    plan = backend.plan(desired)

    assert {change.path.name: change.action for change in plan.changes}[
        f"{CATCHUP_LABEL}.plist"
    ] == DELETE
    backend.apply(plan, desired)
    assert not catchup_path.exists()
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert f"launchctl bootout gui/501/{CATCHUP_LABEL}" in runner.transcript


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


def test_activate_reloads_a_plist_this_reconcile_rewrote(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """A drifted plist launchd already loaded must be reconverged, not trusted."""
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    backend.activate(automations, tasks, files)

    # The file drifted on disk and this reconcile put it back: the content now
    # matches what we last activated, but launchd is running the edit.
    filename = "automationctl.calendar-task.plist"
    second = RecordingRunner()
    backend.runner = second
    backend.activate(automations, tasks, files, rewritten={filename})

    booted_out = [line for line in second.transcript if "bootout" in line]
    assert booted_out == ["launchctl bootout gui/501/automationctl.calendar-task"]
    assert f"launchctl bootstrap gui/501 {backend.unit_dir}/{filename}" in second.transcript


def test_activate_boots_out_when_the_probe_cannot_answer(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """Unknown state means reload, exactly as it means bootout in deactivate."""
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    backend.activate(automations, tasks, files)  # hashes now match

    label = "automationctl.calendar-task"
    probe = ("launchctl", "print", f"gui/501/{label}")
    unknown = RecordingRunner(
        responses={probe: CommandResult(probe, 5, stderr="Bad file descriptor")}
    )
    backend.runner = unknown
    backend.activate(automations, tasks, files)

    relevant = [line for line in unknown.transcript if label in line and "print" not in line]
    assert relevant == [
        f"launchctl enable gui/501/{label}",
        f"launchctl bootout gui/501/{label}",
        f"launchctl bootstrap gui/501 {backend.unit_dir}/{label}.plist",
    ]


def test_a_not_found_code_is_unknown_when_the_domain_will_not_answer(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """113 means "not loaded" only if the domain itself is reachable."""
    backend, _, _ = rendered
    label = "automationctl.calendar-task"
    domain = ("launchctl", "print", "gui/501")
    runner = RecordingRunner(
        responses={
            **not_loaded(label),
            domain: CommandResult(domain, 1, stderr="domain unreachable"),
        }
    )
    backend.runner = runner
    results = backend.deactivate([f"{label}.plist"])

    assert [result.argv for result in results] == [("launchctl", "bootout", f"gui/501/{label}")]
    assert runner.transcript.count("launchctl print gui/501") == 1


def test_the_domain_is_probed_at_most_once_per_verb(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, _, files = rendered
    labels = [name[: -len(".plist")] for name in files]
    runner = RecordingRunner(responses=not_loaded(*labels))
    backend.runner = runner
    backend.deactivate(sorted(files))
    assert runner.transcript.count("launchctl print gui/501") == 1


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
    tree.write_task("catchup", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
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
    backend, automations, files = rendered
    label = "automationctl.calendar-task"
    backend.activate(automations, automations.enabled_tasks(), files)
    assert label in records.read_activation(backend.state_dir, "launchd")

    runner = RecordingRunner(responses=not_loaded(label))
    backend.runner = runner
    assert backend.deactivate([f"{label}.plist"]) == []
    assert not any("bootout" in line for line in runner.transcript)
    assert label not in records.read_activation(backend.state_dir, "launchd")


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


def test_deactivate_keeps_the_activation_record_when_bootout_fails(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    backend.activate(automations, tasks, files)
    label = "automationctl.calendar-task"
    assert label in records.read_activation(backend.state_dir, "launchd")

    backend.runner = FailingRunner()
    results = backend.deactivate([f"{label}.plist"])

    assert results and not results[0].ok
    assert label in records.read_activation(backend.state_dir, "launchd")


def test_submit_and_pause_use_the_expected_commands(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    task = automations.tasks["calendar-task"]
    backend.submit(task)
    backend.pause(task)
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript == [
        "launchctl kickstart -k gui/501/automationctl.calendar-task",
        "launchctl disable gui/501/automationctl.calendar-task",
        "launchctl print gui/501/automationctl.calendar-task",
        "launchctl bootout gui/501/automationctl.calendar-task",
    ]


def test_pause_is_idempotent_when_the_agent_is_already_absent(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    task = tree.load().tasks["calendar-task"]
    label = "automationctl.calendar-task"
    backend = make_backend(tree, tmp_path, RecordingRunner(responses=not_loaded(label)))

    results = backend.pause(task)

    assert all(result.ok for result in results)
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert not any("bootout" in line for line in runner.transcript)


def test_pause_stops_when_disable_is_refused(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    argv = ("launchctl", "disable", "gui/501/automationctl.calendar-task")
    runner = RecordingRunner(responses={argv: CommandResult(argv, 1, stderr="refused")})
    backend.runner = runner

    results = backend.pause(automations.tasks["calendar-task"])

    assert len(results) == 1 and not results[0].ok
    assert runner.transcript == ["launchctl disable gui/501/automationctl.calendar-task"]


def test_resume_is_idempotent_when_the_agent_is_already_loaded(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    backend.resume(automations.tasks["calendar-task"])
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript == [
        "launchctl enable gui/501/automationctl.calendar-task",
        "launchctl print gui/501/automationctl.calendar-task",
    ]


def test_resume_bootstraps_an_agent_that_is_absent(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(MANIFEST)
    for name, text in TASKS.items():
        tree.write_task(name, text)
    task = tree.load().tasks["calendar-task"]
    label = "automationctl.calendar-task"
    backend = make_backend(tree, tmp_path, RecordingRunner(responses=not_loaded(label)))

    results = backend.resume(task)

    assert all(result.ok for result in results)
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.transcript == [
        "launchctl enable gui/501/automationctl.calendar-task",
        "launchctl print gui/501/automationctl.calendar-task",
        "launchctl print gui/501",
        f"launchctl bootstrap gui/501 {backend.unit_dir}/automationctl.calendar-task.plist",
    ]


def test_resume_stops_when_enable_is_refused(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    argv = ("launchctl", "enable", "gui/501/automationctl.calendar-task")
    runner = RecordingRunner(responses={argv: CommandResult(argv, 1, stderr="refused")})
    backend.runner = runner

    results = backend.resume(automations.tasks["calendar-task"])

    assert len(results) == 1 and not results[0].ok
    assert runner.transcript == ["launchctl enable gui/501/automationctl.calendar-task"]


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


def test_doctor_reports_the_catchup_agent_and_its_triggers(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    assert backend.catchup_health(automations, tasks)[0].ok is False

    backend.unit_dir.mkdir(parents=True)
    backend.apply(backend.plan(files), files)
    check = backend.catchup_health(automations, tasks)[0]
    assert check.ok is True
    assert LOCALTIME_PATH in check.detail
    assert "no sweep configured" in check.detail


def test_doctor_reports_catchup_as_unneeded_without_persistent_calendar_work(
    tree: Tree, tmp_path: Path
) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\n')
    automations = tree.load()
    backend = make_backend(tree, tmp_path)

    check = backend.catchup_health(automations, automations.enabled_tasks())[0]

    assert check.ok is True
    assert "not needed" in check.detail
    runner = backend.runner
    assert isinstance(runner, RecordingRunner)
    assert runner.calls == []


def test_doctor_reports_a_configured_sweep(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(
        MANIFEST.replace('randomized_delay = "5m"', 'randomized_delay = "5m"\ncatchup_sweep = "6h"')
    )
    for name, text in TASKS.items():
        tree.write_task(name, text)
    automations = tree.load()
    backend = make_backend(tree, tmp_path)
    files = backend.desired_files(automations, automations.enabled_tasks())
    backend.unit_dir.mkdir(parents=True)
    backend.apply(backend.plan(files), files)
    check = backend.catchup_health(automations, automations.enabled_tasks())[0]
    assert "sweeping every 21600s" in check.detail


def test_launchd_cannot_follow_live_logs(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    backend, automations, _ = rendered
    assert backend.follow_argv(automations.tasks["calendar-task"]) is None


def test_doctor_reports_a_stale_catchup_agent(
    rendered: tuple[LaunchdBackend, Automations, dict[str, str]],
) -> None:
    """A pre-upgrade plist without the current triggers must not read as ok."""
    backend, automations, files = rendered
    tasks = automations.enabled_tasks()
    backend.unit_dir.mkdir(parents=True)
    backend.apply(backend.plan(files), files)
    path = backend.unit_dir / "automationctl.catchup.plist"
    path.write_text(
        path.read_text(encoding="utf-8").replace("WatchPaths", "SomedayPaths"),
        encoding="utf-8",
    )
    check = backend.catchup_health(automations, tasks)[0]
    assert check.ok is False
    assert "stale" in check.detail
