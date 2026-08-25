"""The policy engine behind ``lint`` and the hard gate inside ``install``.

The tool ships the mechanism; the automations repository ships the opinions.
The forbidden-argv deny-list lives in ``manifest.toml``, and a hit is an error
unless the task or its runner carries an explicit ``allow_full_access = true``
marker — a visible, greppable, committed decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import Automations, load_prompt, resolve_repo_path
from .errors import AutomationctlError, ScheduleError, TemplateError
from .locks import lock_path
from .records import utcnow
from .schedule import to_launchd, to_systemd
from .spec import Runner, TaskSpec, effective_on_failure
from .template import PROMPT_PLACEHOLDER, build_invocation, builtin_values

ERROR = "error"
WARNING = "warning"

LINT_RUN_DIR = "<run-dir>"

#: Names automationctl generates agents for; a task may not claim them.
RESERVED_TASK_NAMES = frozenset({"catchup"})


@dataclass(frozen=True)
class Diagnostic:
    """One lint finding."""

    level: str
    message: str
    task: str | None = None
    path: Path | None = None

    def render(self) -> str:
        where = self.task or (str(self.path) if self.path is not None else "manifest")
        return f"{self.level}: {where}: {self.message}"


@dataclass(frozen=True)
class LintReport:
    """The result of one lint pass."""

    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.level == ERROR)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.level == WARNING)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> list[str]:
        return [item.render() for item in self.diagnostics]


def _check_schedule(task: TaskSpec, backend: str, out: list[Diagnostic]) -> None:
    if task.schedule is None:
        return
    try:
        if backend == "systemd":
            to_systemd(task.schedule)
        elif backend == "launchd":
            to_launchd(task.schedule)
    except ScheduleError as exc:
        out.append(
            Diagnostic(
                ERROR,
                f"schedule is not expressible by the {backend} backend: {exc}",
                task.name,
                task.path,
            )
        )


def _check_exclusivity(task: TaskSpec, out: list[Diagnostic]) -> None:
    if task.command is None and task.runner is None:
        out.append(
            Diagnostic(ERROR, "exactly one of command or runner is required", task.name, task.path)
        )
    if task.command is not None and task.runner is not None:
        out.append(
            Diagnostic(ERROR, "command and runner are mutually exclusive", task.name, task.path)
        )
    if task.command is not None and not task.command:
        out.append(Diagnostic(ERROR, "command must not be empty", task.name, task.path))
    if task.prompt is not None and task.prompt_file is not None:
        out.append(
            Diagnostic(ERROR, "prompt and prompt_file are mutually exclusive", task.name, task.path)
        )
    if task.runner is None and (task.prompt is not None or task.prompt_file is not None):
        out.append(
            Diagnostic(ERROR, "prompt and prompt_file require a runner", task.name, task.path)
        )
    if task.runner is not None and task.prompt is None and task.prompt_file is None:
        out.append(Diagnostic(ERROR, "runner requires prompt or prompt_file", task.name, task.path))
    if task.summary_cmd is not None and not task.summary_cmd:
        out.append(Diagnostic(ERROR, "summary_cmd must not be empty", task.name, task.path))


def _resolve_runner(
    automations: Automations, task: TaskSpec, out: list[Diagnostic]
) -> Runner | None:
    if task.runner is None:
        return None
    runner = automations.runners.get(task.runner)
    if runner is None:
        known = ", ".join(sorted(automations.runners)) or "none"
        out.append(
            Diagnostic(
                ERROR, f"unknown runner: {task.runner} (known: {known})", task.name, task.path
            )
        )
        return None
    if runner.stdin == PROMPT_PLACEHOLDER and any("{prompt}" in item for item in runner.argv):
        out.append(
            Diagnostic(
                ERROR,
                f"runner {runner.name} delivers the prompt on stdin and in argv; pick one",
                task.name,
                task.path,
            )
        )
    return runner


def _check_notify(automations: Automations, task: TaskSpec, out: list[Diagnostic]) -> None:
    for reference in effective_on_failure(automations.manifest, task):
        if not reference.startswith("notify:"):
            out.append(
                Diagnostic(
                    ERROR,
                    f"unsupported on_failure entry: {reference} (expected notify:<transport>)",
                    task.name,
                    task.path,
                )
            )
            continue
        name = reference.removeprefix("notify:")
        if name not in automations.manifest.notify:
            out.append(
                Diagnostic(
                    ERROR,
                    f"on_failure references undefined notify transport: {name}",
                    task.name,
                    task.path,
                )
            )


def _check_lock(task: TaskSpec, out: list[Diagnostic]) -> None:
    if task.lock is None:
        return
    try:
        lock_path(Path("/"), task.lock)
    except AutomationctlError as exc:
        out.append(Diagnostic(ERROR, str(exc), task.name, task.path))


def _check_runtime_path(
    value: str,
    field: str,
    out: list[Diagnostic],
    *,
    task: TaskSpec | None = None,
    path: Path,
) -> None:
    """Require paths whose runtime meaning must not depend on process cwd."""
    if paths.expand(value).is_absolute():
        return
    out.append(
        Diagnostic(
            ERROR,
            f"{field} must be an absolute path or a tilde-expanded home path: {value!r}",
            task.name if task is not None else None,
            path,
        )
    )


def _check_host_runtime_paths(automations: Automations, out: list[Diagnostic]) -> None:
    config = automations.host_config
    for value in config.path_prepend:
        _check_runtime_path(value, "path_prepend", out, path=automations.manifest.path)
    for value in config.env_files:
        _check_runtime_path(value, "env_files", out, path=automations.manifest.path)


def _check_task_runtime_paths(task: TaskSpec, out: list[Diagnostic]) -> None:
    if task.cwd is not None:
        _check_runtime_path(task.cwd, "cwd", out, task=task, path=task.path)
    for value in task.env_files:
        _check_runtime_path(value, "env_files", out, task=task, path=task.path)


def _check_argv(
    automations: Automations, task: TaskSpec, runner: Runner | None, out: list[Diagnostic]
) -> None:
    prompt: str | None = None
    if task.prompt is not None:
        prompt = task.prompt
    elif task.prompt_file is not None:
        try:
            prompt = load_prompt(automations.manifest, task)
        except AutomationctlError as exc:
            out.append(Diagnostic(ERROR, str(exc), task.name, task.path))
            return

    values = builtin_values(
        task=task.name, hostname=automations.host, run_dir=LINT_RUN_DIR, now=utcnow()
    )
    try:
        invocation = build_invocation(task, runner, prompt, values)
    except TemplateError as exc:
        out.append(Diagnostic(ERROR, str(exc), task.name, task.path))
        return

    allowed = task.allow_full_access or (runner is not None and runner.allow_full_access)
    forbidden = automations.manifest.lint.forbidden_argv
    for needle in forbidden:
        if not any(needle in item for item in invocation.argv):
            continue
        if allowed:
            out.append(
                Diagnostic(
                    WARNING,
                    f"expanded argv contains {needle!r}, permitted by allow_full_access",
                    task.name,
                    task.path,
                )
            )
        else:
            out.append(
                Diagnostic(
                    ERROR,
                    f"expanded argv contains forbidden entry {needle!r}; "
                    "set allow_full_access = true to permit it deliberately",
                    task.name,
                    task.path,
                )
            )


def _check_name(task: TaskSpec, out: list[Diagnostic]) -> None:
    """Reject names the generated substrate has already claimed.

    The character rule is enforced at load time, in :mod:`spec`; what is left
    here is the reserved-word rule, which belongs to the backends.
    """
    if task.name in RESERVED_TASK_NAMES:
        out.append(
            Diagnostic(
                ERROR,
                f"reserved task name: {task.name!r}; automationctl generates an agent of that "
                "name, so a task using it would be silently replaced",
                task.name,
                task.path,
            )
        )


def lint_task(automations: Automations, task: TaskSpec, backend: str) -> list[Diagnostic]:
    """Run every check against one task spec."""
    out: list[Diagnostic] = []
    _check_name(task, out)
    _check_exclusivity(task, out)
    runner = _resolve_runner(automations, task, out)
    if task.prompt_file is not None:
        path = resolve_repo_path(automations.manifest, task.prompt_file)
        if not path.exists():
            out.append(Diagnostic(ERROR, f"prompt_file not found: {path}", task.name, task.path))
    _check_schedule(task, backend, out)
    _check_notify(automations, task, out)
    _check_lock(task, out)
    _check_task_runtime_paths(task, out)
    if task.command is not None or runner is not None:
        _check_argv(automations, task, runner, out)
    return out


def lint(
    automations: Automations,
    *,
    backend: str,
    tasks: Sequence[str] | None = None,
) -> LintReport:
    """Lint the manifest, host selection, and the requested tasks.

    ``tasks`` narrows the scan to named specs, and with it the load errors
    reported. That scoping is what lets ``install`` gate on this host's
    selection alone: a spec written for another host — a launchd-only escape
    hatch, say — is not this host's problem to fix before it can install.
    """
    out: list[Diagnostic] = []
    scope = None if tasks is None else set(tasks)
    _check_host_runtime_paths(automations, out)
    for error in automations.errors:
        if scope is None or error.path.stem in scope:
            out.append(Diagnostic(ERROR, error.message, error.path.stem, error.path))

    if not automations.host_declared:
        out.append(
            Diagnostic(
                WARNING,
                f"host {automations.host!r} is not declared in the manifest; "
                "no tasks are selected for it",
                None,
                automations.manifest.path,
            )
        )

    failed = {error.path.stem for error in automations.errors}
    for name in automations.selected_names():
        if name not in automations.tasks and name not in failed:
            out.append(
                Diagnostic(
                    ERROR,
                    f"host {automations.host} selects unknown task: {name}",
                    None,
                    automations.manifest.path,
                )
            )

    if tasks is None:
        targets = [automations.tasks[name] for name in sorted(automations.tasks)]
    else:
        targets = []
        for name in tasks:
            task = automations.tasks.get(name)
            if task is None:
                if name not in failed:
                    out.append(
                        Diagnostic(ERROR, f"unknown task: {name}", name, automations.manifest.path)
                    )
                continue
            targets.append(task)

    for task in targets:
        out.extend(lint_task(automations, task, backend))

    return LintReport(diagnostics=tuple(out))
