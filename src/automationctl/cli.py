"""Command line for XDG-installed standalone tasks."""

from __future__ import annotations

import difflib
import os
import socket
import subprocess
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

from . import backends, catchup, paths, records
from . import doctor as doctor_module
from . import lint as lint_module
from .backends import Backend, ReconcilePlan
from .commands import CommandResult
from .config import (
    discover_installed,
    load_installed,
    load_policy,
    load_task,
    management_lock,
    materialize,
    task_path,
    write_installed,
)
from .errors import AutomationctlError, ConfigError
from .schedule import format_duration
from .schedule import parse as parse_schedule
from .spec import MachinePolicy, TaskSpec, validate_task_name
from .wrapper import ExecOptions, exec_task

app = typer.Typer(
    name="automationctl", help="Install and run standalone scheduled tasks.", no_args_is_help=True
)
EXIT_USAGE, EXIT_FAILURE = 2, 1
BackendOption = Annotated[str | None, typer.Option("--backend", help="Force systemd or launchd.")]
UnitDirOption = Annotated[
    Path | None, typer.Option("--unit-dir", help="Generated-artifact directory (testing only).")
]


def _fail(message: str, code: int = EXIT_USAGE) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(version("automationctl"))
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool, typer.Option("--version", callback=_print_version, is_eager=True)
    ] = False,
) -> None:
    """Agent-neutral automation runner."""


def _env() -> dict[str, str]:
    return dict(os.environ)


def _backend(name: str | None, unit_dir: Path | None, env: dict[str, str]) -> tuple[str, Backend]:
    try:
        backend_name = name or backends.default_backend_name(env)
        return backend_name, backends.create(backend_name, unit_dir=unit_dir, env=env)
    except (ValueError, AutomationctlError) as exc:
        raise _fail(str(exc)) from exc


def _hostname() -> str:
    return socket.gethostname().split(".")[0]


def _run(task: TaskSpec, env: dict[str, str], *, jitter: bool) -> int:
    result = exec_task(
        task,
        ExecOptions(state_dir=paths.state_dir(env), hostname=_hostname(), env=env, jitter=jitter),
    )
    message = (
        f"{result.task}: {result.status} (exit {result.exit_code}, "
        f"{result.duration_seconds:.1f}s, {result.run_dir})"
        + (f"; {result.reason}" if result.reason else "")
    )
    typer.echo(message, err=True)
    return 128 + abs(result.exit_code) if result.exit_code < 0 else result.exit_code


