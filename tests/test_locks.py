"""Named fcntl locks."""

from __future__ import annotations

from pathlib import Path

import pytest

from automationctl.errors import AutomationctlError, LockBusy
from automationctl.locks import named_lock


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
