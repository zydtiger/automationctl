"""The short-lived standalone-task execution wrapper."""

from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import IO, Any, TextIO

from . import paths, records
from .commands import CommandRunner
from .errors import AutomationctlError, ConfigError, LockBusy
from .locks import named_lock, run_lock
from .notify import HttpSender, NotifyEvent, NotifyOutcome, dispatch, transport_name
from .spec import TaskSpec, effective_persistent
from .template import build_invocation, builtin_values

BASE_ENV_KEYS = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "SSH_AUTH_SOCK",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
KILL_GRACE_SECONDS = 30.0
VERSION_PROBE_TIMEOUT = 5.0
STDERR_TAIL_LINES = 20
STDERR_TAIL_BYTES = 64 * 1024
NOTIFY_SUMMARY_BYTES = 64 * 1024


def tool_version() -> str:
    try:
        return version("automationctl")
    except PackageNotFoundError:
        return "unknown"


def parse_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read env file: {exc}", path) from exc
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ConfigError(f"invalid env file line {number}: {raw!r}", path)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def build_env(
    task: TaskSpec, ambient: Mapping[str, str], run_dir: Path, *, strict: bool = True
) -> dict[str, str]:
    env = {key: ambient[key] for key in BASE_ENV_KEYS if key in ambient}
    env.setdefault("HOME", str(Path.home()))
    prepended = [str(paths.expand(value)) for value in task.path_prepend]
    env["PATH"] = os.pathsep.join([*prepended, DEFAULT_PATH]) if prepended else DEFAULT_PATH
    for value in task.env_files:
        try:
            env.update(parse_env_file(paths.expand(value)))
        except ConfigError:
            if strict:
                raise
    env.update(task.env)
    env["AUTOMATIONCTL_TASK"] = task.name
    env["AUTOMATIONCTL_RUN_DIR"] = str(run_dir)
    return env


@dataclass
class ExecOptions:
    state_dir: Path
    hostname: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    jitter: bool = False
    kill_grace: float = KILL_GRACE_SECONDS
    capture_versions: bool = True
    sleeper: Callable[[float], None] = time.sleep
    stdout: TextIO | None = None
    stderr: TextIO | None = None
    notify_sender: HttpSender | None = None
    notify_runner: CommandRunner | None = None

    def out(self) -> TextIO:
        return self.stdout or sys.stdout

    def err(self) -> TextIO:
        return self.stderr or sys.stderr


@dataclass(frozen=True)
class ExecResult:
    task: str
    status: str
    exit_code: int
    run_id: str
    run_dir: Path
    duration_seconds: float
    reason: str = ""
    notifications: tuple[NotifyOutcome, ...] = ()

    @property
    def failed(self) -> bool:
        return self.status in records.FAILURE_STATUSES


def _write_stdin(handle: IO[bytes], payload: bytes) -> None:
    try:
        handle.write(payload)
        handle.flush()
    except (OSError, ValueError):
        pass
    finally:
        with suppress(OSError, ValueError):
            handle.close()


def _pump(source: IO[bytes], sink: Path, passthrough: TextIO) -> None:
    descriptor = os.open(sink, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, records.PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        while chunk := source.readline():
            handle.write(chunk)
            handle.flush()
            passthrough.write(chunk.decode("utf-8", "replace"))
            passthrough.flush()


@contextmanager
def _run_locks(state_dir: Path, task: TaskSpec) -> Iterator[None]:
    with run_lock(state_dir / "locks", task.name):
        if task.lock is None:
            yield
        else:
            with named_lock(state_dir / "locks", task.lock):
                yield


def _terminate(process: subprocess.Popen[bytes], grace: float) -> None:
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signal_number)
        except (ProcessLookupError, PermissionError):
            with suppress(ProcessLookupError):
                process.send_signal(signal_number)
        try:
            process.wait(timeout=grace if signal_number == signal.SIGTERM else 5)
            return
        except subprocess.TimeoutExpired:
            pass


