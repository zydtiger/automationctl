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
from collections.abc import Callable, Collection, Mapping, Sequence
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

#: Changing the system timezone re-points this symlink, and launchd's
#: ``WatchPaths`` fires on the change. It is the only timezone signal launchd
#: offers; there is no equivalent for a clock step, which is what the optional
#: ``catchup_sweep`` interval exists to bound.
LOCALTIME_PATH = "/etc/localtime"

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
        state_dir: Path,
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
            "ProgramArguments": self.exec_argv(task, host=automations.host, jitter=jitter),
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

    def render_catchup(self, automations: Automations) -> str:
        """Render the one agent that recovers occurrences launchd will not replay.

        ``RunAtLoad`` covers power-off, ``WatchPaths`` covers a timezone
        change, and the optional sweep covers what is left: launchd exposes no
        clock-step event at all, so a bounded interval is the only way to put
        an upper bound on how long a stepped clock can hide a missed run. It
        is off unless the manifest asks for it, because polling is a cost the
        design does not pay by default.
        """
        data: dict[str, Any] = {
            "Label": CATCHUP_LABEL,
            "ProgramArguments": self.catchup_argv(host=automations.host),
            "ProcessType": "Background",
            "RunAtLoad": True,
            "WatchPaths": [LOCALTIME_PATH],
        }
        sweep = automations.manifest.defaults.catchup_sweep_seconds
        if sweep is not None:
            data["StartInterval"] = sweep
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
        # Unconditional, unlike the systemd units: this agent is also the
        # RunAtLoad power-off recovery every host wants, and its label is
        # reserved on every host whether or not it is rendered.
        files[plist_name(CATCHUP_LABEL)] = self.render_catchup(automations)
        return files

    # -- substrate operations ---------------------------------------------

    def _launchctl(self, *args: str) -> CommandResult:
        return self.runner.run([LAUNCHCTL, *args])

    def _label_of(self, filename: str) -> str:
        return filename[: -len(PLIST_SUFFIX)]

    def _domain_gate(self) -> Callable[[], bool]:
        """Return a "is the domain reachable?" probe memoized for one verb.

        launchctl reports "could not find service" for a label in a domain it
        cannot reach as readily as for a label that genuinely is not loaded,
        so 113 only means "not loaded" if the domain itself answers. The probe
        is lazy and runs at most once per verb: nothing pays for it unless a
        113 actually turns up.
        """
        answer: list[bool] = []

        def reachable() -> bool:
            if not answer:
                answer.append(self._launchctl("print", self.domain).ok)
            return answer[0]

        return reachable

    def _load_state(self, label: str, domain_ok: Callable[[], bool] | None = None) -> bool | None:
        """Read-only probe: ``True`` loaded, ``False`` not loaded, ``None`` unknown.

        Only launchctl's own "could not find service" code — and only when the
        domain is reachable — is read as a definite no. Any other failure, and
        any 113 from a domain that will not answer, is *unknown*, and callers
        must not mistake unknown for "there is nothing to stop": that turns a
        broken substrate into a silent success. The probe's own result is
        never reported as an operation, since asking a question is not a
        control command.
        """
        result = self._launchctl("print", self.service_target(label))
        if result.ok:
            return True
        if result.returncode == NOT_FOUND_RETURNCODE:
            gate = domain_ok if domain_ok is not None else self._domain_gate()
            return False if gate() else None
        return None

    def activate(
        self,
        automations: Automations,
        tasks: Sequence[TaskSpec],
        desired: Mapping[str, str] = MappingProxyType({}),
        rewritten: Collection[str] = (),
    ) -> list[CommandResult]:
        """Re-assert the domain's view of every desired agent.

        ``enable`` runs for all of them, because that is what clears a
        ``pause`` and makes "install re-asserts the repository state" true.
        The disruptive part — bootout followed by bootstrap — runs for agents
        whose definition differs from the one launchd last accepted, and for
        those whose file this reconcile just rewrote. Both signals are needed:
        the hash catches an activation that never landed, and ``rewritten``
        catches a plist that drifted on disk and was reloaded behind our back.
        Everything else is left alone, and is bootstrapped only if it is not
        loaded.
        """
        results: list[CommandResult] = []
        activated = records.read_activation(self.state_dir, self.name)
        domain_ok = self._domain_gate()
        labels = [label_for(task.name) for task in tasks] + [CATCHUP_LABEL]
        for label in labels:
            target = self.service_target(label)
            filename = plist_name(label)
            path = self.unit_dir / filename
            content = desired.get(filename)
            wanted = records.content_hash(content) if content is not None else None
            stale = wanted is None or activated.get(label) != wanted or filename in rewritten

            results.append(self._launchctl("enable", target))
            state = self._load_state(label, domain_ok)
            # An unknown state is booted out for the same reason deactivate
            # does it: a redundant bootout is cheap, and skipping one because
            # the probe could not answer leaves a stale definition running.
            if (stale or state is None) and state is not False:
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
        domain_ok = self._domain_gate()
        for filename in filenames:
            label = self._label_of(filename)
            activated.pop(label, None)
            if self._load_state(label, domain_ok) is False:
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

    def catchup_health(
        self, automations: Automations, tasks: Sequence[TaskSpec]
    ) -> list[HealthCheck]:
        path = self.unit_dir / plist_name(CATCHUP_LABEL)
        if not path.is_file():
            return [
                HealthCheck(
                    "catch-up triggers",
                    False,
                    f"{path} is missing; run automationctl install",
                )
            ]
        # Probe the plist on disk, not the manifest's wishes: an upgraded tool
        # or an edited sweep leaves the installed agent stale until `install`
        # rewrites it, and that is exactly when a false green would hide it.
        try:
            installed = path.read_text(encoding="utf-8")
        except OSError as exc:
            return [HealthCheck("catch-up triggers", False, f"{path} is unreadable: {exc}")]
        if installed != self.render_catchup(automations):
            return [
                HealthCheck(
                    "catch-up triggers",
                    False,
                    f"{path} is stale; run automationctl install",
                )
            ]
        sweep = automations.manifest.defaults.catchup_sweep_seconds
        # There is no clock-step event to probe for, so the report says which
        # triggers this agent actually carries and leaves the operator to judge
        # whether the sweep is worth its cost on this machine.
        detail = f"{CATCHUP_LABEL} watches {LOCALTIME_PATH} and runs at load"
        detail += f", sweeping every {sweep}s" if sweep is not None else "; no sweep configured"
        return [HealthCheck("catch-up triggers", True, detail)]
