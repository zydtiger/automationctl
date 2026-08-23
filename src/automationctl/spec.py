"""Manifest, runner, and task-spec models with schema validation.

Everything the tool understands about user configuration is declared here.
Unknown keys are rejected rather than ignored: a silently dropped field in an
unattended automation is a silent behaviour change.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, ScheduleError
from .schedule import Schedule, parse_duration
from .schedule import parse as parse_schedule

SCHEMA_VERSION = 1

TASK_KEYS = frozenset(
    {
        "schema_version",
        "description",
        "command",
        "runner",
        "prompt",
        "prompt_file",
        "cwd",
        "env",
        "env_files",
        "schedule",
        "timeout",
        "randomized_delay",
        "persistent",
        "lock",
        "summary_cmd",
        "on_failure",
        "allow_full_access",
        "disabled",
    }
)
MANIFEST_KEYS = frozenset({"schema_version", "defaults", "hosts", "notify", "lint"})
DEFAULTS_KEYS = frozenset({"timeout", "on_failure", "randomized_delay", "persistent"})
HOST_KEYS = frozenset({"tasks", "path_prepend", "env_files"})
NOTIFY_KEYS = frozenset({"type", "url_env", "command", "title"})
LINT_KEYS = frozenset({"forbidden_argv"})
RUNNERS_KEYS = frozenset({"schema_version", "runners"})
RUNNER_KEYS = frozenset({"argv", "stdin", "allow_full_access", "description"})


# --------------------------------------------------------------------------
# typed TOML accessors
# --------------------------------------------------------------------------


def load_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file into a plain dictionary, reporting errors with the path."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError("file not found", path) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}", path) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read file: {exc}", path) from exc
    return data


def check_unknown(
    table: Mapping[str, Any], allowed: frozenset[str], path: Path, where: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown field(s): {', '.join(unknown)}", path)


def check_schema_version(table: Mapping[str, Any], path: Path, *, required: bool) -> int:
    value = table.get("schema_version")
    if value is None:
        if required:
            raise ConfigError("missing required field: schema_version", path)
        return SCHEMA_VERSION
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError("schema_version must be an integer", path)
    if value > SCHEMA_VERSION:
        raise ConfigError(
            f"schema_version {value} is newer than this tool understands ({SCHEMA_VERSION})",
            path,
        )
    if value < 1:
        raise ConfigError(f"unsupported schema_version: {value}", path)
    return value


def _as_str(value: Any, key: str, path: Path) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string", path)
    return value


def opt_str(table: Mapping[str, Any], key: str, path: Path) -> str | None:
    if key not in table:
        return None
    return _as_str(table[key], key, path)


def req_str(table: Mapping[str, Any], key: str, path: Path) -> str:
    if key not in table:
        raise ConfigError(f"missing required field: {key}", path)
    return _as_str(table[key], key, path)


def opt_bool(table: Mapping[str, Any], key: str, path: Path) -> bool | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean", path)
    return value


def opt_str_tuple(table: Mapping[str, Any], key: str, path: Path) -> tuple[str, ...] | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConfigError(f"{key} must be a list of strings", path)
    return tuple(_as_str(item, f"{key}[]", path) for item in value)


def opt_str_map(table: Mapping[str, Any], key: str, path: Path) -> dict[str, str] | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a table of strings", path)
    return {str(name): _as_str(item, f"{key}.{name}", path) for name, item in value.items()}


def opt_duration(table: Mapping[str, Any], key: str, path: Path) -> int | None:
    text = opt_str(table, key, path)
    if text is None:
        return None
    try:
        return parse_duration(text)
    except ScheduleError as exc:
        raise ConfigError(str(exc), path) from exc


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Runner:
    """A data-only argv expansion table entry."""

    name: str
    argv: tuple[str, ...]
    stdin: str | None = None
    allow_full_access: bool = False
    description: str | None = None


@dataclass(frozen=True)
class NotifyTransport:
    """A notification transport declared in the manifest."""

    name: str
    kind: str
    url_env: str | None = None
    command: tuple[str, ...] = ()
    title: str | None = None


@dataclass(frozen=True)
class HostConfig:
    """Per-host task selection and environment contribution."""

    name: str
    tasks: tuple[str, ...] = ()
    path_prepend: tuple[str, ...] = ()
    env_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Defaults:
    """Manifest-wide fallbacks for task fields."""

    timeout_seconds: int | None = None
    on_failure: tuple[str, ...] = ()
    randomized_delay_seconds: int | None = None
    persistent: bool | None = None


@dataclass(frozen=True)
class LintPolicy:
    """The user-owned deny-list scanned against expanded argv."""

    forbidden_argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    """The parsed ``manifest.toml`` plus the automations root it lives in."""

    path: Path
    root: Path
    schema_version: int
    defaults: Defaults = field(default_factory=Defaults)
    hosts: Mapping[str, HostConfig] = field(default_factory=dict)
    notify: Mapping[str, NotifyTransport] = field(default_factory=dict)
    lint: LintPolicy = field(default_factory=LintPolicy)


@dataclass(frozen=True)
class TaskSpec:
    """One declarative task."""

    name: str
    path: Path
    description: str
    command: tuple[str, ...] | None = None
    runner: str | None = None
    prompt: str | None = None
    prompt_file: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    env_files: tuple[str, ...] = ()
    schedule: Schedule | None = None
    timeout_seconds: int | None = None
    randomized_delay_seconds: int | None = None
    persistent: bool | None = None
    lock: str | None = None
    summary_cmd: tuple[str, ...] | None = None
    on_failure: tuple[str, ...] | None = None
    allow_full_access: bool = False
    disabled: bool = False

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for run records."""
        return {
            "name": self.name,
            "path": str(self.path),
            "description": self.description,
            "command": list(self.command) if self.command is not None else None,
            "runner": self.runner,
            "prompt_file": self.prompt_file,
            "has_inline_prompt": self.prompt is not None,
            "cwd": self.cwd,
            "env_keys": sorted(self.env),
            "env_files": list(self.env_files),
            "schedule": self.schedule.text if self.schedule is not None else None,
            "timeout_seconds": self.timeout_seconds,
            "lock": self.lock,
            "summary_cmd": list(self.summary_cmd) if self.summary_cmd is not None else None,
            "allow_full_access": self.allow_full_access,
            "disabled": self.disabled,
        }


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_manifest(data: Mapping[str, Any], path: Path) -> Manifest:
    check_unknown(data, MANIFEST_KEYS, path, "manifest")
    version = check_schema_version(data, path, required=True)

    defaults_table = data.get("defaults", {})
    if not isinstance(defaults_table, Mapping):
        raise ConfigError("[defaults] must be a table", path)
    check_unknown(defaults_table, DEFAULTS_KEYS, path, "[defaults]")
    defaults = Defaults(
        timeout_seconds=opt_duration(defaults_table, "timeout", path),
        on_failure=opt_str_tuple(defaults_table, "on_failure", path) or (),
        randomized_delay_seconds=opt_duration(defaults_table, "randomized_delay", path),
        persistent=opt_bool(defaults_table, "persistent", path),
    )

    hosts_table = data.get("hosts", {})
    if not isinstance(hosts_table, Mapping):
        raise ConfigError("[hosts] must be a table", path)
    hosts: dict[str, HostConfig] = {}
    for name, raw in hosts_table.items():
        if not isinstance(raw, Mapping):
            raise ConfigError(f"[hosts.{name}] must be a table", path)
        check_unknown(raw, HOST_KEYS, path, f"[hosts.{name}]")
        hosts[str(name)] = HostConfig(
            name=str(name),
            tasks=opt_str_tuple(raw, "tasks", path) or (),
            path_prepend=opt_str_tuple(raw, "path_prepend", path) or (),
            env_files=opt_str_tuple(raw, "env_files", path) or (),
        )

    notify_table = data.get("notify", {})
    if not isinstance(notify_table, Mapping):
        raise ConfigError("[notify] must be a table", path)
    transports: dict[str, NotifyTransport] = {}
    for name, raw in notify_table.items():
        if not isinstance(raw, Mapping):
            raise ConfigError(f"[notify.{name}] must be a table", path)
        check_unknown(raw, NOTIFY_KEYS, path, f"[notify.{name}]")
        command = opt_str_tuple(raw, "command", path) or ()
        url_env = opt_str(raw, "url_env", path)
        kind = opt_str(raw, "type", path)
        if kind is None:
            kind = "command" if command else "ntfy"
        if kind not in {"ntfy", "command"}:
            raise ConfigError(f"[notify.{name}]: unknown transport type: {kind!r}", path)
        if kind == "ntfy" and not url_env:
            raise ConfigError(f"[notify.{name}]: ntfy transport requires url_env", path)
        if kind == "command" and not command:
            raise ConfigError(f"[notify.{name}]: command transport requires command", path)
        transports[str(name)] = NotifyTransport(
            name=str(name),
            kind=kind,
            url_env=url_env,
            command=command,
            title=opt_str(raw, "title", path),
        )

    lint_table = data.get("lint", {})
    if not isinstance(lint_table, Mapping):
        raise ConfigError("[lint] must be a table", path)
    check_unknown(lint_table, LINT_KEYS, path, "[lint]")
    lint = LintPolicy(forbidden_argv=opt_str_tuple(lint_table, "forbidden_argv", path) or ())

    return Manifest(
        path=path,
        root=path.parent,
        schema_version=version,
        defaults=defaults,
        hosts=hosts,
        notify=transports,
        lint=lint,
    )


