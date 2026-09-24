"""Read-only diagnostics for installed tasks and their scheduler substrate."""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import catchup, paths, records
from .backends import Backend, HealthCheck
from .errors import AutomationctlError
from .spec import MachinePolicy, TaskSpec
from .template import build_invocation, builtin_values
from .wrapper import build_env


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[HealthCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _state_dir_check(state_dir: Path) -> HealthCheck:
    if os.path.lexists(state_dir):
        writable = state_dir.is_dir() and os.access(state_dir, os.W_OK | os.X_OK)
        return HealthCheck(
            "state dir",
            writable,
            f"{state_dir} writable and searchable"
            if writable
            else f"{state_dir} is not a writable directory",
        )
    parent = state_dir.parent
    while not os.path.lexists(parent) and parent != parent.parent:
        parent = parent.parent
    writable = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    return HealthCheck(
        "state dir",
        writable,
        f"{state_dir} is absent; lazy creation is available under {parent}"
        if writable
        else f"{state_dir} cannot be created under {parent}",
    )


def _catchup_check(
    tasks: Sequence[TaskSpec], policy: MachinePolicy, backend: Backend
) -> HealthCheck:
    if not catchup.triggers_wanted(tasks):
        return HealthCheck("catch-up triggers", True, "not needed")
    desired = backend.desired_catchup_files(policy)
    for name, content in desired.items():
        path = backend.unit_dir / name
        try:
            installed = path.read_text(encoding="utf-8")
        except OSError:
            return HealthCheck("catch-up triggers", False, f"{path} is missing")
        if installed != content:
            return HealthCheck("catch-up triggers", False, f"{path} is stale")
    return HealthCheck("catch-up triggers", True, "installed and current")


def run(
    tasks: Sequence[TaskSpec],
    errors: Sequence[Exception],
    backend: Backend,
    *,
    policy: MachinePolicy,
    state_dir: Path,
    env: Mapping[str, str],
) -> DoctorReport:
    checks = list(backend.health())
    checks.extend(HealthCheck("spec", False, str(error)) for error in errors)
    checks.append(_state_dir_check(state_dir))
    catchup_checks = backend.catchup_health(tasks, policy)
    checks.extend(catchup_checks or [_catchup_check(tasks, policy, backend)])
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        runtime = build_env(task, env, Path("<doctor-run>"), strict=False)
        for item in task.env_files:
            path = paths.expand(item)
            checks.append(
                HealthCheck(
                    "env file",
                    path.is_file() and os.access(path, os.R_OK),
                    f"{task.name}: {path}",
                )
            )
        cwd = paths.expand(task.cwd) if task.cwd else Path(runtime["HOME"])
        checks.append(HealthCheck("cwd", cwd.is_dir(), f"{task.name}: {cwd}"))
        try:
            program = build_invocation(
                task,
                builtin_values(
                    task.name,
                    socket.gethostname().split(".")[0],
                    "<doctor-run>",
                    records.utcnow(),
                ),
            ).argv[0]
        except AutomationctlError as exc:
            checks.append(HealthCheck("binary", False, f"{task.name}: cannot build command: {exc}"))
            continue
        search_path = runtime["PATH"]
        if (program, search_path) in seen:
            continue
        seen.add((program, search_path))
        found = (
            Path(program)
            if os.sep in program
            else Path(shutil.which(program, path=search_path) or "")
        )
        checks.append(
            HealthCheck(
                "binary",
                bool(found) and found.is_file() and os.access(found, os.X_OK),
                f"{task.name}: {program} -> {found or 'not found'}",
            )
        )
    return DoctorReport(tuple(checks))
