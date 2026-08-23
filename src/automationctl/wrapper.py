"""The ``exec`` lifecycle: the only nontrivial runtime code in the tool.

Everything platform-divergent lives here — environment construction, named
locks, timeout enforcement, tee logging, summary extraction, run records, and
failure notification — so that generated units and plists stay dumb.
"""

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
from .config import Automations, load_prompt
from .errors import AutomationctlError, ConfigError, LockBusy
from .locks import named_lock
from .notify import HttpSender, NotifyEvent, NotifyOutcome, dispatch, transport_name
from .records import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    STDERR_FILE,
    STDOUT_FILE,
    create_run_dir,
    isoformat,
    new_run_id,
    utcnow,
    write_meta,
)
from .spec import (
    TaskSpec,
    effective_on_failure,
    effective_persistent,
    effective_randomized_delay,
    effective_timeout,
)
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


def effective_snapshot(automations: Automations, task: TaskSpec) -> dict[str, Any]:
    """Return the task fields after manifest defaults have been applied.

    A run record has to answer "why did this task time out after 30 minutes"
    even when the spec says nothing about a timeout, so the resolved values go
    in beside the raw spec snapshot.
    """
    return {
        "timeout_seconds": effective_timeout(automations.manifest, task),
        "on_failure": list(effective_on_failure(automations.manifest, task)),
        "randomized_delay_seconds": effective_randomized_delay(automations.manifest, task),
        "persistent": effective_persistent(automations.manifest, task),
    }


