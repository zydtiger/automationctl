"""Command-line interface for automationctl."""

import os
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

from . import backends, catchup, paths, records
from . import doctor as doctor_module
from . import lint as lint_module
from .backends import Backend, ReconcilePlan
from .commands import CommandResult
from .config import Automations, load
from .errors import AutomationctlError
from .schedule import format_duration
from .spec import TaskSpec
from .wrapper import ExecOptions, ExecResult, exec_task

app = typer.Typer(
    name="automationctl",
    help="Agent-neutral automation runner: compile task specs to the platform scheduler.",
    no_args_is_help=True,
)

EXIT_USAGE = 2
EXIT_FAILURE = 1

ManifestOption = Annotated[
    Path | None,
    typer.Option("--manifest", help="Path to manifest.toml.", envvar=paths.MANIFEST_ENV),
]
HostOption = Annotated[
    str | None, typer.Option("--host", help="Host key to use instead of the short hostname.")
]
BackendOption = Annotated[
    str | None, typer.Option("--backend", help="Force a scheduler backend: systemd or launchd.")
]
UnitDirOption = Annotated[
    Path | None,
    typer.Option("--unit-dir", help="Directory for generated units.", envvar=paths.UNIT_DIR_ENV),
]


def _print_version(value: bool) -> None:
    if value:
        typer.echo(version("automationctl"))
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Agent-neutral automation runner."""


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _fail(message: str, code: int = EXIT_USAGE) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


@dataclass
class Session:
    """Everything a command needs after option resolution."""

    automations: Automations
    backend_name: str
    backend: Backend
    state_dir: Path
    env: dict[str, str]

    def task(self, name: str) -> TaskSpec:
        try:
            return self.automations.require_task(name)
        except AutomationctlError as exc:
            raise _fail(str(exc)) from exc


def _session(
    manifest: Path | None,
    host: str | None,
    backend_name: str | None = None,
    unit_dir: Path | None = None,
) -> Session:
    env = dict(os.environ)
    try:
        automations = load(manifest_path=manifest, host=host, env=env)
        name = backend_name or backends.default_backend_name(env)
        backend = backends.create(
            name,
            manifest_path=automations.manifest.path,
            unit_dir=unit_dir,
            state_dir=paths.state_dir(env),
            env=env,
        )
    except AutomationctlError as exc:
        raise _fail(str(exc)) from exc
    return Session(
        automations=automations,
        backend_name=name,
        backend=backend,
        state_dir=paths.state_dir(env),
        env=env,
    )


def _lint_or_exit(session: Session, tasks: list[str] | None = None) -> None:
    report = lint_module.lint(session.automations, backend=session.backend_name, tasks=tasks)
    for line in report.render():
        typer.echo(line, err=not report.ok)
    if not report.ok:
        raise _fail(f"lint found {len(report.errors)} error(s)", EXIT_FAILURE)


def _require_declared_host(session: Session) -> None:
    """Refuse to reconcile for a host the manifest does not declare.

    An undeclared host selects no tasks, so a reconcile would compute an empty
    desired state and garbage-collect every managed unit on the machine. A
    typo in ``--host`` must not uninstall the installation.
    """
    if session.automations.host_declared:
        return
    raise _fail(
        f"host {session.automations.host!r} is not declared in "
        f"{session.automations.manifest.path}; refusing to reconcile, which would "
        "remove every managed file. Add a [hosts.*] section or pass --host."
    )


def _exit_code_for(result: ExecResult) -> int:
    if result.exit_code < 0:
        return 128 + abs(result.exit_code)
    return result.exit_code


def _run_task(session: Session, task: TaskSpec, *, jitter: bool) -> ExecResult:
    options = ExecOptions(
        state_dir=session.state_dir,
        hostname=session.automations.host,
        env=session.env,
        jitter=jitter,
    )
    return exec_task(session.automations, task, options)


def _report_results(results: list[CommandResult]) -> int:
    """Report every failed control command and return how many failed."""
    failures = 0
    for result in results:
        if result.ok:
            continue
        failures += 1
        typer.secho(
            f"warning: {result.display} exited {result.returncode}: {result.stderr.strip()}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    return failures


def _exit_on_substrate_failure(failures: int) -> None:
    """A scheduler that refused our command left the host in an unknown state."""
    if failures:
        raise _fail(f"{failures} scheduler command(s) failed", EXIT_FAILURE)


def _print_table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for row in rows:
        line = "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        typer.echo(line.rstrip())


def _enabled_label(session: Session, task: TaskSpec) -> str:
    if task.disabled:
        return "disabled"
    present = any(
        (session.backend.unit_dir / name).exists() for name in session.backend.task_filenames(task)
    )
    if not present:
        return "not installed"
    state = session.backend.enabled(task)
    if state is None:
        return "installed"
    return "yes" if state else "no"


def _last_columns(session: Session, task: TaskSpec) -> tuple[str, str]:
    last = records.read_last(session.state_dir, task.name)
    if last is None:
        return ("never", "-")
    started = str(last.get("started_at", "-"))
    status = str(last.get("status", "unknown"))
    duration = last.get("duration_seconds")
    if isinstance(duration, int | float):
        return (started, f"{status} ({format_duration(int(duration))})")
    return (started, status)


# ---------------------------------------------------------------------------
# validation and inspection
# ---------------------------------------------------------------------------


@app.command()
def lint(
    tasks: Annotated[list[str] | None, typer.Argument(help="Tasks to lint; default all.")] = None,
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
) -> None:
    """Validate the automations repository against schema and policy."""
    session = _session(manifest, host, backend)
    _lint_or_exit(session, tasks or None)
    typer.echo("lint: ok")


@app.command("list")
def list_tasks(
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """List the tasks this host selects with their last recorded outcome."""
    session = _session(manifest, host, backend, unit_dir)
    tasks = session.automations.selected_tasks()
    if not tasks:
        typer.echo(f"no tasks selected for host {session.automations.host}")
        return
    rows: list[tuple[str, ...]] = [("TASK", "SCHEDULE", "ENABLED", "LAST", "RESULT")]
    for task in tasks:
        schedule = task.schedule.text if task.schedule is not None else "manual"
        rows.append(
            (task.name, schedule, _enabled_label(session, task), *_last_columns(session, task))
        )
    _print_table(rows)


@app.command()
def status(
    task_name: Annotated[str | None, typer.Argument(help="Task to inspect.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Recent runs to show.")] = 10,
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Show recent runs, exit codes, and durations."""
    session = _session(manifest, host, backend, unit_dir)
    if task_name:
        names = [task_name]
    else:
        names = [task.name for task in session.automations.selected_tasks()]
    for name in names:
        task = session.task(name)
        typer.echo(f"{task.name}: {task.description}")
        typer.echo(f"  schedule: {task.schedule.text if task.schedule else 'manual'}")
        typer.echo(f"  substrate: {_enabled_label(session, task)}")
        decision = catchup.decide(
            session.automations,
            task,
            state_dir=session.state_dir,
            backend=session.backend_name,
        )
        typer.echo(f"  catch-up: {'due' if decision.due else 'not due'} ({decision.reason})")
        runs = records.recent_runs(session.state_dir, name, limit=limit)
        if not runs:
            typer.echo("  no recorded runs")
            continue
        rows: list[tuple[str, ...]] = [("  RUN", "STATUS", "EXIT", "DURATION")]
        for entry in runs:
            duration = (
                format_duration(int(entry.duration_seconds))
                if entry.duration_seconds is not None
                else "-"
            )
            rows.append(
                (
                    f"  {entry.run_id}",
                    entry.status,
                    "-" if entry.exit_code is None else str(entry.exit_code),
                    duration,
                )
            )
        _print_table(rows)


