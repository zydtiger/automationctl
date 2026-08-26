"""Named and implicit fcntl locks."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from automationctl import locks, spec
from automationctl.errors import AutomationctlError, LockBusy
from automationctl.locks import named_lock, run_lock


def test_lock_is_created_and_released(tmp_path: Path) -> None:
    with named_lock(tmp_path, "gpu") as path:
        assert path == tmp_path / "gpu.lock"
        assert path.exists()
    with named_lock(tmp_path, "gpu"):
        pass


def test_contended_lock_raises_lock_busy(tmp_path: Path) -> None:
    with named_lock(tmp_path, "agents"), pytest.raises(LockBusy), named_lock(tmp_path, "agents"):
        pass


def test_distinct_names_do_not_contend(tmp_path: Path) -> None:
    with named_lock(tmp_path, "a"), named_lock(tmp_path, "b"):
        pass


@pytest.mark.parametrize("name", ["../escape", "a/b", "", ".hidden"])
def test_invalid_lock_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(AutomationctlError), named_lock(tmp_path, name):
        pass


def test_a_run_lock_lives_in_its_own_namespace(tmp_path: Path) -> None:
    """A named lock and a run lock spelled the same are different locks."""
    with run_lock(tmp_path, "gpu") as path:
        assert path == tmp_path / "tasks" / "gpu.lock"
        with named_lock(tmp_path, "gpu"):
            pass


def test_lock_directories_are_private(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"

    with run_lock(lock_dir, "audit"):
        pass

    assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((lock_dir / locks.TASK_LOCK_DIR).stat().st_mode) == 0o700


def test_a_contended_run_lock_names_the_task(tmp_path: Path) -> None:
    with (
        run_lock(tmp_path, "audit"),
        pytest.raises(LockBusy, match="already running"),
        run_lock(tmp_path, "audit"),
    ):
        pass


def test_task_names_are_always_valid_lock_names() -> None:
    """Every exec derives a run-lock path from the task name, so the two name
    grammars must not drift apart."""
    assert spec.TASK_NAME_RE.pattern == locks._NAME_RE.pattern
