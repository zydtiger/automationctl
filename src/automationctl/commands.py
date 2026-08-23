"""The seam through which scheduler control commands are executed.

Every ``systemctl``/``launchctl`` invocation goes through a
:class:`CommandRunner`. Production uses :class:`SubprocessRunner`; tests inject
:class:`RecordingRunner`, so no test can ever mutate the host's scheduler.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one control-plane command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def display(self) -> str:
        return " ".join(self.argv)


class CommandRunner(Protocol):
    """Runs a control command and returns its result."""

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult: ...


@dataclass
class SubprocessRunner:
    """Executes control commands as real subprocesses."""

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        items = tuple(str(item) for item in argv)
        try:
            completed = subprocess.run(
                items,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return CommandResult(argv=items, returncode=127, stderr=str(exc))
        except subprocess.TimeoutExpired as exc:
            return CommandResult(argv=items, returncode=124, stderr=str(exc))
        return CommandResult(
            argv=items,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


@dataclass
class RecordingRunner:
    """Records control commands instead of running them.

    ``responses`` maps an exact argv tuple to a canned result; anything else
    succeeds silently. ``calls`` is the ordered transcript.
    """

    responses: Mapping[tuple[str, ...], CommandResult] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        items = tuple(str(item) for item in argv)
        self.calls.append(items)
        canned = self.responses.get(items)
        if canned is not None:
            return canned
        return CommandResult(argv=items, returncode=0)

    @property
    def transcript(self) -> list[str]:
        return [" ".join(call) for call in self.calls]
