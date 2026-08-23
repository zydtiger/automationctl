"""Discovery and loading of an automations repository.

An automations repository is a directory containing ``manifest.toml``, an
optional ``runners.toml``, and a ``tasks/`` directory of task specs. Task
files that fail to parse are collected rather than raised so that ``lint`` can
report every problem at once.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .errors import ConfigError
from .spec import (
    HostConfig,
    Manifest,
    Runner,
    TaskSpec,
    load_toml,
    parse_manifest,
    parse_runners,
    parse_task,
)


@dataclass(frozen=True)
class LoadError:
    """A task spec that could not be loaded."""

    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class Automations:
    """Everything loaded from one automations repository, for one host."""

    manifest: Manifest
    runners: Mapping[str, Runner]
    tasks: Mapping[str, TaskSpec]
    errors: tuple[LoadError, ...]
    host: str
    runners_path: Path

    @property
    def root(self) -> Path:
        return self.manifest.root

    @property
    def host_config(self) -> HostConfig:
        existing = self.manifest.hosts.get(self.host)
        return existing if existing is not None else HostConfig(name=self.host)

    @property
    def host_declared(self) -> bool:
        return self.host in self.manifest.hosts

    def selected_names(self) -> tuple[str, ...]:
        """Task names this host selects, in manifest order."""
        return self.host_config.tasks

    def selected_tasks(self) -> list[TaskSpec]:
        """Loaded specs this host selects, skipping names that do not resolve."""
        return [self.tasks[name] for name in self.selected_names() if name in self.tasks]

    def enabled_tasks(self) -> list[TaskSpec]:
        """Selected specs that are not marked ``disabled``."""
        return [task for task in self.selected_tasks() if not task.disabled]

    def require_task(self, name: str) -> TaskSpec:
        task = self.tasks.get(name)
        if task is None:
            failed = [error for error in self.errors if error.path.stem == name]
            if failed:
                raise ConfigError(failed[0].message, failed[0].path)
            known = ", ".join(sorted(self.tasks)) or "none"
            raise ConfigError(f"unknown task: {name} (known tasks: {known})", self.manifest.path)
        return task


def default_host() -> str:
    """Return the short hostname, which is the default host key."""
    return socket.gethostname().split(".")[0]


def load_prompt(manifest: Manifest, task: TaskSpec) -> str | None:
    """Return the task's prompt text, reading ``prompt_file`` when configured."""
    if task.prompt is not None:
        return task.prompt
    if task.prompt_file is None:
        return None
    path = resolve_repo_path(manifest, task.prompt_file)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read prompt_file: {exc}", task.path) from exc


def resolve_repo_path(manifest: Manifest, value: str) -> Path:
    """Resolve a path relative to the automations repository root."""
    expanded = paths.expand(value)
    if expanded.is_absolute():
        return expanded
    return manifest.root / expanded


def load(
    manifest_path: Path | None = None,
    host: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Automations:
    """Load the manifest, runner table, and every task spec."""
    values = os.environ if env is None else env
    path = manifest_path if manifest_path is not None else paths.default_manifest_path(values)
    # Generated units and run records embed this path, so it must be absolute.
    path = paths.expand(path).resolve()
    manifest = parse_manifest(load_toml(path), path)

    runners_path = manifest.root / "runners.toml"
    runners: dict[str, Runner] = {}
    if runners_path.exists():
        runners = parse_runners(load_toml(runners_path), runners_path)

    tasks: dict[str, TaskSpec] = {}
    errors: list[LoadError] = []
    tasks_dir = manifest.root / "tasks"
    if tasks_dir.is_dir():
        for spec_path in sorted(tasks_dir.glob("*.toml")):
            name = spec_path.stem
            try:
                tasks[name] = parse_task(load_toml(spec_path), spec_path, name)
            except ConfigError as exc:
                errors.append(LoadError(path=spec_path, message=exc.message))

    return Automations(
        manifest=manifest,
        runners=runners,
        tasks=tasks,
        errors=tuple(errors),
        host=host if host is not None else default_host(),
        runners_path=runners_path,
    )
