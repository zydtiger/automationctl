"""Mutual-exclusion locks backed by ``fcntl.flock``.

A lock is a file in the state directory held open for the lifetime of a run.
The kernel releases it when the process exits, so a crashed run never leaves a
stale lock behind. Contention is not an error: the caller records ``skipped``.

Two kinds exist. A *named* lock is the mutex a spec's ``lock`` field declares,
shared by every task naming it. A *run* lock is implicit, one per task, and is
held by every ``exec`` whether or not the spec declares a named one.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from . import records
from .errors import AutomationctlError, LockBusy

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Implicit per-task run locks live in a subdirectory of the lock directory.
#: User lock names are flat files directly in it, and a name containing a
#: separator is rejected, so no declared lock can ever name a run lock.
TASK_LOCK_DIR = "tasks"


def lock_path(lock_dir: Path, name: str) -> Path:
    """Return the lock file path for a lock name, validating the name."""
    if not _NAME_RE.match(name):
        raise AutomationctlError(
            f"invalid lock name: {name!r} (letters, digits, dot, dash, underscore)"
        )
    return lock_dir / f"{name}.lock"


def run_lock_path(lock_dir: Path, task: str) -> Path:
    """Return the implicit run-lock path for a task."""
    return lock_path(lock_dir / TASK_LOCK_DIR, task)


@contextmanager
def _hold(path: Path, busy: str) -> Iterator[Path]:
    """Hold an exclusive non-blocking lock, raising :class:`LockBusy` if taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusy(busy) from exc
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        yield path
    finally:
        os.close(handle)


def named_lock(lock_dir: Path, name: str) -> AbstractContextManager[Path]:
    """Hold the mutex a task's ``lock`` field declares."""
    records.ensure_private_dir(lock_dir)
    return _hold(lock_path(lock_dir, name), f"lock {name!r} is held by another run")


def run_lock(lock_dir: Path, task: str) -> AbstractContextManager[Path]:
    """Hold the implicit run lock every ``exec`` of a task takes.

    It is independent of the optional named lock and cannot collide with one,
    which is what makes two overlapping triggers of the same task converge on
    one run and one skip even when the spec declares no ``lock``.
    """
    records.ensure_private_dir(lock_dir)
    records.ensure_private_dir(lock_dir / TASK_LOCK_DIR)
    return _hold(run_lock_path(lock_dir, task), f"task {task!r} is already running")
