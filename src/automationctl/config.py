"""Installed-task discovery, policy loading, and private atomic persistence."""

from __future__ import annotations

import fcntl
import os
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import paths, records
from .errors import ConfigError
from .spec import MachinePolicy, TaskSpec, load_toml, parse_policy, parse_task, validate_task_name


def task_path(name: str, env: Mapping[str, str] | None = None) -> Path:
    validate_task_name(name, paths.tasks_dir(env) / f"{name}.toml")
    return paths.tasks_dir(env) / f"{name}.toml"


def load_task(path: Path) -> TaskSpec:
    return parse_task(load_toml(path), path.resolve())


def load_installed(name: str, env: Mapping[str, str] | None = None) -> TaskSpec:
    path = task_path(name, env)
    task = load_task(path)
    if task.name != name:
        raise ConfigError(f"installed filename and task name disagree: {task.name!r}", path)
    if task.stdin_file is not None:
        raise ConfigError("installed task must materialize stdin_file as stdin", path)
    return task


def discover_installed(
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, TaskSpec], tuple[ConfigError, ...]]:
    directory = paths.tasks_dir(env)
    tasks: dict[str, TaskSpec] = {}
    errors: list[ConfigError] = []
    if not directory.is_dir():
        return tasks, ()
    for path in sorted(directory.glob("*.toml")):
        try:
            task = load_task(path)
            if task.name != path.stem:
                raise ConfigError(f"installed filename and task name disagree: {task.name!r}", path)
            if task.stdin_file is not None:
                raise ConfigError("installed task must materialize stdin_file as stdin", path)
            if task.name in tasks:
                raise ConfigError(f"duplicate installed task name: {task.name}", path)
            tasks[task.name] = task
        except ConfigError as exc:
            errors.append(exc)
    return tasks, tuple(errors)


def load_policy(env: Mapping[str, str] | None = None) -> MachinePolicy:
    path = paths.policy_path(env)
    if not path.exists():
        return MachinePolicy(path)
    return parse_policy(load_toml(path), path)


def materialize(task: TaskSpec) -> TaskSpec:
    """Inline source-local stdin_file before an installed definition is written."""
    if task.stdin_file is None:
        return task
    source = Path(task.stdin_file)
    source = source if source.is_absolute() else task.path.parent / source
    try:
        stdin = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read stdin_file: {exc}", task.path) from exc
    return replace(task, stdin=stdin, stdin_file=None)


def _toml_string(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, tuple | list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{_toml_string(str(key))} = {_toml_value(item)}" for key, item in value.items()
            )
            + " }"
        )
    raise TypeError(f"unsupported TOML value: {value!r}")


def dump_task(task: TaskSpec) -> str:
    """Render a complete self-contained schema-v2 task definition."""
    values: list[tuple[str, object]] = [
        ("schema_version", 2),
        ("name", task.name),
        ("description", task.description),
        ("command", task.command),
    ]
    if task.stdin is not None:
        values.append(("stdin", task.stdin))
    if task.cwd is not None:
        values.append(("cwd", task.cwd))
    if task.env:
        values.append(("env", dict(task.env)))
    if task.env_files:
        values.append(("env_files", task.env_files))
    if task.path_prepend:
        values.append(("path_prepend", task.path_prepend))
    if task.schedule is not None:
        values.append(
            (
                "schedule",
                dict(task.schedule.raw) if task.schedule.kind == "raw" else task.schedule.text,
            )
        )
    if task.timeout_seconds is not None:
        values.append(("timeout", f"{task.timeout_seconds}s"))
    if task.jitter_seconds:
        values.append(("jitter", f"{task.jitter_seconds}s"))
    if task.persistent is not None:
        values.append(("persistent", task.persistent))
    if task.lock is not None:
        values.append(("lock", task.lock))
    if task.summary_cmd is not None:
        values.append(("summary_cmd", task.summary_cmd))
    if task.on_failure:
        values.append(("on_failure", task.on_failure))
    if task.allow_full_access:
        values.append(("allow_full_access", True))
    if task.disabled:
        values.append(("disabled", True))
    lines = [f"{key} = {_toml_value(value)}" for key, value in values]
    for name, transport in task.notify.items():
        lines.extend(
            ["", f"[notify.{_toml_string(name)}]", f"type = {_toml_value(transport.kind)}"]
        )
        if transport.url_env is not None:
            lines.append(f"url_env = {_toml_value(transport.url_env)}")
        if transport.command:
            lines.append(f"command = {_toml_value(transport.command)}")
        if transport.title is not None:
            lines.append(f"title = {_toml_value(transport.title)}")
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    records.ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, records.PRIVATE_FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(records.PRIVATE_FILE_MODE)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def write_installed(task: TaskSpec, env: Mapping[str, str] | None = None) -> Path:
    path = task_path(task.name, env)
    atomic_write(path, dump_task(task))
    return path


@contextmanager
def management_lock(env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Serialize add/install/remove after their no-write validation phase."""
    root = paths.config_dir(env)
    records.ensure_private_dir(root)
    lock = root / ".manage.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, records.PRIVATE_FILE_MODE)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