@app.command()
def logs(
    task_name: Annotated[str, typer.Argument(help="Task whose logs to show.")],
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow live logs.")] = False,
    show_stderr: Annotated[
        bool, typer.Option("--stderr", help="Show stderr instead of stdout.")
    ] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Tail this many lines.")] = 200,
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
) -> None:
    """Show the last run's captured output, or follow the substrate's live log."""
    session = _session(manifest, host, backend)
    task = session.task(task_name)
    if follow:
        argv = session.backend.follow_argv(task)
        if argv is None:
            raise _fail(f"the {session.backend_name} backend cannot follow live logs")
        try:
            code = subprocess.call(list(argv))
        except OSError as exc:
            raise _fail(f"cannot follow logs: {argv[0]} is not available: {exc}") from exc
        raise typer.Exit(code)
    latest = records.latest_run(session.state_dir, task.name)
    if latest is None:
        typer.echo(f"no recorded runs for {task.name}")
        return
    path = latest.run_dir / (records.STDERR_FILE if show_stderr else records.STDOUT_FILE)
    if not path.exists():
        typer.echo(f"no output captured at {path}")
        return
    typer.echo(f"# {path}")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]:
        typer.echo(line)


@app.command()
def doctor(
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Probe the host for the failures that silently break unattended runs."""
    session = _session(manifest, host, backend, unit_dir)
    report = doctor_module.run(
        session.automations, session.backend, env=session.env, state_dir=session.state_dir
    )
    for check in report.checks:
        mark = "ok  " if check.ok else "FAIL"
        typer.echo(f"{mark} {check.name}: {check.detail}")
    if not report.ok:
        raise typer.Exit(EXIT_FAILURE)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


@app.command()
def run(
    task_name: Annotated[str, typer.Argument(help="Task to run.")],
    jitter: Annotated[bool, typer.Option("--jitter", help="Apply randomized_delay first.")] = False,
    manifest: ManifestOption = None,
    host: HostOption = None,
) -> None:
    """Run a task in the foreground with streaming output."""
    session = _session(manifest, host)
    task = session.task(task_name)
    result = _run_task(session, task, jitter=jitter)
    typer.echo(
        f"{result.task}: {result.status} "
        f"(exit {result.exit_code}, {result.duration_seconds:.1f}s, {result.run_dir})",
        err=True,
    )
    raise typer.Exit(_exit_code_for(result))


@app.command("exec")
def exec_command(
    task_name: Annotated[str, typer.Argument(help="Task to execute.")],
    jitter: Annotated[bool, typer.Option("--jitter", help="Apply randomized_delay first.")] = False,
    manifest: ManifestOption = None,
    host: HostOption = None,
) -> None:
    """Run one task through the wrapper lifecycle; this is what units start."""
    session = _session(manifest, host)
    task = session.task(task_name)
    result = _run_task(session, task, jitter=jitter)
    raise typer.Exit(_exit_code_for(result))


@app.command()
def submit(
    task_name: Annotated[str, typer.Argument(help="Task to start now.")],
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Start a task in the background through the platform scheduler."""
    session = _session(manifest, host, backend, unit_dir)
    task = session.task(task_name)
    failures = _report_results(session.backend.submit(task))
    _exit_on_substrate_failure(failures)
    typer.echo(f"submitted {task.name} via {session.backend_name}")


@app.command("catch-up")
def catch_up(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only report decisions.")] = False,
    manifest: ManifestOption = None,
    host: HostOption = None,
) -> None:
    """Run tasks whose scheduled occurrence was missed while the machine was off."""
    session = _session(manifest, host)
    decisions = catchup.plan(
        session.automations, state_dir=session.state_dir, backend=session.backend_name
    )
    failures = 0
    for decision in decisions:
        marker = "due " if decision.due else "skip"
        typer.echo(f"{marker} {decision.task}: {decision.reason}")
        if not decision.due or dry_run:
            continue
        result = _run_task(session, session.task(decision.task), jitter=False)
        typer.echo(f"     {result.task}: {result.status} (exit {result.exit_code})")
        if result.failed:
            failures += 1
    if failures:
        raise typer.Exit(EXIT_FAILURE)


# ---------------------------------------------------------------------------
# substrate management
# ---------------------------------------------------------------------------


def _render_plan(plan: ReconcilePlan, *, show_diff: bool) -> None:
    if not plan.has_changes:
        typer.echo("no changes")
        return
    for change in plan.changed:
        typer.echo(f"{change.action}: {change.path}")
        if show_diff and change.diff:
            typer.echo(change.diff.rstrip("\n"))


@app.command()
def install(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change.")] = False,
    diff: Annotated[bool, typer.Option("--diff", help="Show unified diffs of changes.")] = False,
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Lint, render, reconcile, and enable this host's generated units."""
    session = _session(manifest, host, backend, unit_dir)
    _require_declared_host(session)
    _lint_or_exit(session, list(session.automations.selected_names()))
    tasks = session.automations.enabled_tasks()
    desired = session.backend.desired_files(session.automations, tasks)
    plan = session.backend.plan(desired)
    _render_plan(plan, show_diff=diff)
    if dry_run:
        return
    # Captured before apply(): once the files are written, this reconcile's
    # own rewrites become invisible, and a backend that reloads selectively
    # needs to know a drifted file was just put back.
    rewritten = {
        change.path.name
        for change in plan.changed
        if change.action in {backends.CREATE, backends.UPDATE}
    }
    results = list(session.backend.apply(plan, desired))
    results.extend(session.backend.reload())
    results.extend(session.backend.activate(session.automations, tasks, desired, rewritten))
    # The failure path speaks first: "installed N task(s)" after a refused
    # scheduler command would be the last line a reader sees, and false.
    _exit_on_substrate_failure(_report_results(results))
    typer.echo(f"installed {len(tasks)} task(s) into {session.backend.unit_dir}")


@app.command()
def uninstall(
    task_name: Annotated[str | None, typer.Argument(help="Task to remove.")] = None,
    remove_all: Annotated[bool, typer.Option("--all", help="Remove every managed file.")] = False,
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Remove generated units for one task, or every managed file."""
    session = _session(manifest, host, backend, unit_dir)
    if remove_all == (task_name is not None):
        raise _fail("pass exactly one of a task name or --all")
    existing = session.backend.existing_files()
    if remove_all:
        # No host guard here: --all is an explicit request that names every
        # file it removes, and it is the right tool for cleaning up a host the
        # manifest no longer declares.
        targets = sorted(existing)
    else:
        task = session.task(str(task_name))
        targets = [name for name in session.backend.task_filenames(task) if name in existing]
    if not targets:
        typer.echo("nothing to remove")
        return
    results = list(session.backend.deactivate(targets))
    for name in targets:
        (session.backend.unit_dir / name).unlink(missing_ok=True)
        typer.echo(f"removed: {session.backend.unit_dir / name}")
    results.extend(session.backend.reload())
    _exit_on_substrate_failure(_report_results(results))


@app.command()
def pause(
    task_name: Annotated[str, typer.Argument(help="Task to pause.")],
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Temporarily stop a task's schedule; the next install restores it."""
    session = _session(manifest, host, backend, unit_dir)
    task = session.task(task_name)
    _exit_on_substrate_failure(_report_results(session.backend.pause(task)))
    typer.echo(f"paused {task.name} (temporary; install re-asserts the repository state)")


@app.command()
def resume(
    task_name: Annotated[str, typer.Argument(help="Task to resume.")],
    manifest: ManifestOption = None,
    host: HostOption = None,
    backend: BackendOption = None,
    unit_dir: UnitDirOption = None,
) -> None:
    """Undo a pause."""
    session = _session(manifest, host, backend, unit_dir)
    task = session.task(task_name)
    _exit_on_substrate_failure(_report_results(session.backend.resume(task)))
    typer.echo(f"resumed {task.name}")


@app.command()
def prune(
    keep_runs: Annotated[int, typer.Option("--keep-runs", help="Runs to keep per task.")] = 50,
) -> None:
    """Delete old run records, keeping the newest per task."""
    if keep_runs < 0:
        raise _fail("--keep-runs must not be negative")
    state_dir = paths.state_dir()
    removed = records.prune(state_dir, keep_runs)
    typer.echo(f"pruned {len(removed)} run record(s) under {state_dir}")


def entrypoint() -> None:  # pragma: no cover - console-script shim
    try:
        app()
    except AutomationctlError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(EXIT_USAGE)
