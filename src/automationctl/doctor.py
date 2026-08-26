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
from .wrapper import DEFAULT_PATH, build_env


@dataclass(frozen=True)
class DoctorReport:
    """Everything ``doctor`` observed."""

    checks: tuple[HealthCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def effective_path(automations: Automations, task: TaskSpec, env: Mapping[str, str]) -> str:
    """Return the PATH this task's child would actually receive.

    It has to come from the same builder the wrapper uses, because an env file
    is allowed to set PATH — and if doctor guesses instead, it reports on an
    environment no run will ever have.
    """
    probe = build_env(automations, task, env, Path("<doctor>"), strict=False)
    return probe.get("PATH", DEFAULT_PATH)


def _resolve(program: str, search_path: str) -> str | None:
    """Resolve a program the way the wrapper will, absolute paths included."""
    if os.sep in program:
        return program if os.access(program, os.X_OK) else None
    return shutil.which(program, path=search_path)


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


def _state_dir_check(state_dir: Path) -> HealthCheck:
    """Report whether runtime state can be written without creating anything."""
    if os.path.lexists(state_dir):
        if not state_dir.is_dir():
            return HealthCheck("state dir", False, f"{state_dir} exists but is not a directory")
        writable = os.access(state_dir, os.W_OK | os.X_OK)
        return HealthCheck(
            "state dir",
            writable,
            (
                f"{state_dir} writable and searchable"
                if writable
                else f"{state_dir} is not writable and searchable"
            ),
        )

    parent = state_dir.parent
    while not os.path.lexists(parent) and parent != parent.parent:
        parent = parent.parent
    writable = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    return HealthCheck(
        "state dir",
        writable,
        (
            f"{state_dir} is absent; lazy creation is available under writable parent {parent}"
            if writable
            else f"{state_dir} is absent and cannot be created under {parent}"
        ),
    )


def run(
    automations: Automations,
    backend: Backend,
    *,
    env: Mapping[str, str],
    state_dir: Path,
) -> DoctorReport:
    """Probe the backend, the manifest, the environment, and required binaries."""
    manifest = automations.manifest
    tasks = automations.enabled_tasks()

    checks: list[HealthCheck] = list(backend.health())
    checks.extend(backend.catchup_health(automations, tasks))

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

    checks.append(_state_dir_check(state_dir))

    # Keyed on the search path as well as the name: PATH is per task now, so
    # the same program can resolve differently — or not at all — for two tasks.
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        search_path = effective_path(automations, task, env)
        for program in _programs(automations, task):
            if (program, search_path) in seen:
                continue
            seen.add((program, search_path))
            found = _resolve(program, search_path)
            # Attributed to the task, because two tasks can probe the same
            # program name against different PATHs and get different answers.
            checks.append(
                HealthCheck(
                    "binary",
                    found is not None,
                    f"{task.name}: {program} -> {found}"
                    if found
                    else f"{task.name}: {program} not found or not executable",
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