def _report(results: Sequence[CommandResult]) -> None:
    failed = [result for result in results if not result.ok]
    for result in failed:
        typer.secho(
            f"warning: {result.display} exited {result.returncode}: {result.stderr.strip()}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if failed:
        raise _fail(f"{len(failed)} scheduler command(s) failed", EXIT_FAILURE)


def _render(plan: ReconcilePlan, *, show_diff: bool) -> None:
    if not plan.has_changes:
        typer.echo("no generated artifact changes")
    for change in plan.changed:
        typer.echo(f"{change.action}: {change.path}")
        if show_diff and change.diff:
            typer.echo(change.diff.rstrip())


def _lint(task: TaskSpec, policy: MachinePolicy, backend_name: str) -> None:
    report = lint_module.lint_task(task, policy, backend_name)
    for line in report.render():
        typer.echo(line, err=not report.ok)
    if not report.ok:
        raise _fail(f"lint found {len(report.errors)} error(s)", EXIT_FAILURE)


def _catchup_desired(
    backend: Backend, policy: MachinePolicy, tasks: dict[str, TaskSpec]
) -> dict[str, str]:
    return (
        backend.desired_catchup_files(policy)
        if catchup.triggers_wanted(list(tasks.values()))
        else {}
    )


def _install_task(
    task: TaskSpec,
    *,
    replace_existing: bool,
    dry_run: bool,
    show_diff: bool,
    backend_name: str | None,
    unit_dir: Path | None,
    env: dict[str, str],
) -> None:
    policy = load_policy(env)
    backend_label, backend = _backend(backend_name, unit_dir, env)
    task = materialize(task)
    _lint(task, policy, backend_label)
    installed, _ = discover_installed(env)
    target = task_path(task.name, env)
    if os.path.lexists(target) and not replace_existing:
        raise _fail(f"task {task.name!r} is already installed; pass --replace to update it")
    post = dict(installed)
    post[task.name] = task
    desired = backend.desired_task_files(task)
    desired.update(_catchup_desired(backend, policy, post))
    scope = [*backend.possible_task_filenames(task.name), *backend.desired_catchup_files(policy)]
    plan = backend.plan(desired, scope)
    try:
        before = target.read_text(encoding="utf-8")
    except OSError:
        before = ""
    task_change = "update" if os.path.lexists(target) else "create"
    typer.echo(f"{task_change}: {target}")
    if show_diff:
        typer.echo(
            "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    __import__("automationctl.config", fromlist=["dump_task"])
                    .dump_task(task)
                    .splitlines(keepends=True),
                    fromfile=f"a/{target.name}",
                    tofile=f"b/{target.name}",
                )
            ).rstrip()
        )
    _render(plan, show_diff=show_diff)
    if dry_run:
        return
    with management_lock(env):
        latest, _ = discover_installed(env)
        if os.path.lexists(task_path(task.name, env)) and not replace_existing:
            raise _fail(f"task {task.name!r} was installed concurrently; retry with --replace")
        post = dict(latest)
        post[task.name] = task
        desired = backend.desired_task_files(task)
        desired.update(_catchup_desired(backend, policy, post))
        plan = backend.plan(desired, scope)
        _report(backend.apply(plan, desired))
        write_installed(task, env)
        _report(backend.reload())
        rewritten = backend.rewritten(plan)
        _report(backend.activate(task, desired, rewritten))
        _report(backend.activate_catchup(desired, rewritten))
    typer.echo(f"installed {task.name}")


@app.command()
def lint(
    files: Annotated[
        list[Path] | None, typer.Argument(help="Source task files; omit to lint installed tasks.")
    ] = None,
    backend: BackendOption = None,
) -> None:
    """Lint source task files or every installed definition."""
    env = _env()
    policy = load_policy(env)
    backend_name, _ = _backend(backend, None, env)
    tasks: list[TaskSpec] = []
    errors: list[Exception] = []
    if files:
        for path in files:
            try:
                tasks.append(materialize(load_task(path)))
            except ConfigError as exc:
                errors.append(exc)
    else:
        installed, discovered = discover_installed(env)
        tasks = list(installed.values())
        errors.extend(discovered)
    reports = [lint_module.lint_task(task, policy, backend_name) for task in tasks]
    for error in errors:
        typer.echo(f"error: {error}", err=True)
    for report in reports:
        for line in report.render():
            typer.echo(line, err=not report.ok)
    count = len(errors) + sum(len(report.errors) for report in reports)
    if count:
        raise _fail(f"lint found {count} error(s)", EXIT_FAILURE)
    typer.echo("lint: ok")