def parse_runners(data: Mapping[str, Any], path: Path) -> dict[str, Runner]:
    check_unknown(data, RUNNERS_KEYS, path, "runners")
    check_schema_version(data, path, required=True)
    table = data.get("runners", {})
    if not isinstance(table, Mapping):
        raise ConfigError("[runners] must be a table", path)
    runners: dict[str, Runner] = {}
    for name, raw in table.items():
        if not isinstance(raw, Mapping):
            raise ConfigError(f"[runners.{name}] must be a table", path)
        check_unknown(raw, RUNNER_KEYS, path, f"[runners.{name}]")
        argv = opt_str_tuple(raw, "argv", path)
        if not argv:
            raise ConfigError(f"[runners.{name}]: argv is required and must be non-empty", path)
        stdin = opt_str(raw, "stdin", path)
        if stdin is not None and stdin != "prompt":
            raise ConfigError(f'[runners.{name}]: stdin must be "prompt" when set', path)
        runners[str(name)] = Runner(
            name=str(name),
            argv=argv,
            stdin=stdin,
            allow_full_access=opt_bool(raw, "allow_full_access", path) or False,
            description=opt_str(raw, "description", path),
        )
    return runners


def parse_task(data: Mapping[str, Any], path: Path, name: str) -> TaskSpec:
    check_unknown(data, TASK_KEYS, path, f"task {name}")
    check_schema_version(data, path, required=False)
    schedule_value = data.get("schedule")
    schedule: Schedule | None = None
    if schedule_value is not None:
        if not isinstance(schedule_value, str | Mapping):
            raise ConfigError("schedule must be a string or a per-backend table", path)
        try:
            schedule = parse_schedule(schedule_value)
        except ScheduleError as exc:
            raise ConfigError(str(exc), path) from exc
    return TaskSpec(
        name=name,
        path=path,
        description=req_str(data, "description", path),
        command=opt_str_tuple(data, "command", path),
        runner=opt_str(data, "runner", path),
        prompt=opt_str(data, "prompt", path),
        prompt_file=opt_str(data, "prompt_file", path),
        cwd=opt_str(data, "cwd", path),
        env=opt_str_map(data, "env", path) or {},
        env_files=opt_str_tuple(data, "env_files", path) or (),
        schedule=schedule,
        timeout_seconds=opt_duration(data, "timeout", path),
        randomized_delay_seconds=opt_duration(data, "randomized_delay", path),
        persistent=opt_bool(data, "persistent", path),
        lock=opt_str(data, "lock", path),
        summary_cmd=opt_str_tuple(data, "summary_cmd", path),
        on_failure=opt_str_tuple(data, "on_failure", path),
        allow_full_access=opt_bool(data, "allow_full_access", path) or False,
        disabled=opt_bool(data, "disabled", path) or False,
    )


# --------------------------------------------------------------------------
# effective values
# --------------------------------------------------------------------------


def effective_timeout(manifest: Manifest, task: TaskSpec) -> int | None:
    if task.timeout_seconds is not None:
        return task.timeout_seconds
    return manifest.defaults.timeout_seconds


def effective_on_failure(manifest: Manifest, task: TaskSpec) -> tuple[str, ...]:
    if task.on_failure is not None:
        return task.on_failure
    return manifest.defaults.on_failure


def effective_randomized_delay(manifest: Manifest, task: TaskSpec) -> int:
    if task.randomized_delay_seconds is not None:
        return task.randomized_delay_seconds
    return manifest.defaults.randomized_delay_seconds or 0


def effective_persistent(manifest: Manifest, task: TaskSpec) -> bool:
    if task.persistent is not None:
        return task.persistent
    if manifest.defaults.persistent is not None:
        return manifest.defaults.persistent
    return task.schedule is not None and task.schedule.is_calendar
