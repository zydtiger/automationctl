"""Named mutual-exclusion locks backed by ``fcntl.flock``.

A lock is a file in the state directory held open for the lifetime of a run.
The kernel releases it when the process exits, so a crashed run never leaves a
stale lock behind. Contention is not an error: the caller records ``skipped``.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import AutomationctlError, LockBusy

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def lock_path(lock_dir: Path, name: str) -> Path:
    """Return the lock file path for a lock name, validating the name."""
    if not _NAME_RE.match(name):
        raise AutomationctlError(
            f"invalid lock name: {name!r} (letters, digits, dot, dash, underscore)"
        )
    return lock_dir / f"{name}.lock"


@contextmanager
def named_lock(lock_dir: Path, name: str) -> Iterator[Path]:
    """Hold an exclusive non-blocking lock, raising :class:`LockBusy` if taken."""
    path = lock_path(lock_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusy(f"lock {name!r} is held by another run") from exc
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        yield path
    finally:
        os.close(handle)
