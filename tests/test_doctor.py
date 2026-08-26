"""Read-only host probes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import FIXED_EXECUTABLE, FIXED_MANIFEST, Tree

from automationctl import doctor
from automationctl.backends.systemd import SystemdBackend
from automationctl.commands import RecordingRunner


def probe(tree: Tree, tmp_path: Path, env: dict[str, str] | None = None) -> doctor.DoctorReport:
    automations = tree.load()
    backend = SystemdBackend(
        unit_dir=tmp_path / "units",
        runner=RecordingRunner(),
        executable=FIXED_EXECUTABLE,
        manifest_path=FIXED_MANIFEST,
        state_dir=tree.state,
        uid=1000,
    )
    return doctor.run(
        automations,
        backend,
        env=env if env is not None else {"HOME": str(tmp_path)},
        state_dir=tree.state,
    )


def details(report: doctor.DoctorReport, name: str) -> list[str]:
    return [check.detail for check in report.checks if check.name == name]


def failures(report: doctor.DoctorReport) -> list[str]:
    return [check.detail for check in report.checks if not check.ok]


def test_binaries_are_resolved_through_the_env_file_path_override(
    tree: Tree, tmp_path: Path
) -> None:
    """An env file may set PATH; doctor must probe the PATH the run will get."""
    bin_dir = tmp_path / "opt" / "bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "only-here"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)

    env_file = tmp_path / "agent-env"
    env_file.write_text(f"PATH={bin_dir}:/usr/bin:/bin\n", encoding="utf-8")
    tree.write_manifest(
        f'schema_version = 1\n\n[hosts.testhost]\ntasks = ["hello"]\nenv_files = ["{env_file}"]\n'
    )
    tree.write_task("hello", 'description = "d"\ncommand = ["only-here"]\n')

    report = probe(tree, tmp_path)
    assert details(report, "binary") == [f"hello: only-here -> {tool}"]


def test_two_tasks_with_divergent_paths_are_probed_separately(tree: Tree, tmp_path: Path) -> None:
    """PATH is per task now, so deduping on the program name alone hides a gap."""
    good_dir = tmp_path / "good" / "bin"
    good_dir.mkdir(parents=True)
    tool = good_dir / "shared-tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)

    with_tool = tmp_path / "with-tool.env"
    with_tool.write_text(f"PATH={good_dir}:/usr/bin:/bin\n", encoding="utf-8")
    without_tool = tmp_path / "without-tool.env"
    without_tool.write_text("PATH=/usr/bin:/bin\n", encoding="utf-8")

    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["finds", "misses"]\n')
    tree.write_task(
        "finds",
        f'description = "d"\ncommand = ["shared-tool"]\nenv_files = ["{with_tool}"]\n',
    )
    tree.write_task(
        "misses",
        f'description = "d"\ncommand = ["shared-tool"]\nenv_files = ["{without_tool}"]\n',
    )

    report = probe(tree, tmp_path)
    binaries = details(report, "binary")
    assert len(binaries) == 2
    assert f"finds: shared-tool -> {tool}" in binaries
    assert "misses: shared-tool not found or not executable" in binaries
    assert not report.ok


def test_one_program_under_one_path_is_probed_once(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["a", "b"]\n')
    for name in ("a", "b"):
        tree.write_task(name, 'description = "d"\ncommand = ["/bin/echo", "hi"]\n')
    assert details(probe(tree, tmp_path), "binary") == ["a: /bin/echo -> /bin/echo"]


def test_a_missing_absolute_binary_is_reported(tree: Tree, tmp_path: Path) -> None:
    """An absolute path is not proof of existence."""
    tree.write_task("hello", f'description = "d"\ncommand = ["{tmp_path}/no-such-tool"]\n')
    report = probe(tree, tmp_path)
    assert details(report, "binary") == [
        f"hello: {tmp_path}/no-such-tool not found or not executable"
    ]
    assert not report.ok


def test_a_non_executable_absolute_binary_is_reported(tree: Tree, tmp_path: Path) -> None:
    target = tmp_path / "not-executable"
    target.write_text("", encoding="utf-8")
    target.chmod(0o644)
    tree.write_task("hello", f'description = "d"\ncommand = ["{target}"]\n')
    report = probe(tree, tmp_path)
    assert details(report, "binary") == [f"hello: {target} not found or not executable"]


def test_an_executable_absolute_binary_passes(tree: Tree, tmp_path: Path) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "hi"]\n')
    report = probe(tree, tmp_path)
    assert details(report, "binary") == ["hello: /bin/echo -> /bin/echo"]


def test_an_undeclared_host_fails_the_report(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest("schema_version = 1\n\n[hosts.elsewhere]\ntasks = []\n")
    report = probe(tree, tmp_path)
    assert not report.ok
    assert any("has no [hosts.*] section" in detail for detail in failures(report))


def test_a_missing_env_file_is_reported(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(
        "schema_version = 1\n\n[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'env_files = ["{tmp_path}/absent"]\n'
    )
    tree.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
    report = probe(tree, tmp_path)
    assert any("missing or unreadable" in detail for detail in details(report, "env file"))


def test_an_absent_state_dir_passes_when_its_parent_is_writable(tree: Tree, tmp_path: Path) -> None:
    report = probe(tree, tmp_path)
    check = next(item for item in report.checks if item.name == "state dir")

    assert check.ok is True
    assert check.detail == (
        f"{tree.state} is absent; lazy creation is available under writable parent {tmp_path}"
    )


def test_a_state_path_that_is_not_a_directory_fails(tree: Tree, tmp_path: Path) -> None:
    tree.state.write_text("not a directory\n", encoding="utf-8")
    report = probe(tree, tmp_path)
    check = next(item for item in report.checks if item.name == "state dir")

    assert check.ok is False
    assert check.detail == f"{tree.state} exists but is not a directory"


def test_a_dangling_state_dir_symlink_fails(tree: Tree, tmp_path: Path) -> None:
    tree.state.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    report = probe(tree, tmp_path)
    check = next(item for item in report.checks if item.name == "state dir")

    assert check.ok is False
    assert check.detail == f"{tree.state} exists but is not a directory"


def test_an_absent_state_dir_fails_when_its_parent_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "blocked"
    parent.mkdir()
    state = parent / "nested" / "automationctl"
    real_access = os.access

    def access(path: str | os.PathLike[str], mode: int) -> bool:
        return False if Path(path) == parent else real_access(path, mode)

    monkeypatch.setattr(os, "access", access)
    check = doctor._state_dir_check(state)

    assert check.ok is False
    assert check.detail == f"{state} is absent and cannot be created under {parent}"
