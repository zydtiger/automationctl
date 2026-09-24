"""systemd user-unit rendering for installed standalone tasks."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence

from .. import records
from ..commands import CommandResult
from ..schedule import to_systemd
from ..spec import MachinePolicy, TaskSpec, effective_persistent
from . import GENERATED_HEADER, Backend, HealthCheck

UNIT_PREFIX, SERVICE_SUFFIX, TIMER_SUFFIX = "automationctl-", ".service", ".timer"
CATCHUP_SERVICE, CATCHUP_TIMER = "automationctl-catchup.service", "automationctl-catchup.timer"
CLOCK_TRIGGER_MIN_VERSION = 242
_VERSION_RE = re.compile(r"systemd\s+(\d+)")


def service_name(name: str) -> str:
    return f"{UNIT_PREFIX}{name}{SERVICE_SUFFIX}"


def timer_name(name: str) -> str:
    return f"{UNIT_PREFIX}{name}{TIMER_SUFFIX}"


def _quote(value: str, *, exec_start: bool) -> str:
    value = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if exec_start:
        value = value.replace("$", "$$")
    return (
        f'"{value}"'
        if not value or any(char.isspace() or char in '"\\' for char in value)
        else value
    )


class SystemdBackend(Backend):
    name = "systemd"

    def __init__(self, *args: object, uid: int | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.uid = os.getuid() if uid is None else uid

    def task_filenames(self, task: TaskSpec) -> tuple[str, ...]:
        return (
            (service_name(task.name),)
            if task.schedule is None
            else (service_name(task.name), timer_name(task.name))
        )

    def possible_task_filenames(self, name: str) -> tuple[str, ...]:
        return service_name(name), timer_name(name)

    def _environment(self) -> list[str]:
        return [
            f"Environment={_quote('XDG_CONFIG_HOME=' + str(self.config_home), exec_start=False)}",
            f"Environment={_quote('XDG_STATE_HOME=' + str(self.state_home), exec_start=False)}",
        ]

    def desired_task_files(self, task: TaskSpec) -> dict[str, str]:
        exec_start = " ".join(_quote(value, exec_start=True) for value in self.exec_argv(task))
        service = [
            f"# {GENERATED_HEADER}",
            "",
            "[Unit]",
            f"Description=automationctl: {task.name}",
            "",
            "[Service]",
            "Type=oneshot",
            *self._environment(),
            f"ExecStart={exec_start}",
        ]
        if task.timeout_seconds is not None:
            service.append(f"TimeoutStartSec={task.timeout_seconds + 300}")
        result = {service_name(task.name): "\n".join(service) + "\n"}
        if task.schedule is not None:
            timing = to_systemd(task.schedule)
            timer = [
                f"# {GENERATED_HEADER}",
                "",
                "[Unit]",
                f"Description=automationctl timer: {task.name}",
                "",
                "[Timer]",
                f"Unit={service_name(task.name)}",
            ]
            timer.extend(f"OnCalendar={item}" for item in timing.on_calendar)
            if timing.on_boot_sec:
                timer.append(f"OnBootSec={timing.on_boot_sec}")
            if timing.on_unit_active_sec:
                timer.append(f"OnUnitActiveSec={timing.on_unit_active_sec}")
            if timing.on_calendar and effective_persistent(task):
                timer.append("Persistent=true")
            if task.jitter_seconds:
                timer.append(f"RandomizedDelaySec={task.jitter_seconds}")
            timer.extend(["", "[Install]", "WantedBy=timers.target"])
            result[timer_name(task.name)] = "\n".join(timer) + "\n"
        return result

    def desired_catchup_files(self, policy: MachinePolicy) -> dict[str, str]:
        exec_start = " ".join(_quote(value, exec_start=True) for value in self.catchup_argv())
        service = (
            "\n".join(
                [
                    f"# {GENERATED_HEADER}",
                    "",
                    "[Unit]",
                    "Description=automationctl: catch up missed occurrences",
                    "",
                    "[Service]",
                    "Type=oneshot",
                    *self._environment(),
                    f"ExecStart={exec_start}",
                ]
            )
            + "\n"
        )
        timer = (
            "\n".join(
                [
                    f"# {GENERATED_HEADER}",
                    "",
                    "[Unit]",
                    "Description=automationctl timer: catch up missed occurrences",
                    "",
                    "[Timer]",
                    f"Unit={CATCHUP_SERVICE}",
                    "OnBootSec=2m",
                    "OnClockChange=true",
                    "OnTimezoneChange=true",
                    "",
                    "[Install]",
                    "WantedBy=timers.target",
                ]
            )
            + "\n"
        )
        return {CATCHUP_SERVICE: service, CATCHUP_TIMER: timer}

    def _systemctl(self, *args: str) -> CommandResult:
        return self.runner.run(["systemctl", "--user", *args])

    def reload(self) -> list[CommandResult]:
        return [self._systemctl("daemon-reload")]

    def activate(
        self, task: TaskSpec, desired: Mapping[str, str], rewritten: set[str]
    ) -> list[CommandResult]:
        if task.schedule is None:
            return []
        return [
            self._systemctl(
                "disable" if task.disabled else "enable", "--now", timer_name(task.name)
            )
        ]

    def activate_catchup(
        self, desired: Mapping[str, str], rewritten: set[str]
    ) -> list[CommandResult]:
        if CATCHUP_TIMER not in desired:
            return []
        activated = records.read_activation(self.state_dir, self.name)
        wanted = records.content_hash(desired[CATCHUP_TIMER])
        if activated.get(CATCHUP_TIMER) == wanted and CATCHUP_TIMER not in rewritten:
            return []
        results = [self._systemctl("enable", "--now", CATCHUP_TIMER)]
        if results[0].ok:
            activated[CATCHUP_TIMER] = wanted
            records.write_activation(self.state_dir, self.name, activated)
        return results

    def deactivate(self, filenames: Sequence[str]) -> list[CommandResult]:
        results: list[CommandResult] = []
        for name in sorted(filename for filename in filenames if filename.endswith(TIMER_SUFFIX)):
            results.append(self._systemctl("disable", "--now", name))
        if any(not result.ok for result in results):
            return results
        if CATCHUP_TIMER in filenames:
            activation = records.read_activation(self.state_dir, self.name)
            if CATCHUP_TIMER in activation:
                activation.pop(CATCHUP_TIMER)
                records.write_activation(self.state_dir, self.name, activation)
        for name in sorted(filename for filename in filenames if filename.endswith(SERVICE_SUFFIX)):
            results.append(self._systemctl("stop", name))
        return results

    def submit(self, task: TaskSpec) -> list[CommandResult]:
        return [self._systemctl("start", service_name(task.name))]

    def pause(self, task: TaskSpec) -> list[CommandResult]:
        return (
            []
            if task.schedule is None
            else [self._systemctl("disable", "--now", timer_name(task.name))]
        )

    def resume(self, task: TaskSpec) -> list[CommandResult]:
        return (
            []
            if task.schedule is None
            else [self._systemctl("enable", "--now", timer_name(task.name))]
        )

    def enabled(self, task: TaskSpec) -> bool | None:
        if task.schedule is None:
            return None
        result = self._systemctl("is-enabled", timer_name(task.name))
        return None if not result.stdout.strip() else result.stdout.strip() == "enabled"

    def follow_argv(self, task: TaskSpec) -> tuple[str, ...] | None:
        return "journalctl", "--user", "-u", service_name(task.name), "-f"

    def health(self) -> list[HealthCheck]:
        state = self._systemctl("is-system-running")
        text = state.stdout.strip() or state.stderr.strip() or "unknown"
        failed = self._systemctl("list-units", "--state=failed", "--no-legend", f"{UNIT_PREFIX}*")
        count = len([line for line in failed.stdout.splitlines() if line.strip()])
        linger = self.runner.run(
            ["loginctl", "show-user", str(self.uid), "--property=Linger", "--value"]
        )
        value = linger.stdout.strip()
        return [
            HealthCheck("backend", state.returncode in {0, 1}, f"systemd user manager: {text}"),
            HealthCheck("failed units", count == 0, f"{count} failed managed unit(s)"),
            HealthCheck(
                "linger",
                value == "yes",
                "enabled" if value == "yes" else f"not enabled ({value or 'unknown'})",
            ),
        ]

    def version(self) -> int | None:
        result = self._systemctl("--version")
        match = _VERSION_RE.search(result.stdout or result.stderr)
        return int(match.group(1)) if match is not None else None

    def catchup_health(self, tasks: Sequence[TaskSpec], policy: MachinePolicy) -> list[HealthCheck]:
        from .. import catchup

        if not catchup.triggers_wanted(tasks):
            return [HealthCheck("catch-up triggers", True, "not needed")]
        desired = self.desired_catchup_files(policy)
        for name, content in desired.items():
            path = self.unit_dir / name
            try:
                installed = path.read_text(encoding="utf-8")
            except OSError:
                return [HealthCheck("catch-up triggers", False, f"{path} is missing")]
            if installed != content:
                return [HealthCheck("catch-up triggers", False, f"{path} is stale")]
        running = self.version()
        supported = running is not None and running >= CLOCK_TRIGGER_MIN_VERSION
        return [
            HealthCheck("catch-up triggers", True, "installed and current"),
            HealthCheck(
                "clock triggers",
                supported,
                f"systemd {running} supports OnTimezoneChange="
                if supported
                else (
                    f"systemd {running} predates OnTimezoneChange= "
                    f"(needs {CLOCK_TRIGGER_MIN_VERSION}+)"
                    if running is not None
                    else "cannot determine the systemd version"
                ),
            ),
        ]
