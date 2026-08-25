"""The install gate rejects runtime paths that depend on process cwd."""

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
InstallGate = tuple[Tree, Path, RecordingRunner]


@pytest.fixture
def install_gate(
    tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[InstallGate]:
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


@pytest.mark.parametrize(
    ("scope", "field"),
    [
        ("host", "path_prepend"),
        ("host", "env_files"),
        ("task", "cwd"),
        ("task", "env_files"),
    ],
)
def test_install_rejects_relative_runtime_paths_before_substrate_changes(
    install_gate: InstallGate, scope: str, field: str
) -> None:
    tree, unit_dir, recording = install_gate
    if scope == "host":
        tree.write_manifest(
            f'schema_version = 1\n\n[hosts.testhost]\ntasks = ["hello"]\n{field} = ["relative"]\n'
        )
        runtime_field = ""
    else:
        runtime_field = f'{field} = "relative"\n' if field == "cwd" else f'{field} = ["relative"]\n'
    tree.write_task("hello", f'description = "d"\ncommand = ["/usr/bin/true"]\n{runtime_field}')

    result = runner.invoke(
        app,
        [
            "install",
            "--dry-run",
            "--manifest",
            str(tree.manifest_path),
            "--host",
            "testhost",
        ],
    )

    assert result.exit_code == 1
    assert field in result.output
    assert not unit_dir.exists()
    assert recording.calls == []
