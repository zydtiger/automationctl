"""Task ownership and recovery of the Home Manager sync, without a scheduler."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "sync_tasks", Path(__file__).resolve().parents[1] / "nix/sync_tasks.py"
)
assert _spec is not None and _spec.loader is not None
sync: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


class Scheduler:
    """Records automationctl invocations and applies them to a fake task set."""

    def __init__(self, root: Path) -> None:
        self.tasks = root / "tasks"
        self.tasks.mkdir()
        self.state = root / "state/owned.json"
        self.calls: list[list[str]] = []
        self.installed = {"unmanaged": "keep"}
        self.fail: Callable[[list[str]], bool] | None = None

    def task(self, name: str, value: str = "old") -> None:
        (self.tasks / f"{name}.toml").write_text(f'name = "{name}"\nvalue = "{value}"\n')

    def run(self, argv: list[str], *, check: bool) -> None:
        assert check
        self.calls.append(argv)
        if self.fail and self.fail(argv):
            raise subprocess.CalledProcessError(1, argv)
        if argv[1] == "remove":
            self.installed.pop(argv[2], None)
        elif "--dry-run" not in argv:
            path = Path(argv[2])
            self.installed[path.stem] = path.read_text()

    def synchronize(self, *, dry_run: bool = False) -> None:
        sync.synchronize(
            self.tasks,
            self.state,
            "/nix/store/test/bin/automationctl",
            "launchd",
            dry_run=dry_run,
            run=self.run,
        )

    def owned(self) -> list[str]:
        owned: list[str] = json.loads(self.state.read_text())
        return owned


@pytest.fixture
def scheduler(tmp_path: Path) -> Scheduler:
    return Scheduler(tmp_path)


def real_install(argv: list[str]) -> bool:
    return argv[1] == "install" and "--dry-run" not in argv


def test_add_update_repeat_and_remove_preserve_unmanaged(scheduler: Scheduler) -> None:
    scheduler.task("owned")
    scheduler.synchronize()
    assert scheduler.owned() == ["owned"]
    scheduler.task("owned", "new")
    scheduler.synchronize()
    scheduler.synchronize()
    assert 'value = "new"' in scheduler.installed["owned"]
    (scheduler.tasks / "owned.toml").unlink()
    scheduler.synchronize()
    assert scheduler.installed == {"unmanaged": "keep"}
    assert scheduler.owned() == []


def test_unchanged_inputs_skip_scheduler_until_tasks_change(scheduler: Scheduler) -> None:
    scheduler.task("owned")
    scheduler.synchronize()
    scheduler.calls.clear()
    scheduler.synchronize()
    assert scheduler.calls == []
    scheduler.task("owned", "new")
    scheduler.synchronize()
    assert 'value = "new"' in scheduler.installed["owned"]


def test_failed_sync_is_retried_despite_previous_digest(scheduler: Scheduler) -> None:
    scheduler.task("owned")
    scheduler.synchronize()
    scheduler.task("owned", "new")
    scheduler.fail = real_install
    with pytest.raises(subprocess.CalledProcessError):
        scheduler.synchronize()
    scheduler.fail = None
    scheduler.task("owned")
    scheduler.calls.clear()
    scheduler.synchronize()
    assert scheduler.calls != []


def test_preflight_failure_does_not_remove_or_install(scheduler: Scheduler) -> None:
    scheduler.task("old")
    scheduler.synchronize()
    (scheduler.tasks / "old.toml").unlink()
    scheduler.task("new")
    scheduler.fail = lambda argv: "--dry-run" in argv
    with pytest.raises(subprocess.CalledProcessError):
        scheduler.synchronize()
    assert "old" in scheduler.installed
    assert "new" not in scheduler.installed
    assert scheduler.owned() == ["old"]


def test_failed_install_is_owned_and_retryable(scheduler: Scheduler) -> None:
    scheduler.task("new")
    scheduler.fail = real_install
    with pytest.raises(subprocess.CalledProcessError):
        scheduler.synchronize()
    assert scheduler.owned() == ["new"]
    scheduler.fail = None
    scheduler.synchronize()
    assert "new" in scheduler.installed


def test_failed_remove_retains_ownership_for_retry(scheduler: Scheduler) -> None:
    scheduler.task("old")
    scheduler.synchronize()
    (scheduler.tasks / "old.toml").unlink()
    scheduler.fail = lambda argv: argv[1] == "remove"
    with pytest.raises(subprocess.CalledProcessError):
        scheduler.synchronize()
    assert scheduler.owned() == ["old"]
    scheduler.fail = None
    scheduler.synchronize()
    assert "old" not in scheduler.installed


def test_dry_run_creates_no_state_or_installed_tasks(scheduler: Scheduler) -> None:
    scheduler.task("new")
    scheduler.synchronize(dry_run=True)
    assert not scheduler.state.parent.exists()
    assert scheduler.installed == {"unmanaged": "keep"}
    assert all("--dry-run" in argv for argv in scheduler.calls)


def test_dry_run_does_not_remove_existing_task(scheduler: Scheduler) -> None:
    scheduler.task("old")
    scheduler.synchronize()
    before = scheduler.state.read_bytes()
    (scheduler.tasks / "old.toml").unlink()
    scheduler.calls.clear()
    scheduler.synchronize(dry_run=True)
    assert scheduler.calls == []
    assert "old" in scheduler.installed
    assert scheduler.state.read_bytes() == before


def test_name_mismatch_fails_before_scheduler_operations(scheduler: Scheduler) -> None:
    (scheduler.tasks / "one.toml").write_text('name = "two"\n')
    with pytest.raises(ValueError):
        scheduler.synchronize()
    assert scheduler.calls == []


def test_missing_directory_does_not_prune_owned_tasks(scheduler: Scheduler) -> None:
    scheduler.task("old")
    scheduler.synchronize()
    scheduler.tasks = scheduler.tasks.parent / "missing"
    scheduler.calls.clear()
    with pytest.raises(ValueError):
        scheduler.synchronize()
    assert scheduler.calls == []
    assert "old" in scheduler.installed
