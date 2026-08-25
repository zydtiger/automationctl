"""Manual tasks reject verbs that only control automatic schedules."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import Tree
from typer.testing import CliRunner

from automationctl import backends
from automationctl.backends import Backend
from automationctl.cli import app
from automationctl.commands import RecordingRunner

runner = CliRunner()
ScheduleControl = tuple[Tree, Path, RecordingRunner]


@pytest.fixture
def schedule_control(
    tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ScheduleControl]:
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
    yield tree, unit_dir, recording


@pytest.mark.parametrize("backend", ["systemd", "launchd"])
@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_manual_task_rejects_schedule_control_without_substrate_calls(
    schedule_control: ScheduleControl, backend: str, verb: str
) -> None:
    tree, unit_dir, recording = schedule_control
    tree.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')

    result = runner.invoke(
        app,
        [
            verb,
            "hello",
            "--manifest",
            str(tree.manifest_path),
            "--host",
            "testhost",
            "--backend",
            backend,
        ],
    )

    assert result.exit_code == 2
    assert f"cannot {verb} manual task 'hello': task has no schedule" in result.output
    assert f"{verb}d hello" not in result.output
    assert recording.calls == []
    assert not unit_dir.exists()
