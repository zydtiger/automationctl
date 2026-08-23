"""Hermetic fixtures: a temporary automations repository and state directory.

No test touches the real scheduler, the user's state directory, or the
network. Scheduler control commands go through a recording runner, and every
child process a test spawns is a stock POSIX tool.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from automationctl.commands import CommandResult, RecordingRunner
from automationctl.config import Automations, load

HOST = "testhost"
FIXED_EXECUTABLE = "/opt/bin/automationctl"
FIXED_MANIFEST = Path("/home/example/automations/manifest.toml")

MANIFEST = """\
schema_version = 1

[defaults]
timeout = "10m"

[hosts.testhost]
tasks = ["hello"]

[lint]
forbidden_argv = ["--danger"]
"""

RUNNERS = """\
schema_version = 1

[runners.stdin-runner]
argv = ["/bin/cat"]
stdin = "prompt"

[runners.argv-runner]
argv = ["/bin/echo", "{prompt}"]

[runners.full-runner]
argv = ["/bin/echo", "--danger"]
allow_full_access = true

# A child that is handed a prompt on stdin and never reads it.
[runners.sleep-runner]
argv = ["/bin/sleep", "30"]
stdin = "prompt"
"""


class FailingRunner(RecordingRunner):
    """Records control commands and reports every one of them as refused.

    Stands in for a scheduler that is absent or broken — the case where a
    silent success is the worst possible outcome.
    """

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        items = tuple(str(item) for item in argv)
        self.calls.append(items)
        return CommandResult(argv=items, returncode=1, stderr="refused")


@contextmanager
def local_tz(name: str) -> Iterator[None]:
    """Run a block with the process's local timezone forced to ``name``.

    Schedules are local wall clock, so the tests that prove it have to control
    what "local" means rather than inherit the build machine's zone.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def local_moment(text: str) -> datetime:
    """Parse ``YYYY-MM-DD HH:MM`` as a local wall-clock instant."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone()


@dataclass
class Tree:
    """A temporary automations repository plus its state directory."""

    root: Path
    state: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.toml"

    def write_manifest(self, text: str) -> None:
        self.manifest_path.write_text(text, encoding="utf-8")

    def write_runners(self, text: str) -> None:
        (self.root / "runners.toml").write_text(text, encoding="utf-8")

    def write_task(self, name: str, text: str) -> Path:
        path = self.root / "tasks" / f"{name}.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def write_prompt(self, name: str, text: str) -> Path:
        path = self.root / "prompts" / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def load(self, host: str = HOST) -> Automations:
        return load(manifest_path=self.manifest_path, host=host, env={})


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    root = tmp_path / "automations"
    (root / "tasks").mkdir(parents=True)
    (root / "prompts").mkdir(parents=True)
    built = Tree(root=root, state=tmp_path / "state")
    built.write_manifest(MANIFEST)
    built.write_runners(RUNNERS)
    return built
