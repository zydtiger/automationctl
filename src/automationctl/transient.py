"""Private execution snapshots and bounded transient lifecycle operations."""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import shutil
import signal
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import records
from .backends import Backend
from .backends.launchd import LaunchdBackend, dump_plist
from .backends.systemd import _quote
from .config import atomic_write, dump_task, load_task
from .errors import AutomationctlError
from .schedule import parse_duration
from .spec import TaskSpec
from .wrapper import ExecOptions, exec_task

ID_PATTERN = re.compile(r"tx:[0-9a-f]{32}\Z")
TERMINAL = frozenset({"ok", "failed", "timeout", "skipped", "error", "cancelled", "abandoned"})
CONTROL_TIMEOUT = 60


def is_id(value: str) -> bool:
    return ID_PATTERN.fullmatch(value) is not None


def directory(state: Path, execution_id: str) -> Path:
    if not is_id(execution_id):
        raise AutomationctlError("expected execution ID tx:<32 lowercase hexadecimal digits>")
    return state / "transient" / execution_id[3:]


def read(state: Path, execution_id: str) -> dict[str, Any]:
    data = records.read_json(directory(state, execution_id) / "execution.json")
    if data is None:
        raise AutomationctlError(f"execution {execution_id} not found or unreadable")
    return data


def discover(state: Path) -> list[dict[str, Any]]:
    return [
        data
        for path in sorted((state / "transient").glob("*/execution.json"))
        if (data := records.read_json(path)) is not None and is_id(str(data.get("id", "")))
    ]


