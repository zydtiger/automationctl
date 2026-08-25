"""Removal keeps generated files until the scheduler accepts deactivation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import Tree
from typer.testing import CliRunner, Result

from automationctl import backends
from automationctl.backends import Backend
from automationctl.cli import app
from automationctl.commands import CommandResult, RecordingRunner

runner = CliRunner()
RemovalCli = tuple[Tree, Path, RecordingRunner]


@pytest.fixture
def removal_cli(
    tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RemovalCli]:
    unit_dir = tmp_path / "units"
    recording = RecordingRunner()
    original = backends.create

    def fake_create(name: str, **kwargs: object) -> Backend:
        kwargs["runner"] = recording
        return original(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends, "create", fake_create)
    monkeypatch.setattr("automationctl.paths.state_dir", lambda env=None: tree.state)
    monkeypatch.setenv("AUTOMATIONCTL_UNIT_DIR", str(unit_dir))
    monkeypatch.setenv("AUTOMATIONCTL_EXECUTABLE", "/opt/bin/automationctl")
    monkeypatch.setenv("AUTOMATIONCTL_BACKEND", "systemd")
    yield tree, unit_dir, recording


def invoke(tree: Tree, *args: str) -> Result:
    return runner.invoke(
        app,
        [*args, "--manifest", str(tree.manifest_path), "--host", "testhost"],
    )


def test_uninstall_refusal_keeps_files_and_skips_reload(removal_cli: RemovalCli) -> None:
    tree, unit_dir, recording = removal_cli
    unit_dir.mkdir()
    target = unit_dir / "automationctl-hello.timer"
    target.write_text("stale\n", encoding="utf-8")
    disable = ("systemctl", "--user", "disable", "--now", target.name)
    recording.responses = {disable: CommandResult(disable, 1, stderr="refused")}

    result = invoke(tree, "uninstall", "--all")

    assert result.exit_code == 1
    assert target.exists()
    assert recording.transcript == ["systemctl --user disable --now automationctl-hello.timer"]


def test_reconcile_gc_refusal_keeps_old_files_and_applies_no_writes(
    removal_cli: RemovalCli,
) -> None:
    tree, unit_dir, recording = removal_cli
    tree.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    unit_dir.mkdir()
    stale_files = [unit_dir / "automationctl-old-a.timer", unit_dir / "automationctl-old-b.timer"]
    for stale in stale_files:
        stale.write_text("stale\n", encoding="utf-8")
    refused = ("systemctl", "--user", "disable", "--now", stale_files[1].name)
    recording.responses = {refused: CommandResult(refused, 1, stderr="refused")}

    result = invoke(tree, "install")

    assert result.exit_code == 1
    assert all(stale.exists() for stale in stale_files)
    assert not (unit_dir / "automationctl-hello.service").exists()
    assert recording.transcript == [
        "systemctl --user disable --now automationctl-old-a.timer",
        "systemctl --user disable --now automationctl-old-b.timer",
    ]
