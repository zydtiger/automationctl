"""Standalone task and machine-policy models."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, ScheduleError
from .schedule import Schedule, parse_duration
from .schedule import parse as parse_schedule

SCHEMA_VERSION = 2
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TASK_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "description",
        "command",
        "stdin",
        "stdin_file",
        "cwd",
        "env",
        "env_files",
        "path_prepend",
        "schedule",
        "timeout",
        "jitter",
        "persistent",
        "lock",
        "summary_cmd",
        "on_failure",
        "allow_full_access",
        "disabled",
        "notify",
    }
)
POLICY_KEYS = frozenset({"schema_version", "lint", "catchup_sweep"})
LINT_KEYS = frozenset({"forbidden_argv"})
NOTIFY_KEYS = frozenset({"type", "url_env", "command", "title"})


def load_toml(path: Path) -> dict[str, Any]:
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


def _unknown(table: Mapping[str, Any], allowed: frozenset[str], path: Path, where: str) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise ConfigError(f"{where}: unknown field(s): {', '.join(extra)}", path)


def _version(table: Mapping[str, Any], path: Path) -> int:
    value = table.get("schema_version")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError("schema_version must be an integer", path)
    if value != SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version: {value} (expected {SCHEMA_VERSION})", path)
    return value


def _str(table: Mapping[str, Any], key: str, path: Path, *, required: bool = False) -> str | None:
    if key not in table:
        if required:
            raise ConfigError(f"missing required field: {key}", path)
        return None
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string", path)
    return value


def _bool(table: Mapping[str, Any], key: str, path: Path) -> bool | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean", path)
    return value


def _strings(table: Mapping[str, Any], key: str, path: Path) -> tuple[str, ...] | None:
    if key not in table:
        return None
    value = table[key]
    if (
        isinstance(value, str)
        or not isinstance(value, Sequence)
        or not all(isinstance(item, str) for item in value)
    ):
        raise ConfigError(f"{key} must be a list of strings", path)
    return tuple(value)


def _string_map(table: Mapping[str, Any], key: str, path: Path) -> dict[str, str]:
    if key not in table:
        return {}
    value = table[key]
    if not isinstance(value, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ConfigError(f"{key} must be a table of strings", path)
    return dict(value)


def _duration(table: Mapping[str, Any], key: str, path: Path) -> int | None:
    text = _str(table, key, path)
    if text is None:
        return None
    try:
        return parse_duration(text)
    except ScheduleError as exc:
        raise ConfigError(str(exc), path) from exc


def validate_task_name(name: str, path: Path) -> None:
    if not TASK_NAME_RE.fullmatch(name) or name == "catchup":
        raise ConfigError("invalid or reserved task name", path)


@dataclass(frozen=True)
class NotifyTransport:
    name: str
    kind: str
    url_env: str | None = None
    command: tuple[str, ...] = ()
    title: str | None = None


@dataclass(frozen=True)
class LintPolicy:
    forbidden_argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class MachinePolicy:
    path: Path
    lint: LintPolicy = field(default_factory=LintPolicy)
    catchup_sweep_seconds: int | None = None


@dataclass(frozen=True)
class TaskSpec:
    name: str
    path: Path
    description: str
    command: tuple[str, ...]
    stdin: str | None = None
    stdin_file: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    env_files: tuple[str, ...] = ()
    path_prepend: tuple[str, ...] = ()
    schedule: Schedule | None = None
    timeout_seconds: int | None = None
    jitter_seconds: int = 0
    persistent: bool | None = None
    lock: str | None = None
    summary_cmd: tuple[str, ...] | None = None
    on_failure: tuple[str, ...] = ()
    allow_full_access: bool = False
    disabled: bool = False
    notify: Mapping[str, NotifyTransport] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "description": self.description,
            "command": list(self.command),
            "has_stdin": self.stdin is not None,
            "cwd": self.cwd,
            "env_keys": sorted(self.env),
            "env_files": list(self.env_files),
            "path_prepend": list(self.path_prepend),
            "schedule": self.schedule.text if self.schedule else None,
            "timeout_seconds": self.timeout_seconds,
            "jitter_seconds": self.jitter_seconds,
            "persistent": effective_persistent(self),
            "lock": self.lock,
            "summary_cmd": list(self.summary_cmd) if self.summary_cmd else None,
            "allow_full_access": self.allow_full_access,
            "disabled": self.disabled,
        }


def _notify(table: Mapping[str, Any], path: Path) -> dict[str, NotifyTransport]:
    raw = table.get("notify", {})
    if not isinstance(raw, Mapping):
        raise ConfigError("[notify] must be a table", path)
    result: dict[str, NotifyTransport] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            raise ConfigError("[notify] entries must be tables", path)
        _unknown(value, NOTIFY_KEYS, path, f"[notify.{name}]")
        command = _strings(value, "command", path) or ()
        kind = _str(value, "type", path) or ("command" if command else "ntfy")
        url_env = _str(value, "url_env", path)
        if kind not in {"ntfy", "command"}:
            raise ConfigError(f"[notify.{name}]: unknown transport type: {kind!r}", path)
        if kind == "ntfy":
            if not url_env:
                raise ConfigError(f"[notify.{name}]: ntfy transport requires url_env", path)
            if "command" in value:
                raise ConfigError(f"[notify.{name}]: ntfy transport cannot set command", path)
        if kind == "command":
            if not command:
                raise ConfigError(f"[notify.{name}]: command transport requires command", path)
            if "url_env" in value:
                raise ConfigError(f"[notify.{name}]: command transport cannot set url_env", path)
        result[name] = NotifyTransport(name, kind, url_env, command, _str(value, "title", path))
    return result


def parse_task(data: Mapping[str, Any], path: Path) -> TaskSpec:
    _unknown(data, TASK_KEYS, path, "task")
    _version(data, path)
    name = _str(data, "name", path, required=True)
    assert name is not None
    validate_task_name(name, path)
    description = _str(data, "description", path, required=True)
    command = _strings(data, "command", path)
    if not command:
        raise ConfigError("command is required and must be non-empty", path)
    stdin = _str(data, "stdin", path)
    stdin_file = _str(data, "stdin_file", path)
    if stdin is not None and stdin_file is not None:
        raise ConfigError("stdin and stdin_file are mutually exclusive", path)
    schedule_value = data.get("schedule")
    schedule = None
    if schedule_value is not None:
        if not isinstance(schedule_value, str | Mapping):
            raise ConfigError("schedule must be a string or a per-backend table", path)
        try:
            schedule = parse_schedule(schedule_value)
        except ScheduleError as exc:
            raise ConfigError(str(exc), path) from exc
    jitter = _duration(data, "jitter", path) or 0
    summary_cmd = _strings(data, "summary_cmd", path)
    if summary_cmd is not None and not summary_cmd:
        raise ConfigError("summary_cmd must be non-empty when supplied", path)
    return TaskSpec(
        name,
        path,
        description or "",
        command,
        stdin,
        stdin_file,
        _str(data, "cwd", path),
        _string_map(data, "env", path),
        _strings(data, "env_files", path) or (),
        _strings(data, "path_prepend", path) or (),
        schedule,
        _duration(data, "timeout", path),
        jitter,
        _bool(data, "persistent", path),
        _str(data, "lock", path),
        summary_cmd,
        _strings(data, "on_failure", path) or (),
        _bool(data, "allow_full_access", path) or False,
        _bool(data, "disabled", path) or False,
        _notify(data, path),
    )


def parse_policy(data: Mapping[str, Any], path: Path) -> MachinePolicy:
    _unknown(data, POLICY_KEYS, path, "machine policy")
    _version(data, path)
    lint = data.get("lint", {})
    if not isinstance(lint, Mapping):
        raise ConfigError("[lint] must be a table", path)
    _unknown(lint, LINT_KEYS, path, "[lint]")
    sweep = _duration(data, "catchup_sweep", path)
    if sweep is not None and sweep <= 0:
        raise ConfigError("catchup_sweep must be a positive duration", path)
    return MachinePolicy(path, LintPolicy(_strings(lint, "forbidden_argv", path) or ()), sweep)


def effective_persistent(task: TaskSpec) -> bool:
    if task.persistent is not None:
        return task.persistent
    return task.schedule is not None and (task.schedule.is_calendar or task.schedule.kind == "raw")
