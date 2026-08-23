"""The launchd LaunchAgent backend.

Generated plists are dumb: ``ProgramArguments`` starts ``automationctl exec``
and a start condition expresses the schedule. Because launchd has no
randomized-delay control and does not replay runs missed across power-off, the
plists pass ``--jitter`` where the spec asks for it and one extra
``automationctl.catchup`` agent runs at load.
"""

from __future__ import annotations

import os
import plistlib
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from ..commands import CommandResult, CommandRunner
from ..config import Automations
from ..errors import BackendError
from ..schedule import to_launchd
from ..spec import TaskSpec, effective_randomized_delay
from . import GENERATED_HEADER, Backend, HealthCheck

LABEL_PREFIX = "automationctl."
PLIST_SUFFIX = ".plist"
CATCHUP_LABEL = "automationctl.catchup"
LAUNCHCTL = "launchctl"


def label_for(task: str) -> str:
    return f"{LABEL_PREFIX}{task}"


def plist_name(label: str) -> str:
    return f"{label}{PLIST_SUFFIX}"


def dump_plist(data: dict[str, Any]) -> str:
    """Serialize a plist dictionary to XML text with a generated-file marker."""
    xml = plistlib.dumps(data, sort_keys=True).decode("utf-8")
    marker = f"<!-- {GENERATED_HEADER} -->\n"
    head, sep, tail = xml.partition("\n")
    if not sep:  # pragma: no cover - plistlib always emits multiple lines
        return marker + xml
    return f"{head}\n{marker}{tail}"


class LaunchdBackend(Backend):
    """Compiles task specs into launchd user agents."""

    name = "launchd"

    def __init__(
        self,
        *,
        unit_dir: Path,
        runner: CommandRunner,
        executable: str,
        manifest_path: Path,
        uid: int | None = None,
    ) -> None:
        super().__init__(
            unit_dir=unit_dir,
            runner=runner,
            executable=executable,
            manifest_path=manifest_path,
        )
        self.uid = uid if uid is not None else os.getuid()

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def service_target(self, label: str) -> str:
        return f"{self.domain}/{label}"

    # -- rendering ---------------------------------------------------------

    def is_managed(self, filename: str) -> bool:
        return filename.startswith(LABEL_PREFIX) and filename.endswith(PLIST_SUFFIX)

    def task_filenames(self, task: TaskSpec) -> tuple[str, ...]:
        return (plist_name(label_for(task.name)),)

    def render_task(self, automations: Automations, task: TaskSpec) -> str:
        # Jitter spreads scheduled starts. A task with no schedule only ever
        # runs because someone asked for it now, and `submit` must be immediate
        # on both platforms, so an unscheduled agent never carries --jitter.
        jitter = (
            task.schedule is not None and effective_randomized_delay(automations.manifest, task) > 0
        )
        data: dict[str, Any] = {
            "Label": label_for(task.name),
            "ProgramArguments": self.exec_argv(task, jitter=jitter),
            "ProcessType": "Background",
            "RunAtLoad": False,
        }
        if task.schedule is not None:
            timing = to_launchd(task.schedule)
            if timing.start_interval is not None:
                data["StartInterval"] = timing.start_interval
            elif len(timing.start_calendar_interval) == 1:
                data["StartCalendarInterval"] = dict(timing.start_calendar_interval[0])
            else:
                data["StartCalendarInterval"] = [
                    dict(entry) for entry in timing.start_calendar_interval
                ]
        return dump_plist(data)

    def render_catchup(self) -> str:
        data: dict[str, Any] = {
            "Label": CATCHUP_LABEL,
            "ProgramArguments": [
                self.executable,
                "catch-up",
                "--manifest",
                str(self.manifest_path),
            ],
            "ProcessType": "Background",
            "RunAtLoad": True,
        }
        return dump_plist(data)

    def desired_files(self, automations: Automations, tasks: Sequence[TaskSpec]) -> dict[str, str]:
        files: dict[str, str] = {}
        for task in tasks:
            name = plist_name(label_for(task.name))
            if name == plist_name(CATCHUP_LABEL):
                # lint reserves the name; refuse rather than silently drop the
                # task by overwriting it with the catch-up agent below.
                raise BackendError(
                    f"task {task.name!r} collides with the reserved {CATCHUP_LABEL} agent"
                )
            files[name] = self.render_task(automations, task)
        files[plist_name(CATCHUP_LABEL)] = self.render_catchup()
        return files

    # -- substrate operations ---------------------------------------------

    def _launchctl(self, *args: str) -> CommandResult:
        return self.runner.run([LAUNCHCTL, *args])

    def _label_of(self, filename: str) -> str:
        return filename[: -len(PLIST_SUFFIX)]

    def _loaded(self, label: str) -> bool:
        """Read-only probe: is this label bootstrapped in the domain?

        Its result is deliberately not reported to the caller — asking whether
        something is loaded is not a control command, and a "no" must not be
        counted as a failed operation.
        """
        return self._launchctl("print", self.service_target(label)).ok

    def activate(
        self,
        automations: Automations,
        tasks: Sequence[TaskSpec],
        changed: Collection[str] = (),
    ) -> list[CommandResult]:
        """Re-assert the domain's view of every desired agent.

        ``enable`` runs for all of them, because that is what clears a
        ``pause`` and makes "install re-asserts the repository state" true.
        The disruptive part — bootout followed by bootstrap — runs only for
        agents whose plist this reconcile actually rewrote; everything else is
        left alone, and is only bootstrapped if it is not loaded at all.
        """
        results: list[CommandResult] = []
        labels = [label_for(task.name) for task in tasks] + [CATCHUP_LABEL]
        for label in labels:
            target = self.service_target(label)
            path = self.unit_dir / plist_name(label)
            results.append(self._launchctl("enable", target))
            loaded = self._loaded(label)
            if loaded and plist_name(label) in changed:
                results.append(self._launchctl("bootout", target))
                loaded = False
            if not loaded:
                results.append(self._launchctl("bootstrap", self.domain, str(path)))
        return results

    def deactivate(self, filenames: Sequence[str]) -> list[CommandResult]:
        results: list[CommandResult] = []
        for filename in filenames:
            label = self._label_of(filename)
            if self._loaded(label):
                results.append(self._launchctl("bootout", self.service_target(label)))
        return results

    def submit(self, task: TaskSpec) -> list[CommandResult]:
        return [self._launchctl("kickstart", "-k", self.service_target(label_for(task.name)))]

    def pause(self, task: TaskSpec) -> list[CommandResult]:
        target = self.service_target(label_for(task.name))
        return [self._launchctl("disable", target), self._launchctl("bootout", target)]

    def resume(self, task: TaskSpec) -> list[CommandResult]:
        label = label_for(task.name)
        path = self.unit_dir / plist_name(label)
        return [
            self._launchctl("enable", self.service_target(label)),
            self._launchctl("bootstrap", self.domain, str(path)),
        ]

    def enabled(self, task: TaskSpec) -> bool | None:
        result = self._launchctl("print", self.service_target(label_for(task.name)))
        if result.returncode == 127:
            return None
        return result.ok

    def follow_argv(self, task: TaskSpec) -> tuple[str, ...] | None:
        return None

    def health(self) -> list[HealthCheck]:
        domain = self._launchctl("print", self.domain)
        return [
            HealthCheck(
                "backend",
                domain.ok,
                f"launchd domain {self.domain}"
                + ("" if domain.ok else f" unreachable: {domain.stderr.strip()}"),
            )
        ]
