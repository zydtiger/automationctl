"""Read-only host probes.

The two predictable first-day failures of an unattended automation system are
PATH (agent CLIs invisible to non-interactive contexts) and environment
(missing proxy or token variables). ``doctor`` checks both before any timer
ever fires, and never mutates anything.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .backends import Backend, HealthCheck
from .config import Automations, load_prompt
from .errors import AutomationctlError
from .records import utcnow
from .spec import TaskSpec
from .template import build_invocation, builtin_values
from .wrapper import DEFAULT_PATH


@dataclass(frozen=True)
class DoctorReport:
    """Everything ``doctor`` observed."""

    checks: tuple[HealthCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def effective_path(automations: Automations) -> str:
    """Return the PATH the wrapper would build for this host."""
    prepend = [str(paths.expand(item)) for item in automations.host_config.path_prepend]
    return os.pathsep.join([*prepend, DEFAULT_PATH]) if prepend else DEFAULT_PATH


def _programs(automations: Automations, task: TaskSpec) -> list[str]:
    values = builtin_values(
        task=task.name, hostname=automations.host, run_dir="<run-dir>", now=utcnow()
    )
    runner = automations.runners.get(task.runner) if task.runner is not None else None
    try:
        prompt = load_prompt(automations.manifest, task)
        invocation = build_invocation(task, runner, prompt, values)
    except AutomationctlError:
        return []
    programs = [invocation.argv[0]] if invocation.argv else []
    if task.summary_cmd:
        programs.append(task.summary_cmd[0])
    return programs


def run(
    automations: Automations,
    backend: Backend,
    *,
    env: Mapping[str, str],
    state_dir: Path,
) -> DoctorReport:
    """Probe the backend, the manifest, the environment, and required binaries."""
    checks: list[HealthCheck] = list(backend.health())

    manifest = automations.manifest
    tasks = automations.enabled_tasks()
    checks.append(
        HealthCheck(
            "manifest",
            True,
            f"{manifest.path} (schema {manifest.schema_version}, host {automations.host}, "
            f"{len(tasks)} enabled task(s))",
        )
    )
    checks.append(
        HealthCheck(
            "host",
            automations.host_declared,
            "declared in the manifest"
            if automations.host_declared
            else f"host {automations.host!r} has no [hosts.*] section",
        )
    )
    if automations.errors:
        for error in automations.errors:
            checks.append(HealthCheck("spec", False, str(error)))

    for item in (*automations.host_config.env_files, *_task_env_files(tasks)):
        path = paths.expand(item)
        checks.append(
            HealthCheck(
                "env file",
                os.access(path, os.R_OK),
                f"{path} readable" if os.access(path, os.R_OK) else f"{path} missing or unreadable",
            )
        )

    checks.append(
        HealthCheck(
            "state dir",
            state_dir.is_dir(),
            f"{state_dir}" if state_dir.is_dir() else f"{state_dir} does not exist yet",
        )
    )

    search_path = effective_path(automations)
    seen: set[str] = set()
    for task in tasks:
        for program in _programs(automations, task):
            if program in seen:
                continue
            seen.add(program)
            found = program if os.sep in program else shutil.which(program, path=search_path)
            checks.append(
                HealthCheck(
                    "binary",
                    found is not None,
                    f"{program} -> {found}" if found else f"{program} not found on PATH",
                )
            )
        if task.cwd:
            directory = paths.expand(task.cwd)
            checks.append(
                HealthCheck(
                    "cwd",
                    directory.is_dir(),
                    f"{task.name}: {directory}"
                    if directory.is_dir()
                    else f"{task.name}: {directory} does not exist",
                )
            )

    return DoctorReport(checks=tuple(checks))


def _task_env_files(tasks: list[TaskSpec]) -> list[str]:
    out: list[str] = []
    for task in tasks:
        out.extend(task.env_files)
    return out
