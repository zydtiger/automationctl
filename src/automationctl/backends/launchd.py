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
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .. import records
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

#: launchctl's exit code for "could not find service" — a definite "not loaded".
#: Every other failure, 127 (launchctl absent) included, means "unknown".
NOT_FOUND_RETURNCODE = 113


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
        state_dir: Path | None = None,
        uid: int | None = None,
    ) -> None:
        super().__init__(
            unit_dir=unit_dir,
            runner=runner,
            executable=executable,
            manifest_path=manifest_path,
            state_dir=state_dir,
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

    def _load_state(self, label: str) -> bool | None:
        """Read-only probe: ``True`` loaded, ``False`` not loaded, ``None`` unknown.

        Only launchctl's own "could not find service" code is read as a
        definite no. Any other failure — launchctl missing, the domain
        unreachable — is *unknown*, and callers must not mistake it for "there
        is nothing to stop": that turns a broken substrate into a silent
        success. The probe's own result is never reported as an operation,
        since asking a question is not a control command.
        """
        result = self._launchctl("print", self.service_target(label))
        if result.ok:
            return True
        if result.returncode == NOT_FOUND_RETURNCODE:
            return False
        return None

    def activate(
        self,
        automations: Automations,
        tasks: Sequence[TaskSpec],
        desired: Mapping[str, str] = MappingProxyType({}),
    ) -> list[CommandResult]:
        """Re-assert the domain's view of every desired agent.

        ``enable`` runs for all of them, because that is what clears a
        ``pause`` and makes "install re-asserts the repository state" true.
        The disruptive part — bootout followed by bootstrap — runs only for
        agents whose definition differs from the one launchd last accepted;
        everything else is left alone, and is bootstrapped only if it is not
        loaded. An agent whose activation did not land keeps a stale recorded
        hash, so the next install retries it.
        """
        results: list[CommandResult] = []
        activated = records.read_activation(self.state_dir, self.name)
        labels = [label_for(task.name) for task in tasks] + [CATCHUP_LABEL]
        for label in labels:
            target = self.service_target(label)
            filename = plist_name(label)
            path = self.unit_dir / filename
            content = desired.get(filename)
            wanted = records.content_hash(content) if content is not None else None
            stale = wanted is None or activated.get(label) != wanted

            results.append(self._launchctl("enable", target))
            state = self._load_state(label)
            if stale and state is not False:
                results.append(self._launchctl("bootout", target))
                state = False
            if state is not True:
                started = self._launchctl("bootstrap", self.domain, str(path))
                results.append(started)
                if started.ok and wanted is not None:
                    activated[label] = wanted
            elif wanted is not None:
                activated[label] = wanted
        records.write_activation(self.state_dir, self.name, activated)
        return results

    def deactivate(self, filenames: Sequence[str]) -> list[CommandResult]:
        """Unload the agents behind the given generated files.

        A label the probe cannot speak for is booted out anyway: leaving an
        agent bootstrapped against a plist we are about to delete is worse
        than a redundant ``bootout``, and the command's own result decides
        whether the verb succeeded.
        """
        results: list[CommandResult] = []
        activated = records.read_activation(self.state_dir, self.name)
        for filename in filenames:
            label = self._label_of(filename)
            activated.pop(label, None)
            if self._load_state(label) is False:
                continue
            results.append(self._launchctl("bootout", self.service_target(label)))
        records.write_activation(self.state_dir, self.name, activated)
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
        return self._load_state(label_for(task.name))

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