def tool_version() -> str:
    try:
        return version("automationctl")
    except PackageNotFoundError:  # pragma: no cover - only when running uninstalled
        return "unknown"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` environment file, ignoring comments and blanks."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read env file: {exc}", path) from exc
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ConfigError(f"invalid env file line {number}: {raw!r}", path)
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
            cleaned = cleaned[1:-1]
        values[key.strip()] = cleaned
    return values


def build_env(
    automations: Automations,
    task: TaskSpec,
    ambient: Mapping[str, str],
    run_dir: Path,
    *,
    strict: bool = True,
) -> dict[str, str]:
    """Build the child environment: minimal base, path_prepend, env files, task env.

    ``strict=False`` skips unreadable env files instead of failing. It is for
    callers that only want to see the environment a run *would* get — `doctor`
    probing PATH, and the failure-notification path, which must still deliver
    an alert when the very problem being reported is a missing env file.
    """
    env: dict[str, str] = {key: ambient[key] for key in BASE_ENV_KEYS if key in ambient}
    env.setdefault("HOME", str(Path.home()))

    prepend = [str(paths.expand(item)) for item in automations.host_config.path_prepend]
    env["PATH"] = os.pathsep.join([*prepend, DEFAULT_PATH]) if prepend else DEFAULT_PATH

    for item in (*automations.host_config.env_files, *task.env_files):
        try:
            env.update(parse_env_file(paths.expand(item)))
        except ConfigError:
            if strict:
                raise

    env.update(task.env)
    env["AUTOMATIONCTL_TASK"] = task.name
    env["AUTOMATIONCTL_RUN_DIR"] = str(run_dir)
    env["AUTOMATIONCTL_HOST"] = automations.host
    return env


@dataclass
class ExecOptions:
    """Injectable surroundings for one ``exec`` invocation."""

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
        return self.stdout if self.stdout is not None else sys.stdout

    def err(self) -> TextIO:
        return self.stderr if self.stderr is not None else sys.stderr


@dataclass(frozen=True)
class ExecResult:
    """What one ``exec`` invocation did."""

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
    """Feed the prompt to the child from its own thread.

    A prompt larger than the pipe buffer blocks until the child reads it, and
    an unattended agent that never reads stdin would otherwise hold the
    wrapper hostage past its own timeout. Writing off-thread keeps the
    deadline enforceable; the write simply fails once the child is killed.
    """
    try:
        handle.write(payload)
        handle.flush()
    except (OSError, ValueError):
        pass
    finally:
        with suppress(OSError, ValueError):
            handle.close()


def _pump(source: IO[bytes], sink: Path, passthrough: TextIO) -> None:
    with sink.open("wb") as handle:
        while True:
            chunk = source.readline()
            if not chunk:
                break
            handle.write(chunk)
            handle.flush()
            passthrough.write(chunk.decode("utf-8", "replace"))
            passthrough.flush()


@contextmanager
def _maybe_lock(state_dir: Path, name: str | None) -> Iterator[None]:
    if name is None:
        yield
        return
    with named_lock(state_dir / "locks", name):
        yield


def _terminate(process: subprocess.Popen[bytes], grace: float) -> None:
    """SIGTERM the child's process group, then SIGKILL after the grace period."""
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signal_number)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(signal_number)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=grace if signal_number == signal.SIGTERM else 5.0)
            return
        except subprocess.TimeoutExpired:
            continue


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
    first = (completed.stdout or completed.stderr or "").strip().splitlines()
    return first[0][:200] if first else None


def _tail(path: Path, lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _read_result(run_dir: Path) -> str:
    data = records.read_json(run_dir / records.RESULT_FILE)
    if data is None:
        return ""
    summary = data.get("summary")
    if isinstance(summary, str):
        return summary.strip()
    return json.dumps(data, sort_keys=True)[:500]


def _notify_env(
    automations: Automations,
    task: TaskSpec,
    options: ExecOptions,
    run_dir: Path,
) -> Mapping[str, str]:
    """Return the environment notification transports resolve variables from.

    Notification credentials — an ntfy URL, a webhook token — live in
    ``env_files`` alongside the run's other secrets, never in the ambient
    environment of a timer-started process. Notify therefore reads the same
    environment the child would have received.
    """
    try:
        return build_env(automations, task, options.env, run_dir, strict=False)
    except AutomationctlError:
        return options.env


def _finalize(
    automations: Automations,
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
    finished = utcnow()
    duration = (finished - started).total_seconds()
    meta.update(
        {
            "status": status,
            "exit_code": exit_code,
            "finished_at": isoformat(finished),
            "duration_seconds": round(duration, 3),
            "reason": reason,
        }
    )
    write_meta(run_dir, meta)
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
            "schedule": task.schedule.text if task.schedule is not None else None,
        },
    )

    outcomes: tuple[NotifyOutcome, ...] = ()
    if status in records.FAILURE_STATUSES:
        references = effective_on_failure(automations.manifest, task)
        if references:
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
            summary = _read_result(run_dir)
            if summary:
                body_parts.append(f"summary: {summary}")
            tail = _tail(run_dir / STDERR_FILE, STDERR_TAIL_LINES)
            if tail:
                body_parts.append("stderr tail:\n" + tail)
            event = NotifyEvent(
                task=task.name,
                status=status,
                exit_code=exit_code,
                title=f"automationctl: {task.name} {status}",
                body="\n".join(body_parts),
                run_dir=str(run_dir),
            )
            try:
                outcomes = tuple(
                    dispatch(
                        references,
                        automations.manifest,
                        event,
                        env=_notify_env(automations, task, options, run_dir),
                        runner=options.notify_runner,
                        sender=options.notify_sender,
                    )
                )
            except Exception as exc:
                # The invariant, not a substitute for the precise handling
                # inside send(): no notification transport may ever destroy a
                # run's exit code or its record. Transports reach arbitrary
                # third-party code — HTTP stacks, hook programs — and the set
                # of exceptions that can come back is not enumerable.
                outcomes = tuple(
                    NotifyOutcome(
                        transport_name(reference) or reference,
                        False,
                        f"transport raised {type(exc).__name__}: {exc}",
                    )
                    for reference in references
                )
            meta["notifications"] = [
                {"transport": item.transport, "ok": item.ok, "detail": item.detail}
                for item in outcomes
            ]
            write_meta(run_dir, meta)

    return ExecResult(
        task=task.name,
        status=status,
        exit_code=exit_code,
        run_id=run_id,
        run_dir=run_dir,
        duration_seconds=duration,
        reason=reason,
        notifications=outcomes,
    )


def _run_summary_cmd(
    task: TaskSpec,
    run_dir: Path,
    env: Mapping[str, str],
    meta: dict[str, Any],
) -> None:
    if task.summary_cmd is None:
        return
    try:
        stdout_text = (run_dir / STDOUT_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        stdout_text = ""
    try:
        completed = subprocess.run(
            list(task.summary_cmd),
            input=stdout_text,
            capture_output=True,
            text=True,
            env=dict(env),
            cwd=str(run_dir),
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        meta["summary_error"] = str(exc)
        return
    meta["summary_exit_code"] = completed.returncode
    output = (completed.stdout or "").strip()
    payload: dict[str, Any]
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        payload = {"summary": output}
    else:
        payload = parsed if isinstance(parsed, dict) else {"summary": parsed}
    records.write_json(run_dir / records.RESULT_FILE, payload)


def exec_task(automations: Automations, task: TaskSpec, options: ExecOptions) -> ExecResult:
    """Run one task through the full wrapper lifecycle."""
    started = utcnow()
    run_id = new_run_id(started)
    run_dir = create_run_dir(options.state_dir, task.name, run_id)
    meta: dict[str, Any] = {
        "task": task.name,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "host": options.hostname,
        "manifest": str(automations.manifest.path),
        "tool_version": tool_version(),
        "spec": task.snapshot(),
        "effective": effective_snapshot(automations, task),
        "started_at": isoformat(started),
        "status": "running",
    }
    write_meta(run_dir, meta)

    delay = effective_randomized_delay(automations.manifest, task) if options.jitter else 0
    if delay > 0:
        waited = random.uniform(0, delay)
        meta["jitter_seconds"] = round(waited, 3)
        options.sleeper(waited)

    try:
        with _maybe_lock(options.state_dir, task.lock):
            return _execute(automations, task, options, meta, run_dir, run_id, started)
    except LockBusy as exc:
        return _finalize(
            automations,
            task,
            options,
            meta,
            run_dir,
            run_id,
            started,
            STATUS_SKIPPED,
            0,
            reason=str(exc),
        )
    except AutomationctlError as exc:
        options.err().write(f"automationctl: {exc}\n")
        return _finalize(
            automations, task, options, meta, run_dir, run_id, started, STATUS_ERROR, 1, str(exc)
        )


def _execute(
    automations: Automations,
    task: TaskSpec,
    options: ExecOptions,
    meta: dict[str, Any],
    run_dir: Path,
    run_id: str,
    started: datetime,
) -> ExecResult:
    env = build_env(automations, task, options.env, run_dir)
    prompt = load_prompt(automations.manifest, task)
    runner = automations.runners.get(task.runner) if task.runner is not None else None
    if task.runner is not None and runner is None:
        raise ConfigError(f"unknown runner: {task.runner}", task.path)

    values = builtin_values(
        task=task.name, hostname=options.hostname, run_dir=str(run_dir), now=started
    )
    invocation = build_invocation(task, runner, prompt, values)
    meta["argv"] = list(invocation.argv)
    meta["stdin"] = "prompt" if invocation.stdin_text is not None else None

    cwd = paths.expand(task.cwd) if task.cwd else Path.cwd()
    if not cwd.is_dir():
        raise ConfigError(f"cwd does not exist: {cwd}", task.path)
    meta["cwd"] = str(cwd)

    program = invocation.argv[0]
    resolved = program if os.sep in program else shutil.which(program, path=env["PATH"])
    if resolved is None:
        raise ConfigError(f"command not found on PATH: {program}", task.path)
    meta["program"] = resolved

    timeout = effective_timeout(automations.manifest, task)
    meta["timeout_seconds"] = timeout

    try:
        process = subprocess.Popen(
            [resolved, *invocation.argv[1:]],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if invocation.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ConfigError(f"cannot start {resolved}: {exc}", task.path) from exc

    assert process.stdout is not None
    assert process.stderr is not None
    pumps = [
        threading.Thread(
            target=_pump, args=(process.stdout, run_dir / STDOUT_FILE, options.out()), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(process.stderr, run_dir / STDERR_FILE, options.err()), daemon=True
        ),
    ]
    for pump in pumps:
        pump.start()

    writer: threading.Thread | None = None
    if invocation.stdin_text is not None and process.stdin is not None:
        writer = threading.Thread(
            target=_write_stdin,
            args=(process.stdin, invocation.stdin_text.encode("utf-8")),
            daemon=True,
        )
        writer.start()

    status = STATUS_OK
    reason = ""
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(process, options.kill_grace)
        exit_code = process.returncode if process.returncode is not None else -signal.SIGKILL
        status = STATUS_TIMEOUT
        reason = f"timed out after {timeout}s"

    for pump in pumps:
        pump.join(timeout=10)
    if writer is not None:
        writer.join(timeout=10)

    if status != STATUS_TIMEOUT and exit_code != 0:
        status = STATUS_FAILED
        reason = f"exited {exit_code}"

    _run_summary_cmd(task, run_dir, env, meta)
    if options.capture_versions:
        probed = _probe_version(resolved, env)
        if probed is not None:
            meta["program_version"] = probed

    return _finalize(
        automations,
        task,
        options,
        meta,
        run_dir,
        run_id,
        started,
        status,
        exit_code,
        reason,
    )