@contextmanager
def locked(state: Path, execution_id: str) -> Iterator[dict[str, Any]]:
    root = directory(state, execution_id)
    # Never recreate a missing execution as a side effect of a control operation.
    read(state, execution_id)
    with os.fdopen(os.open(root / "state.lock", os.O_CREAT | os.O_RDWR, 0o600), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        data = read(state, execution_id)
        yield data
        records.write_json(root / "execution.json", data, durable=True)


def deadline(after: str | None, at: str | None, now: datetime | None = None) -> datetime | None:
    if after is not None and at is not None:
        raise AutomationctlError("--after and --at are mutually exclusive")
    now = now or records.utcnow()
    if after is not None:
        return now + timedelta(seconds=parse_duration(after))
    if at is None:
        return None
    try:
        value = datetime.fromisoformat(at)
    except ValueError as exc:
        raise AutomationctlError(
            "--at requires an ISO 8601 timestamp with timezone offset"
        ) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise AutomationctlError(
            "--at requires a timezone offset, for example 2030-01-02T09:00:00Z"
        )
    if value <= now:
        raise AutomationctlError("--at must be in the future")
    return value


def unit(execution_id: str, suffix: str = "service") -> str:
    if not is_id(execution_id):
        raise AutomationctlError("invalid execution ID")
    return f"actl-tx-{execution_id[3:]}.{suffix}"


def submission_argv(backend: Backend, execution_id: str, due: datetime | None) -> list[str]:
    if isinstance(backend, LaunchdBackend):
        return [
            "launchctl",
            "bootstrap",
            backend.domain,
            str(directory(backend.state_dir, execution_id) / "job.plist"),
        ]
    finish = " ".join(
        _quote(value, exec_start=True)
        for value in [backend.executable, "_transient-finish", execution_id]
    )
    argv = [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        "--service-type=exec",
        f"--unit={unit(execution_id)}",
        "--property=KillMode=mixed",
        "--property=TimeoutStopSec=90s",
        f"--property=ExecStopPost={finish}",
        f"--setenv=XDG_CONFIG_HOME={backend.config_home}",
        f"--setenv=XDG_STATE_HOME={backend.state_home}",
    ]
    if due is not None:
        calendar = due.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
        # Also cover a deadline that elapses between planning and registration.
        delay = max(0.001, (due - records.utcnow()).total_seconds())
        argv += [
            f"--on-calendar={calendar}",
            f"--on-active={delay:.6f}s",
            "--timer-property=AccuracySec=1us",
            "--timer-property=RemainAfterElapse=false",
            "--timer-property=CollectMode=inactive-or-failed",
        ]
    return [*argv, "--", backend.executable, "_transient-exec", execution_id]


def submit(task: TaskSpec, backend: Backend, due: datetime | None, *, dry_run: bool = False) -> str:
    execution_id = "tx:" + secrets.token_hex(16)
    argv = submission_argv(backend, execution_id, due)
    if dry_run:
        return execution_id
    state = backend.state_dir
    root = directory(state, execution_id)
    records.ensure_private_dir(state)
    records.ensure_private_dir(root.parent)
    root.mkdir(mode=0o700)  # Exclusive creation, never replace another invocation.
    atomic_write(root / "task.toml", dump_task(task))
    records.write_json(
        root / "execution.json",
        {
            "id": execution_id,
            "name": task.name,
            "backend": backend.name,
            "created_at": records.utcnow().isoformat(),
            "trigger_at": due.isoformat() if due else None,
            "status": "prepared",
            "acceptance": "pending",
            "cleanup": "pending",
            "claimed": False,
            "cancel_requested": False,
        },
    )
    if isinstance(backend, LaunchdBackend):
        atomic_write(
            root / "job.plist",
            dump_plist(
                {
                    "Label": launchd_label(execution_id),
                    "ProgramArguments": [backend.executable, "_transient-exec", execution_id],
                    "EnvironmentVariables": {
                        "XDG_CONFIG_HOME": str(backend.config_home),
                        "XDG_STATE_HOME": str(backend.state_home),
                    },
                    "RunAtLoad": True,
                    "LaunchOnlyOnce": True,
                    "ProcessType": "Background",
                    "ExitTimeOut": 90,
                }
            ),
        )
    with locked(state, execution_id) as data:
        # Type=exec returns before wrapper code needs this lock.
        try:
            result = backend.runner.run(argv, timeout=CONTROL_TIMEOUT)
        except OSError as exc:
            from .commands import CommandResult

            result = CommandResult(tuple(argv), 1, stderr=str(exc))
        data["acceptance"] = "accepted" if result.ok else "unknown"
        data["scheduler_returncode"] = result.returncode
        data["scheduler_error"] = result.stderr
        data["status"] = "scheduled" if result.ok else "submission-unknown"
    if not result.ok:
        raise AutomationctlError(
            f"submission {execution_id} was not confirmed: {result.stderr.strip()}; "
            f"inspect status and reconcile or cancel this ID; do not resubmit automatically"
        )
    return execution_id


def launchd_label(execution_id: str) -> str:
    return "automationctl.transient." + execution_id[3:]


def execute(state: Path, execution_id: str, options: ExecOptions, backend: Backend) -> int:
    root = directory(state, execution_id)
    cancelled = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    try:
        with locked(state, execution_id) as data:
            if data["claimed"] or data["status"] in TERMINAL or data["cancel_requested"]:
                return 0
            data["claimed"] = True
            # launchd uses a per-execution, scheduler-owned deadline waiter.
            due = datetime.fromisoformat(data["trigger_at"]) if data["trigger_at"] else None
        while due is not None and (remaining := (due - records.utcnow()).total_seconds()) > 0:
            if cancelled.wait(min(remaining, 1.0)):
                break
        with locked(state, execution_id) as data:
            if cancelled.is_set() or data["cancel_requested"]:
                data.update(
                    status="cancelled", exit_code=None, finished_at=records.utcnow().isoformat()
                )
                should_run = False
            else:
                data.update(status="running", started_at=records.utcnow().isoformat())
                should_run = True
        code = 0
        if should_run:
            task = load_task(root / "task.toml")
            options.state_dir = root / "history"
            options.lock_dir = state / "locks"
            options.cancel_event = cancelled
            # A --version probe is another execution of the user command.
            options.capture_versions = False
            result = exec_task(task, options)
            with locked(state, execution_id) as data:
                data.update(
                    status=result.status,
                    exit_code=result.exit_code,
                    finished_at=records.utcnow().isoformat(),
                    run_dir=str(result.run_dir),
                )
            code = 128 + abs(result.exit_code) if result.exit_code < 0 else result.exit_code
    except Exception as exc:
        with locked(state, execution_id) as data:
            data.update(
                status="error",
                exit_code=1,
                error=str(exc),
                finished_at=records.utcnow().isoformat(),
            )
        code = 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    if isinstance(backend, LaunchdBackend):
        # Last operation: result, logs and notifications are durable already.
        # bootout may kill this process before the call returns. Record intent
        # first; reconciliation verifies removal or retries it after interruption.
        try:
            (root / "job.plist").unlink(missing_ok=True)
            with locked(state, execution_id) as data:
                data.update(cleanup="delegated", cleanup_error=None)
            result_control = backend.runner.run(
                ["launchctl", "bootout", backend.target(launchd_label(execution_id))],
                timeout=CONTROL_TIMEOUT,
            )
            if not result_control.ok and _presence(backend, execution_id, "service"):
                raise AutomationctlError(result_control.stderr)
        except (AutomationctlError, OSError) as exc:
            with locked(state, execution_id) as data:
                data.update(cleanup="failed", cleanup_error=str(exc))
    return code


def _presence(backend: Backend, execution_id: str, suffix: str) -> bool:
    if isinstance(backend, LaunchdBackend):
        if suffix == "timer":
            return False
        result = backend.runner.run(
            ["launchctl", "print", backend.target(launchd_label(execution_id))],
            timeout=CONTROL_TIMEOUT,
        )
        if result.ok:
            return True
        if result.returncode == 113:
            domain = backend.runner.run(
                ["launchctl", "print", backend.domain], timeout=CONTROL_TIMEOUT
            )
            if domain.ok:
                return False
        raise AutomationctlError(f"cannot inspect launchd execution: {result.stderr}")
    result = backend.runner.run(
        [
            "systemctl",
            "--user",
            "show",
            unit(execution_id, suffix),
            "--property=LoadState",
            "--value",
        ],
        timeout=CONTROL_TIMEOUT,
    )
    value = result.stdout.strip()
    if value == "not-found":
        return False
    if not result.ok or value not in {"loaded", "error", "bad-setting", "masked"}:
        raise AutomationctlError(f"cannot inspect {unit(execution_id, suffix)}: {result.stderr}")
    return True


def _stop(backend: Backend, execution_id: str, suffix: str, *, no_block: bool = False) -> None:
    if not _presence(backend, execution_id, suffix):
        return
    if isinstance(backend, LaunchdBackend):
        result = backend.runner.run(
            ["launchctl", "bootout", backend.target(launchd_label(execution_id))],
            timeout=CONTROL_TIMEOUT,
        )
        if not result.ok and _presence(backend, execution_id, suffix):
            raise AutomationctlError(f"cannot bootout execution: {result.stderr}")
        return
    argv = ["systemctl", "--user", "stop"]
    if no_block:
        argv.append("--no-block")
    result = backend.runner.run([*argv, unit(execution_id, suffix)], timeout=CONTROL_TIMEOUT)
    if not result.ok and _presence(backend, execution_id, suffix):
        raise AutomationctlError(f"cannot stop {unit(execution_id, suffix)}: {result.stderr}")


def _recover_outcome(state: Path, execution_id: str, data: dict[str, Any], *, cancel: bool) -> None:
    if data["status"] in TERMINAL:
        return
    latest = records.latest_run(directory(state, execution_id) / "history", data["name"])
    if latest and latest.status in TERMINAL:
        data.update(status=latest.status, exit_code=latest.exit_code, run_dir=str(latest.run_dir))
    else:
        data.update(status="cancelled" if cancel else "abandoned", exit_code=None)
    data["finished_at"] = records.utcnow().isoformat()


def _inactive(backend: Backend, execution_id: str) -> bool:
    if isinstance(backend, LaunchdBackend):
        result = backend.runner.run(
            ["launchctl", "print", backend.target(launchd_label(execution_id))],
            timeout=CONTROL_TIMEOUT,
        )
        return (
            result.ok
            and re.search(r"^\s*state = (not running|exited)\s*$", result.stdout, re.MULTILINE)
            is not None
        )
    result = backend.runner.run(
        [
            "systemctl",
            "--user",
            "show",
            unit(execution_id),
            "--property=ActiveState",
            "--value",
        ],
        timeout=CONTROL_TIMEOUT,
    )
    return result.ok and result.stdout.strip() in {"inactive", "failed"}


def finish(state: Path, execution_id: str, backend: Backend, env: Mapping[str, str]) -> None:
    """systemd-owned post-stop hook; never stop the executing service itself."""
    with locked(state, execution_id) as data:
        _recover_outcome(state, execution_id, data, cancel=data["cancel_requested"])
        data["service_result"] = env.get("SERVICE_RESULT")
        data["service_exit_code"] = env.get("EXIT_CODE")
        data["service_exit_status"] = env.get("EXIT_STATUS")
    try:
        _stop(backend, execution_id, "timer", no_block=True)
    except AutomationctlError as exc:
        with locked(state, execution_id) as data:
            data.update(cleanup="failed", cleanup_error=str(exc))
        return
    with locked(state, execution_id) as data:
        data.update(cleanup="delegated", cleanup_error=None)


def reconcile(state: Path, execution_id: str, backend: Backend, *, cancel: bool = False) -> None:
    data = read(state, execution_id)
    if data["backend"] != backend.name:
        raise AutomationctlError("execution backend does not match the selected backend")
    if cancel:
        with locked(state, execution_id) as data:
            data["cancel_requested"] = True
    try:
        if (
            data["claimed"]
            and data["status"] not in TERMINAL
            and (
                not _presence(backend, execution_id, "service") or _inactive(backend, execution_id)
            )
        ):
            with locked(state, execution_id) as data:
                _recover_outcome(state, execution_id, data, cancel=data["cancel_requested"])
        if cancel or data["status"] in TERMINAL:
            # No state lock across stop: ExecStopPost needs to acquire it.
            _stop(backend, execution_id, "timer")
            _stop(backend, execution_id, "service")
        timer = _presence(backend, execution_id, "timer")
        service = _presence(backend, execution_id, "service")
        with locked(state, execution_id) as data:
            if not timer and not service:
                _recover_outcome(state, execution_id, data, cancel=cancel)
                (directory(state, execution_id) / "job.plist").unlink(missing_ok=True)
                data.update(cleanup="collected", cleanup_error=None)
            elif cancel or data["status"] in TERMINAL:
                data.update(cleanup="pending", cleanup_error="scheduler objects still present")
    except AutomationctlError as exc:
        with locked(state, execution_id) as data:
            data.update(cleanup="failed", cleanup_error=str(exc))
        raise


def prune(state: Path, keep: int, backend: Backend) -> int:
    """Verify terminal registrations are gone before applying per-name retention."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for data in discover(state):
        if data["status"] in TERMINAL and data["backend"] == backend.name:
            try:
                reconcile(state, data["id"], backend)
            except AutomationctlError:
                continue
            data = read(state, data["id"])
            if data["cleanup"] == "collected":
                groups.setdefault(data["name"], []).append(data)
    removed = 0
    for group in groups.values():
        group.sort(key=lambda data: data["created_at"], reverse=True)
        for data in group[keep:]:
            root = directory(state, data["id"])
            # A claimed/terminal execution can never launch again.
            shutil.rmtree(root)
            removed += 1
    return removed