def _probe_version(program: str, env: Mapping[str, str]) -> str | None:
    try:
        completed = subprocess.run(
            [program, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT,
            env=dict(env),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = (completed.stdout or completed.stderr or "").strip().splitlines()
    return lines[0][:200] if lines else None


def _tail(path: Path) -> str:
    return "\n".join(records.tail_lines(path, STDERR_TAIL_LINES, max_bytes=STDERR_TAIL_BYTES))


def _read_result(run_dir: Path) -> str:
    path = run_dir / records.RESULT_FILE
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size > NOTIFY_SUMMARY_BYTES:
        return f"result omitted from notification ({size} bytes)"
    data = records.read_json(path)
    if data is None:
        return ""
    summary = data.get("summary")
    return summary.strip() if isinstance(summary, str) else json.dumps(data, sort_keys=True)[:500]


def _effective(task: TaskSpec) -> dict[str, Any]:
    return {
        "timeout_seconds": task.timeout_seconds,
        "on_failure": list(task.on_failure),
        "jitter_seconds": task.jitter_seconds,
        "persistent": effective_persistent(task),
    }


def _notify_env(task: TaskSpec, options: ExecOptions, run_dir: Path) -> Mapping[str, str]:
    try:
        return build_env(task, options.env, run_dir, strict=False)
    except AutomationctlError:
        return options.env


def _finalize(
    task: TaskSpec,
    options: ExecOptions,
    meta: dict[str, Any],
    run_dir: Path,
    run_id: str,
    started: datetime,
    status: str,
    exit_code: int,
    reason: str = "",
) -> ExecResult:
    finished = records.utcnow()
    duration = (finished - started).total_seconds()
    meta.update(
        {
            "status": status,
            "exit_code": exit_code,
            "finished_at": records.isoformat(finished),
            "duration_seconds": round(duration, 3),
            "reason": reason,
        }
    )
    records.write_meta(run_dir, meta)
    records.write_last(
        options.state_dir,
        task.name,
        {
            "task": task.name,
            "status": status,
            "exit_code": exit_code,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "started_at": meta["started_at"],
            "finished_at": meta["finished_at"],
            "duration_seconds": meta["duration_seconds"],
            "host": options.hostname,
            "schedule": task.schedule.text if task.schedule else None,
        },
    )
    outcomes: tuple[NotifyOutcome, ...] = ()
    if status in records.FAILURE_STATUSES and task.on_failure:
        body_parts = [
            f"task: {task.name}",
            f"host: {options.hostname}",
            f"status: {status}",
            f"exit: {exit_code}",
            f"duration: {duration:.1f}s",
            f"run: {run_dir}",
        ]
        if reason:
            body_parts.append(f"reason: {reason}")
        if summary := _read_result(run_dir):
            body_parts.append(f"summary: {summary}")
        if tail := _tail(run_dir / records.STDERR_FILE):
            body_parts.append("stderr tail:\n" + tail)
        event = NotifyEvent(
            task.name,
            status,
            exit_code,
            f"automationctl: {task.name} {status}",
            "\n".join(body_parts),
            str(run_dir),
        )
        delivered: list[NotifyOutcome] = []
        for reference in task.on_failure:
            try:
                delivered.extend(
                    dispatch(
                        [reference],
                        task.notify,
                        event,
                        env=_notify_env(task, options, run_dir),
                        runner=options.notify_runner,
                        sender=options.notify_sender,
                    )
                )
            except Exception as exc:
                delivered.append(
                    NotifyOutcome(
                        transport_name(reference) or reference,
                        False,
                        f"transport raised {type(exc).__name__}: {exc}",
                    )
                )
        outcomes = tuple(delivered)
        meta["notifications"] = [
            {"transport": item.transport, "ok": item.ok, "detail": item.detail} for item in outcomes
        ]
        records.write_meta(run_dir, meta)
    return ExecResult(task.name, status, exit_code, run_id, run_dir, duration, reason, outcomes)


def _summary(task: TaskSpec, run_dir: Path, env: Mapping[str, str], meta: dict[str, Any]) -> None:
    if task.summary_cmd is None:
        return
    output = (
        (run_dir / records.STDOUT_FILE).read_text(encoding="utf-8", errors="replace")
        if (run_dir / records.STDOUT_FILE).exists()
        else ""
    )
    try:
        completed = subprocess.run(
            task.summary_cmd,
            input=output,
            capture_output=True,
            text=True,
            env=dict(env),
            cwd=run_dir,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        meta["summary_error"] = str(exc)
        return
    meta["summary_exit_code"] = completed.returncode
    text = (completed.stdout or "").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"summary": text}
    records.write_json(
        run_dir / records.RESULT_FILE, result if isinstance(result, dict) else {"summary": result}
    )


def exec_task(task: TaskSpec, options: ExecOptions) -> ExecResult:
    started = records.utcnow()
    run_id = records.new_run_id(started)
    run_dir = records.create_run_dir(options.state_dir, task.name, run_id)
    meta: dict[str, Any] = {
        "task": task.name,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "host": options.hostname,
        "tool_version": tool_version(),
        "spec": task.snapshot(),
        "effective": _effective(task),
        "started_at": records.isoformat(started),
        "status": "running",
    }
    records.write_meta(run_dir, meta)
    if task.disabled:
        return _finalize(
            task,
            options,
            meta,
            run_dir,
            run_id,
            started,
            records.STATUS_SKIPPED,
            0,
            "task is disabled",
        )
    if options.jitter and task.jitter_seconds:
        delay = random.uniform(0, task.jitter_seconds)
        meta["jitter_seconds"] = round(delay, 3)
        options.sleeper(delay)
    try:
        with _run_locks(options.state_dir, task):
            return _execute(task, options, meta, run_dir, run_id, started)
    except LockBusy as exc:
        return _finalize(
            task, options, meta, run_dir, run_id, started, records.STATUS_SKIPPED, 0, str(exc)
        )
    except AutomationctlError as exc:
        options.err().write(f"automationctl: {exc}\n")
        return _finalize(
            task, options, meta, run_dir, run_id, started, records.STATUS_ERROR, 1, str(exc)
        )


def _execute(
    task: TaskSpec,
    options: ExecOptions,
    meta: dict[str, Any],
    run_dir: Path,
    run_id: str,
    started: datetime,
) -> ExecResult:
    env = build_env(task, options.env, run_dir)
    invocation = build_invocation(
        task, builtin_values(task.name, options.hostname, str(run_dir), started)
    )
    meta["argv"] = list(invocation.argv)
    meta["stdin"] = "inline" if invocation.stdin_text is not None else None
    cwd = paths.expand(task.cwd) if task.cwd else Path(env["HOME"])
    if not cwd.is_dir():
        raise ConfigError(f"cwd does not exist: {cwd}", task.path)
    meta["cwd"] = str(cwd)
    program = invocation.argv[0]
    resolved = program if os.sep in program else shutil.which(program, path=env["PATH"])
    if resolved is None:
        raise ConfigError(f"command not found on PATH: {program}", task.path)
    meta["program"] = resolved
    meta["timeout_seconds"] = task.timeout_seconds
    try:
        process = subprocess.Popen(
            [resolved, *invocation.argv[1:]],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if invocation.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ConfigError(f"cannot start {resolved}: {exc}", task.path) from exc
    assert process.stdout is not None and process.stderr is not None
    pumps = [
        threading.Thread(
            target=_pump,
            args=(process.stdout, run_dir / records.STDOUT_FILE, options.out()),
            daemon=True,
        ),
        threading.Thread(
            target=_pump,
            args=(process.stderr, run_dir / records.STDERR_FILE, options.err()),
            daemon=True,
        ),
    ]
    for pump in pumps:
        pump.start()
    writer = None
    if invocation.stdin_text is not None and process.stdin is not None:
        writer = threading.Thread(
            target=_write_stdin, args=(process.stdin, invocation.stdin_text.encode()), daemon=True
        )
        writer.start()
    status = records.STATUS_OK
    reason = ""
    try:
        exit_code = process.wait(timeout=task.timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate(process, options.kill_grace)
        exit_code = process.returncode if process.returncode is not None else -signal.SIGKILL
        status, reason = records.STATUS_TIMEOUT, f"timed out after {task.timeout_seconds}s"
    for pump in pumps:
        pump.join(timeout=10)
    if writer is not None:
        writer.join(timeout=10)
    if status != records.STATUS_TIMEOUT and exit_code != 0:
        status, reason = records.STATUS_FAILED, f"exited {exit_code}"
    _summary(task, run_dir, env, meta)
    if options.capture_versions and (version_text := _probe_version(resolved, env)):
        meta["program_version"] = version_text
    return _finalize(task, options, meta, run_dir, run_id, started, status, exit_code, reason)