@app.command()
def install(
    file: Annotated[Path, typer.Argument(help="One source task TOML file.")],
    replace_existing: Annotated[bool, typer.Option("--replace")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    diff: Annotated[bool, typer.Option("--diff")] = False,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Validate and install one self-contained task."""
    env = _env()
    try:
        task = load_task(file)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    _install_task(
        task,
        replace_existing=replace_existing,
        dry_run=dry_run,
        show_diff=diff,
        backend_name=backend,
        unit_dir=unit_dir,
        env=env,
    )


@app.command()
def add(
    name: Annotated[str, typer.Argument(help="Task name.")],
    command: Annotated[list[str], typer.Argument(help="Command argv; place it after --.")],
    description: Annotated[str | None, typer.Option("--description")] = None,
    every: Annotated[str | None, typer.Option("--every")] = None,
    schedule: Annotated[str | None, typer.Option("--schedule")] = None,
    cwd: Annotated[Path | None, typer.Option("--cwd")] = None,
    timeout: Annotated[str | None, typer.Option("--timeout")] = None,
    stdin: Annotated[str | None, typer.Option("--stdin")] = None,
    stdin_file: Annotated[Path | None, typer.Option("--stdin-file")] = None,
    replace_existing: Annotated[bool, typer.Option("--replace")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    diff: Annotated[bool, typer.Option("--diff")] = False,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Create and install one standalone task."""
    if every and schedule:
        raise _fail("--every and --schedule are mutually exclusive")
    if stdin is not None and stdin_file is not None:
        raise _fail("--stdin and --stdin-file are mutually exclusive")
    if not command:
        raise _fail("COMMAND is required after --")
    env = _env()
    fake_path = Path.cwd() / f"{name}.toml"
    try:
        validate_task_name(name, fake_path)
        schedule_value = f"every {every}" if every else schedule
        parsed = parse_schedule(schedule_value) if schedule_value else None
        source = TaskSpec(
            name=name,
            path=fake_path,
            description=description or name,
            command=tuple(command),
            stdin=stdin,
            stdin_file=str(stdin_file) if stdin_file else None,
            cwd=str(paths.expand(cwd or Path.cwd()).resolve()),
            schedule=parsed,
            timeout_seconds=None
            if timeout is None
            else __import__("automationctl.schedule", fromlist=["parse_duration"]).parse_duration(
                timeout
            ),
        )
    except AutomationctlError as exc:
        raise _fail(str(exc)) from exc
    _install_task(
        source,
        replace_existing=replace_existing,
        dry_run=dry_run,
        show_diff=diff,
        backend_name=backend,
        unit_dir=unit_dir,
        env=env,
    )


@app.command("remove")
def remove_task(
    name: Annotated[str, typer.Argument(help="Installed task name.")],
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Remove one installed definition and its scheduler artifacts."""
    env = _env()
    try:
        validate_task_name(name, task_path(name, env))
        policy = load_policy(env)
    except AutomationctlError as exc:
        raise _fail(str(exc)) from exc
    _, scheduler = _backend(backend, unit_dir, env)
    with management_lock(env):
        tasks, _ = discover_installed(env)
        tasks.pop(name, None)
        desired = _catchup_desired(scheduler, policy, tasks)
        scope = [*scheduler.possible_task_filenames(name), *scheduler.desired_catchup_files(policy)]
        plan = scheduler.plan(desired, scope)
        _report(scheduler.apply(plan, desired))
        target = task_path(name, env)
        target.unlink(missing_ok=True)
        _report(scheduler.reload())
        _report(scheduler.activate_catchup(desired, scheduler.rewritten(plan)))
    typer.echo(f"removed {name}")


def _installed_or_fail(name: str, env: dict[str, str]) -> TaskSpec:
    try:
        return load_installed(name, env)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def run(
    name: Annotated[str, typer.Argument()],
    jitter: Annotated[bool, typer.Option("--jitter")] = False,
) -> None:
    raise typer.Exit(_run(_installed_or_fail(name, _env()), _env(), jitter=jitter))


@app.command("exec")
def exec_command(
    name: Annotated[str, typer.Argument()],
    jitter: Annotated[bool, typer.Option("--jitter")] = False,
) -> None:
    raise typer.Exit(_run(_installed_or_fail(name, _env()), _env(), jitter=jitter))


@app.command()
def submit(
    name: Annotated[str, typer.Argument()],
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    env = _env()
    _, scheduler = _backend(backend, unit_dir, env)
    task = _installed_or_fail(name, env)
    _report(scheduler.submit(task))
    typer.echo(f"submitted {name}")


@app.command("catch-up")
def catch_up(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False, backend: BackendOption = None
) -> None:
    env = _env()
    backend_name, _ = _backend(backend, None, env)
    tasks, _ = discover_installed(env)
    failures = 0
    for decision in catchup.plan(
        list(tasks.values()), state_dir=paths.state_dir(env), backend=backend_name
    ):
        typer.echo(f"{'due ' if decision.due else 'skip'} {decision.task}: {decision.reason}")
        if decision.due and not dry_run:
            code = _run(tasks[decision.task], env, jitter=False)
            failures += code != 0
    if failures:
        raise typer.Exit(EXIT_FAILURE)


def _validate_record_name(name: str, env: dict[str, str]) -> None:
    try:
        validate_task_name(name, task_path(name, env))
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _substrate_label(task: TaskSpec, scheduler: Backend) -> str:
    if task.schedule is None:
        return "manual"
    enabled = scheduler.enabled(task)
    return "enabled" if enabled is True else "paused" if enabled is False else "unknown"


@app.command("list")
def list_tasks(backend: BackendOption = None, unit_dir: UnitDirOption = None) -> None:
    env = _env()
    _, scheduler = _backend(backend, unit_dir, env)
    tasks, errors = discover_installed(env)
    for error in errors:
        typer.secho(f"warning: {error}", fg=typer.colors.YELLOW, err=True)
    if not tasks:
        typer.echo("no installed tasks")
        return
    typer.echo("TASK  SCHEDULE  DESIRED  SUBSTRATE  LAST")
    for task in tasks.values():
        last = records.read_last(paths.state_dir(env), task.name) or {}
        schedule = task.schedule.text if task.schedule else "manual"
        desired = "disabled" if task.disabled else "enabled"
        typer.echo(
            f"{task.name}  {schedule}  {desired}  {_substrate_label(task, scheduler)}  "
            f"{last.get('status', 'never')}"
        )


@app.command()
def status(
    name: Annotated[str | None, typer.Argument()] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 10,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    env = _env()
    if name is not None:
        _validate_record_name(name, env)
    tasks, _ = discover_installed(env)
    scheduler: Backend | None = None
    if tasks:
        _, scheduler = _backend(backend, unit_dir, env)
    names = [name] if name else sorted(set(tasks) | set(records.known_tasks(paths.state_dir(env))))
    for item in names:
        assert item is not None
        task = tasks.get(item)
        typer.echo(f"{item}:")
        if task is not None:
            assert scheduler is not None
            schedule = task.schedule.text if task.schedule else "manual"
            desired = "disabled" if task.disabled else "enabled"
            decision = catchup.decide(task, state_dir=paths.state_dir(env), backend=scheduler.name)
            typer.echo(f"  schedule: {schedule}")
            typer.echo(f"  desired: {desired}")
            typer.echo(f"  substrate: {_substrate_label(task, scheduler)}")
            typer.echo(f"  catch-up: {'due' if decision.due else 'not due'} ({decision.reason})")
        runs = records.recent_runs(paths.state_dir(env), item, limit)
        if not runs:
            typer.echo("  no recorded runs")
        for entry in runs:
            duration = (
                format_duration(int(entry.duration_seconds))
                if entry.duration_seconds is not None
                else "-"
            )
            typer.echo(f"  {entry.run_id}  {entry.status}  {entry.exit_code}  {duration}")


LOG_TAIL_BYTES = 8 * 1024 * 1024


def _has_log_output(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _show_log(path: Path, lines: int, *, annotation: str | None = None) -> None:
    suffix = f" ({annotation})" if annotation else ""
    typer.echo(f"# {path}{suffix}")
    for line in records.tail_lines(path, lines, max_bytes=LOG_TAIL_BYTES):
        typer.echo(line)


@app.command()
def logs(
    name: Annotated[str, typer.Argument()],
    lines: Annotated[int, typer.Option("--lines", "-n", min=1)] = 200,
    show_stdout: Annotated[bool, typer.Option("--stdout")] = False,
    show_stderr: Annotated[bool, typer.Option("--stderr")] = False,
    show_both: Annotated[bool, typer.Option("--both")] = False,
    follow: Annotated[bool, typer.Option("--follow")] = False,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    if sum((show_stdout, show_stderr, show_both)) > 1:
        raise _fail("choose at most one of --stdout, --stderr, and --both")
    env = _env()
    _validate_record_name(name, env)
    if follow:
        if show_stdout or show_stderr or show_both:
            raise _fail("stream selection cannot be used with --follow")
        task = _installed_or_fail(name, env)
        _, scheduler = _backend(backend, unit_dir, env)
        argv = scheduler.follow_argv(task)
        if argv is None:
            raise _fail("backend cannot follow live logs")
        try:
            raise typer.Exit(subprocess.call(argv))
        except FileNotFoundError as exc:
            raise _fail(f"cannot follow logs: {exc}") from exc
    latest = records.latest_run(paths.state_dir(env), name)
    if latest is None:
        typer.echo(f"no recorded runs for {name}")
        return
    stdout_path = latest.run_dir / records.STDOUT_FILE
    stderr_path = latest.run_dir / records.STDERR_FILE
    stdout_captured, stderr_captured = _has_log_output(stdout_path), _has_log_output(stderr_path)
    if show_both:
        if not stdout_captured and not stderr_captured:
            typer.echo(f"no output captured for {name} run {latest.run_id}")
            return
        if stdout_captured:
            _show_log(stdout_path, lines)
        if stderr_captured:
            _show_log(stderr_path, lines)
        return
    annotation: str | None = None
    if show_stdout:
        path = stdout_path
    elif show_stderr:
        path = stderr_path
    elif latest.status in records.FAILURE_STATUSES and stderr_captured:
        path, annotation = stderr_path, f"auto-selected: run {latest.status}"
    elif stdout_captured:
        path = stdout_path
        if latest.status in records.FAILURE_STATUSES:
            annotation = "auto-selected: stderr empty"
    elif stderr_captured:
        path, annotation = stderr_path, "auto-selected: stdout empty"
    else:
        typer.echo(f"no output captured for {name} run {latest.run_id}")
        return
    if not _has_log_output(path):
        typer.echo(f"no output captured at {path}")
        return
    _show_log(path, lines, annotation=annotation)
    if not any((show_stdout, show_stderr, show_both)):
        other_path = stderr_path if path == stdout_path else stdout_path
        if _has_log_output(other_path):
            option = "--stderr" if other_path == stderr_path else "--stdout"
            typer.echo(f"note: {other_path.name} also captured; use {option}", err=True)


@app.command()
def pause(
    name: Annotated[str, typer.Argument()],
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    env = _env()
    task = _installed_or_fail(name, env)
    if task.schedule is None:
        raise _fail(f"cannot pause manual task {name!r}")
    _, scheduler = _backend(backend, unit_dir, env)
    _report(scheduler.pause(task))
    typer.echo(f"paused {name}")


@app.command()
def resume(
    name: Annotated[str, typer.Argument()],
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    env = _env()
    task = _installed_or_fail(name, env)
    if task.schedule is None:
        raise _fail(f"cannot resume manual task {name!r}")
    _, scheduler = _backend(backend, unit_dir, env)
    _report(scheduler.resume(task))
    typer.echo(f"resumed {name}")


@app.command()
def doctor(backend: BackendOption = None, unit_dir: UnitDirOption = None) -> None:
    env = _env()
    _, scheduler = _backend(backend, unit_dir, env)
    tasks, errors = discover_installed(env)
    report = doctor_module.run(
        list(tasks.values()),
        errors,
        scheduler,
        policy=load_policy(env),
        state_dir=paths.state_dir(env),
        env=env,
    )
    for check in report.checks:
        typer.echo(f"{'ok  ' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    if not report.ok:
        raise typer.Exit(EXIT_FAILURE)


@app.command()
def prune(keep_runs: Annotated[int, typer.Option("--keep-runs", min=0)] = 50) -> None:
    removed = records.prune(paths.state_dir(_env()), keep_runs)
    typer.echo(f"pruned {len(removed)} run(s)")


def entrypoint() -> None:
    app()
