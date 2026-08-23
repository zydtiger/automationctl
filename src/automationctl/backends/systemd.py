"""The systemd user-manager backend.

Generated units are dumb: a ``oneshot`` service that starts
``automationctl exec`` and, for scheduled tasks, a timer that starts the
service. Reconciliation is by filename prefix, so hand-written units are never
touched and stale generated units are garbage-collected.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from ..commands import CommandResult, CommandRunner
from ..config import Automations
from ..schedule import to_systemd
from ..spec import (
    TaskSpec,
    effective_persistent,
    effective_randomized_delay,
    effective_timeout,
)
from . import GENERATED_HEADER, Backend, HealthCheck

UNIT_PREFIX = "automationctl-"
SERVICE_SUFFIX = ".service"
TIMER_SUFFIX = ".timer"
BACKSTOP_SECONDS = 300
SYSTEMCTL = "systemctl"


def _quote(value: str) -> str:
    if value and not any(char.isspace() or char in '"\\' for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def service_name(task: str) -> str:
    return f"{UNIT_PREFIX}{task}{SERVICE_SUFFIX}"


def timer_name(task: str) -> str:
    return f"{UNIT_PREFIX}{task}{TIMER_SUFFIX}"


class SystemdBackend(Backend):
    """Compiles task specs into systemd user units."""

    name = "systemd"

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

    # -- rendering ---------------------------------------------------------

    def is_managed(self, filename: str) -> bool:
        return filename.startswith(UNIT_PREFIX) and filename.endswith(
            (SERVICE_SUFFIX, TIMER_SUFFIX)
        )

    def task_filenames(self, task: TaskSpec) -> tuple[str, ...]:
        if task.schedule is None:
            return (service_name(task.name),)
        return (service_name(task.name), timer_name(task.name))

    def render_service(self, automations: Automations, task: TaskSpec) -> str:
        exec_start = " ".join(_quote(item) for item in self.exec_argv(task))
        lines = [
            f"# {GENERATED_HEADER}",
            "",
            "[Unit]",
            f"Description=automationctl: {task.name}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={exec_start}",
        ]
        timeout = effective_timeout(automations.manifest, task)
        if timeout is not None:
            lines.append(f"RuntimeMaxSec={timeout + BACKSTOP_SECONDS}")
        return "\n".join(lines) + "\n"

    def render_timer(self, automations: Automations, task: TaskSpec) -> str:
        assert task.schedule is not None
        timing = to_systemd(task.schedule)
        lines = [
            f"# {GENERATED_HEADER}",
            "",
            "[Unit]",
            f"Description=automationctl timer: {task.name}",
            "",
            "[Timer]",
            f"Unit={service_name(task.name)}",
        ]
        for entry in timing.on_calendar:
            lines.append(f"OnCalendar={entry}")
        if timing.on_boot_sec is not None:
            lines.append(f"OnBootSec={timing.on_boot_sec}")
        if timing.on_unit_active_sec is not None:
            lines.append(f"OnUnitActiveSec={timing.on_unit_active_sec}")
        if timing.on_calendar and effective_persistent(automations.manifest, task):
            lines.append("Persistent=true")
        delay = effective_randomized_delay(automations.manifest, task)
        if delay > 0:
            lines.append(f"RandomizedDelaySec={delay}")
        lines.extend(["", "[Install]", "WantedBy=timers.target"])
        return "\n".join(lines) + "\n"

    def desired_files(self, automations: Automations, tasks: Sequence[TaskSpec]) -> dict[str, str]:
        files: dict[str, str] = {}
        for task in tasks:
            files[service_name(task.name)] = self.render_service(automations, task)
            if task.schedule is not None:
                files[timer_name(task.name)] = self.render_timer(automations, task)
        return files

    # -- substrate operations ---------------------------------------------

    def _systemctl(self, *args: str) -> CommandResult:
        return self.runner.run([SYSTEMCTL, "--user", *args])

    def reload(self) -> list[CommandResult]:
        return [self._systemctl("daemon-reload")]

    def activate(
        self,
        automations: Automations,
        tasks: Sequence[TaskSpec],
        desired: Mapping[str, str] = MappingProxyType({}),
    ) -> list[CommandResult]:
        """Re-assert every scheduled timer.

        ``desired`` is ignored here, and so is any notion of activation
        memory: ``enable --now`` is idempotent, never interrupts a running
        service, and ``daemon-reload`` has already taught systemd the new
        definitions. Re-asserting the whole selection is both harmless and
        exactly what ``install`` promises.
        """
        results: list[CommandResult] = []
        for task in tasks:
            if task.schedule is None:
                continue
            results.append(self._systemctl("enable", "--now", timer_name(task.name)))
        return results

    def deactivate(self, filenames: Sequence[str]) -> list[CommandResult]:
        results: list[CommandResult] = []
        for filename in filenames:
            if filename.endswith(TIMER_SUFFIX):
                results.append(self._systemctl("disable", "--now", filename))
            else:
                results.append(self._systemctl("stop", filename))
        return results

    def submit(self, task: TaskSpec) -> list[CommandResult]:
        return [self._systemctl("start", service_name(task.name))]

    def pause(self, task: TaskSpec) -> list[CommandResult]:
        if task.schedule is None:
            return []
        return [self._systemctl("disable", "--now", timer_name(task.name))]

    def resume(self, task: TaskSpec) -> list[CommandResult]:
        if task.schedule is None:
            return []
        return [self._systemctl("enable", "--now", timer_name(task.name))]

    def enabled(self, task: TaskSpec) -> bool | None:
        if task.schedule is None:
            return None
        result = self._systemctl("is-enabled", timer_name(task.name))
        text = result.stdout.strip()
        if not text:
            return None
        return text == "enabled"

    def follow_argv(self, task: TaskSpec) -> tuple[str, ...] | None:
        return ("journalctl", "--user", "-u", service_name(task.name), "-f")

    def health(self) -> list[HealthCheck]:
        checks: list[HealthCheck] = []
        state = self._systemctl("is-system-running")
        text = state.stdout.strip() or state.stderr.strip() or "unknown"
        checks.append(
            HealthCheck("backend", state.returncode in {0, 1}, f"systemd user manager: {text}")
        )
        failed = self._systemctl("list-units", "--state=failed", "--no-legend", f"{UNIT_PREFIX}*")
        count = len([line for line in failed.stdout.splitlines() if line.strip()])
        checks.append(HealthCheck("failed units", count == 0, f"{count} failed managed unit(s)"))
        linger = self.runner.run(
            ["loginctl", "show-user", str(self.uid), "--property=Linger", "--value"]
        )
        value = linger.stdout.strip()
        checks.append(
            HealthCheck(
                "linger",
                value == "yes",
                "enabled" if value == "yes" else f"not enabled ({value or 'unknown'})",
            )
        )
        return checks
