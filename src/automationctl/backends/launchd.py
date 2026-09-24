"""launchd rendering for installed standalone tasks."""

from __future__ import annotations

import os
import plistlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .. import records
from ..commands import CommandResult
from ..schedule import to_launchd
from ..spec import MachinePolicy, TaskSpec
from . import GENERATED_HEADER, Backend, HealthCheck

LABEL_PREFIX, PLIST_SUFFIX, CATCHUP_LABEL = "automationctl.", ".plist", "automationctl.catchup"


def label_for(name: str) -> str:
    return f"{LABEL_PREFIX}{name}"


def plist_name(label: str) -> str:
    return f"{label}{PLIST_SUFFIX}"


def dump_plist(data: dict[str, Any]) -> str:
    xml = plistlib.dumps(data, sort_keys=True).decode()
    first, separator, rest = xml.partition("\n")
    return f"{first}\n<!-- {GENERATED_HEADER} -->\n{rest}" if separator else xml


class LaunchdBackend(Backend):
    name = "launchd"

    def __init__(self, *args: object, uid: int | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.uid = os.getuid() if uid is None else uid

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def target(self, label: str) -> str:
        return f"{self.domain}/{label}"

    def task_filenames(self, task: TaskSpec) -> tuple[str, ...]:
        return (plist_name(label_for(task.name)),)

    def possible_task_filenames(self, name: str) -> tuple[str, ...]:
        return (plist_name(label_for(name)),)

    def _environment(self) -> dict[str, str]:
        return {"XDG_CONFIG_HOME": str(self.config_home), "XDG_STATE_HOME": str(self.state_home)}

    def desired_task_files(self, task: TaskSpec) -> dict[str, str]:
        data: dict[str, Any] = {
            "Label": label_for(task.name),
            "ProgramArguments": self.exec_argv(
                task, jitter=task.schedule is not None and task.jitter_seconds > 0
            ),
            "EnvironmentVariables": self._environment(),
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
                    dict(item) for item in timing.start_calendar_interval
                ]
        return {plist_name(label_for(task.name)): dump_plist(data)}

    def desired_catchup_files(self, policy: MachinePolicy) -> dict[str, str]:
        data: dict[str, Any] = {
            "Label": CATCHUP_LABEL,
            "ProgramArguments": self.catchup_argv(),
            "EnvironmentVariables": self._environment(),
            "ProcessType": "Background",
            "RunAtLoad": True,
            "WatchPaths": ["/etc/localtime"],
        }
        if policy.catchup_sweep_seconds is not None:
            data["StartInterval"] = policy.catchup_sweep_seconds
        return {plist_name(CATCHUP_LABEL): dump_plist(data)}

    def _launchctl(self, *args: str) -> CommandResult:
        return self.runner.run(["launchctl", *args])

    def _domain_gate(self) -> Callable[[], bool]:
        answer: list[bool] = []

        def reachable() -> bool:
            if not answer:
                answer.append(self._launchctl("print", self.domain).ok)
            return answer[0]

        return reachable

    def _load_state(self, label: str, domain_ok: Callable[[], bool] | None = None) -> bool | None:
        result = self._launchctl("print", self.target(label))
        if result.ok:
            return True
        if result.returncode == 113:
            return False if (domain_ok or self._domain_gate())() else None
        return None

    def _activate_label(
        self, label: str, desired: Mapping[str, str], rewritten: set[str]
    ) -> list[CommandResult]:
        filename, target = plist_name(label), self.target(label)
        activated = records.read_activation(self.state_dir, self.name)
        wanted = records.content_hash(desired[filename])
        results = [self._launchctl("enable", target)]
        if not results[0].ok:
            return results
        state = self._load_state(label, self._domain_gate())
        stale = activated.get(label) != wanted or filename in rewritten
        if (stale or state is None) and state is not False:
            stopped = self._launchctl("bootout", target)
            results.append(stopped)
            if not stopped.ok:
                return results
            state = False
        if state is not True:
            started = self._launchctl("bootstrap", self.domain, str(self.unit_dir / filename))
            results.append(started)
            if not started.ok:
                return results
        activated[label] = wanted
        records.write_activation(self.state_dir, self.name, activated)
        return results

    def activate(
        self, task: TaskSpec, desired: Mapping[str, str], rewritten: set[str]
    ) -> list[CommandResult]:
        if task.disabled:
            return self.pause(task)
        return self._activate_label(label_for(task.name), desired, rewritten)

    def activate_catchup(
        self, desired: Mapping[str, str], rewritten: set[str]
    ) -> list[CommandResult]:
        if plist_name(CATCHUP_LABEL) not in desired:
            return []
        return self._activate_label(CATCHUP_LABEL, desired, rewritten)

    def deactivate(self, filenames: Sequence[str]) -> list[CommandResult]:
        results: list[CommandResult] = []
        activation = records.read_activation(self.state_dir, self.name)
        domain_ok = self._domain_gate()
        for filename in filenames:
            if not filename.endswith(PLIST_SUFFIX):
                continue
            label = filename[: -len(PLIST_SUFFIX)]
            if self._load_state(label, domain_ok) is False:
                activation.pop(label, None)
                continue
            stopped = self._launchctl("bootout", self.target(label))
            results.append(stopped)
            if stopped.ok:
                activation.pop(label, None)
            else:
                break
        records.write_activation(self.state_dir, self.name, activation)
        return results

    def submit(self, task: TaskSpec) -> list[CommandResult]:
        return [self._launchctl("kickstart", "-k", self.target(label_for(task.name)))]

    def pause(self, task: TaskSpec) -> list[CommandResult]:
        if task.schedule is None:
            return []
        label = label_for(task.name)
        target = self.target(label)
        results = [self._launchctl("disable", target)]
        if not results[0].ok or self._load_state(label) is False:
            return results
        results.append(self._launchctl("bootout", target))
        return results

    def resume(self, task: TaskSpec) -> list[CommandResult]:
        if task.schedule is None:
            return []
        label = label_for(task.name)
        target = self.target(label)
        results = [self._launchctl("enable", target)]
        if not results[0].ok:
            return results
        state = self._load_state(label)
        if state is True:
            return results
        if state is None:
            stopped = self._launchctl("bootout", target)
            results.append(stopped)
            if not stopped.ok:
                return results
        results.append(
            self._launchctl("bootstrap", self.domain, str(self.unit_dir / plist_name(label)))
        )
        return results

    def enabled(self, task: TaskSpec) -> bool | None:
        return None if task.schedule is None else self._load_state(label_for(task.name))

    def follow_argv(self, task: TaskSpec) -> tuple[str, ...] | None:
        return None

    def health(self) -> list[HealthCheck]:
        result = self._launchctl("print", self.domain)
        return [
            HealthCheck(
                "backend", result.ok, result.stdout.strip() or result.stderr.strip() or "unknown"
            )
        ]
