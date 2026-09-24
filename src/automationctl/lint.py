"""Schema-adjacent lint for standalone task definitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths
from .errors import AutomationctlError, ScheduleError, TemplateError
from .locks import lock_path
from .schedule import to_launchd, to_systemd
from .spec import MachinePolicy, TaskSpec
from .template import build_invocation, builtin_values

ERROR = "error"
WARNING = "warning"
RESERVED_TASK_NAMES = frozenset({"catchup"})


@dataclass(frozen=True)
class Diagnostic:
    level: str
    message: str
    task: str | None = None
    path: Path | None = None

    def render(self) -> str:
        return f"{self.level}: {self.task or self.path or 'policy'}: {self.message}"


@dataclass(frozen=True)
class LintReport:
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.level == ERROR)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> list[str]:
        return [item.render() for item in self.diagnostics]


def _runtime_path(value: str, field: str, task: TaskSpec, out: list[Diagnostic]) -> None:
    if not paths.expand(value).is_absolute():
        out.append(
            Diagnostic(
                ERROR,
                f"{field} must be an absolute or home-relative path: {value!r}",
                task.name,
                task.path,
            )
        )


def lint_task(task: TaskSpec, policy: MachinePolicy, backend: str) -> LintReport:
    out: list[Diagnostic] = []
    if task.name in RESERVED_TASK_NAMES:
        out.append(Diagnostic(ERROR, "reserved task name", task.name, task.path))
    if task.stdin_file is not None:
        out.append(
            Diagnostic(
                ERROR,
                "stdin_file is a source-only field and must be materialized before installation",
                task.name,
                task.path,
            )
        )
    if task.schedule is not None:
        try:
            (to_systemd if backend == "systemd" else to_launchd)(task.schedule)
        except ScheduleError as exc:
            out.append(
                Diagnostic(
                    ERROR, f"schedule is not expressible by {backend}: {exc}", task.name, task.path
                )
            )
    for field, values in (("env_files", task.env_files), ("path_prepend", task.path_prepend)):
        for value in values:
            _runtime_path(value, field, task, out)
    if task.cwd is not None:
        _runtime_path(task.cwd, "cwd", task, out)
    if task.lock is not None:
        try:
            lock_path(Path("/"), task.lock)
        except AutomationctlError as exc:
            out.append(Diagnostic(ERROR, str(exc), task.name, task.path))
    for reference in task.on_failure:
        if (
            not reference.startswith("notify:")
            or reference.removeprefix("notify:") not in task.notify
        ):
            out.append(
                Diagnostic(
                    ERROR,
                    f"on_failure references undefined task notification: {reference}",
                    task.name,
                    task.path,
                )
            )
    try:
        invocation = build_invocation(
            task, builtin_values(task.name, "hostname", "<run-dir>", datetime.now())
        )
    except TemplateError as exc:
        out.append(Diagnostic(ERROR, str(exc), task.name, task.path))
    else:
        for needle in policy.lint.forbidden_argv:
            if any(needle in item for item in invocation.argv):
                level = WARNING if task.allow_full_access else ERROR
                ending = (
                    "permitted by allow_full_access"
                    if task.allow_full_access
                    else "set allow_full_access = true to permit it deliberately"
                )
                out.append(
                    Diagnostic(
                        level,
                        f"expanded argv contains forbidden entry {needle!r}; {ending}",
                        task.name,
                        task.path,
                    )
                )
    return LintReport(tuple(out))
