"""Every shipped source example remains installable input."""

from __future__ import annotations

from pathlib import Path

from automationctl.config import load_task, materialize
from automationctl.lint import lint_task
from automationctl.spec import MachinePolicy


def test_examples_materialize_and_lint_on_both_backends() -> None:
    examples = Path(__file__).parents[1] / "examples" / "tasks"
    for path in sorted(examples.glob("*.toml")):
        task = materialize(load_task(path))
        policy = MachinePolicy(path.parent / "config.toml")
        for backend in ("systemd", "launchd"):
            report = lint_task(task, policy, backend)
            assert report.ok, (path, backend, report.render())
