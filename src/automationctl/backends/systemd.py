"""The systemd user-manager backend.

Generated units are dumb: a ``oneshot`` service that starts
``automationctl exec`` and, for scheduled tasks, a timer that starts the
service. Reconciliation is by filename prefix, so hand-written units are never
touched and stale generated units are garbage-collected.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from .. import catchup
from ..commands import CommandResult, CommandRunner
from ..config import Automations
from ..errors import BackendError
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

CATCHUP_NAME = "catchup"
CATCHUP_SERVICE = f"{UNIT_PREFIX}{CATCHUP_NAME}{SERVICE_SUFFIX}"
CATCHUP_TIMER = f"{UNIT_PREFIX}{CATCHUP_NAME}{TIMER_SUFFIX}"

#: Boot backstop for the catch-up timer. The event triggers cover a running
#: system; this covers the occurrences missed while it was off, and waits long
#: enough that a task starting at boot finds the session it expects.
CATCHUP_BOOT_SEC = "2m"

#: ``OnClockChange=`` and ``OnTimezoneChange=`` were both added in systemd 242
#: ("Added in version 242." in systemd.timer(5)). An older manager parses the
#: unit, warns about the unknown directives, and runs a timer that fires only
#: on the boot backstop — which is precisely the silent gap doctor exists to
#: surface, so the probe compares against the version that actually has them.
CLOCK_TRIGGER_MIN_VERSION = 242

_VERSION_RE = re.compile(r"systemd\s+(\d+)")


def _quote(value: str) -> str:
    escaped = value.replace("%", "%%").replace("$", "$$")
    if escaped and not any(char.isspace() or char in '"\\' for char in escaped):
        return escaped
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
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

    # -- rendering ---------------------------------------------------------

    def is_managed(self, filename: str) -> bool:
        return filename.startswith(UNIT_PREFIX) and filename.endswith(
            (SERVICE_SUFFIX, TIMER_SUFFIX)
        )

    def task_filenames(self, task: TaskSpec) -> tuple[str, ...]:
        if task.schedule is None:
            return (service_name(task.name),)
        return (service_name(task.name), timer_name(task.name))

    def possible_task_filenames(self, task: TaskSpec) -> tuple[str, ...]:
        return (service_name(task.name), timer_name(task.name))

    def render_service(self, automations: Automations, task: TaskSpec) -> str:
        exec_start = " ".join(_quote(item) for item in self.exec_argv(task, host=automations.host))
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
            # systemd ignores RuntimeMaxSec= on Type=oneshot; TimeoutStartSec=,
            # which defaults to infinity for oneshot, is what bounds ExecStart.
            lines.append(f"TimeoutStartSec={timeout + BACKSTOP_SECONDS}")
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

    def render_catchup_service(self, automations: Automations) -> str:
        # No TimeoutStartSec=. Catch-up runs every missed task serially in one
        # foreground sweep, so its runtime is the sum of runs each of which the
        # wrapper already bounds by its own timeout; any single number here
        # would be an invented aggregate whose failure mode is systemd killing
        # the sweep mid-task and losing the very occurrence it was recovering.
        # Type=oneshot already defaults TimeoutStartSec= to infinity.
        exec_start = " ".join(_quote(item) for item in self.catchup_argv(host=automations.host))
        return (
            "\n".join(
                [
                    f"# {GENERATED_HEADER}",
                    "",
                    "[Unit]",
                    "Description=automationctl: catch up missed occurrences",
                    "",
                    "[Service]",
                    "Type=oneshot",
                    f"ExecStart={exec_start}",
                ]
            )
            + "\n"
        )

    def render_catchup_timer(self) -> str:
        return (
            "\n".join(
                [
                    f"# {GENERATED_HEADER}",
                    "",
                    "[Unit]",
                    "Description=automationctl timer: catch up missed occurrences",
                    "",
                    "[Timer]",
                    f"Unit={CATCHUP_SERVICE}",
                    f"OnBootSec={CATCHUP_BOOT_SEC}",
                    "OnClockChange=true",
                    "OnTimezoneChange=true",
                    "",
                    "[Install]",
                    "WantedBy=timers.target",
                ]
            )
            + "\n"
        )

    def desired_files(self, automations: Automations, tasks: Sequence[TaskSpec]) -> dict[str, str]:
        files: dict[str, str] = {}
        for task in tasks:
            names = (service_name(task.name), timer_name(task.name))
            if CATCHUP_SERVICE in names or CATCHUP_TIMER in names:
                # lint reserves the name; refuse rather than silently drop the
                # task by overwriting it with the catch-up units below.
                raise BackendError(
                    f"task {task.name!r} collides with the reserved {CATCHUP_NAME} units"
                )
            files[names[0]] = self.render_service(automations, task)
            if task.schedule is not None:
                files[names[1]] = self.render_timer(automations, task)
        if catchup.triggers_wanted(automations, tasks):
            files[CATCHUP_SERVICE] = self.render_catchup_service(automations)
            files[CATCHUP_TIMER] = self.render_catchup_timer()
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
        rewritten: Collection[str] = (),
    ) -> list[CommandResult]:
        """Re-assert every scheduled timer.

        ``desired`` and ``rewritten`` are ignored here, and so is any notion
        of activation memory: ``enable --now`` is idempotent, never interrupts
        a running service, and ``daemon-reload`` has already taught systemd
        the new definitions. Re-asserting the whole selection is both harmless
        and exactly what ``install`` promises.
        """
        results: list[CommandResult] = []
        for task in tasks:
            if task.schedule is None:
                continue
            results.append(self._systemctl("enable", "--now", timer_name(task.name)))
        # Recomputed from the same predicate that rendered it rather than read
        # out of ``desired``: render and activation must agree, and they cannot
        # disagree if they answer one question with one function.
        if catchup.triggers_wanted(automations, tasks):
            results.append(self._systemctl("enable", "--now", CATCHUP_TIMER))
        return results

    def deactivate(self, filenames: Sequence[str]) -> list[CommandResult]:
        results: list[CommandResult] = []
        # Disable every trigger before stopping any workload. Otherwise a
        # timer can fire between ``stop service`` and ``disable timer`` and
        # leave a fresh process running after uninstall or reconcile returns.
        timers = sorted(name for name in filenames if name.endswith(TIMER_SUFFIX))
        services = sorted(name for name in filenames if not name.endswith(TIMER_SUFFIX))
        for filename in timers:
            results.append(self._systemctl("disable", "--now", filename))
        if any(not result.ok for result in results):
            return results
        for filename in services:
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

    def version(self) -> int | None:
        """Return the running manager's major version, or ``None`` if unreadable."""
        result = self._systemctl("--version")
        match = _VERSION_RE.search(result.stdout or result.stderr)
        return int(match.group(1)) if match is not None else None

    def catchup_health(
        self, automations: Automations, tasks: Sequence[TaskSpec]
    ) -> list[HealthCheck]:
        if not catchup.triggers_wanted(automations, tasks):
            return [
                HealthCheck(
                    "catch-up triggers",
                    True,
                    "not needed: no calendar occurrence a clock or timezone jump can lose",
                )
            ]
        # Probe what is on disk, not what the manifest wants: after an upgrade
        # the installed units keep their old triggers until `install` rewrites
        # them, and a desired-state probe would show green for exactly the
        # installs that need the reinstall.
        service_path = self.unit_dir / CATCHUP_SERVICE
        timer_path = self.unit_dir / CATCHUP_TIMER
        ok = True
        detail = f"{CATCHUP_TIMER} installed in {self.unit_dir}"
        if not service_path.is_file() or not timer_path.is_file():
            ok = False
            detail = f"{CATCHUP_TIMER} is missing from {self.unit_dir}; run automationctl install"
        else:
            try:
                current = (
                    service_path.read_text(encoding="utf-8")
                    == self.render_catchup_service(automations)
                    and timer_path.read_text(encoding="utf-8") == self.render_catchup_timer()
                )
            except OSError as exc:
                ok, detail = False, f"{CATCHUP_TIMER} is unreadable: {exc}"
            else:
                if not current:
                    ok = False
                    detail = f"{CATCHUP_TIMER} is stale; run automationctl install"
        checks = [HealthCheck("catch-up triggers", ok, detail)]
        # An unreadable version is reported as a failure for the same reason an
        # unreadable launchd probe means "reload": the check exists to catch a
        # trigger that never fires, and "cannot tell" is not "fine".
        running = self.version()
        supported = running is not None and running >= CLOCK_TRIGGER_MIN_VERSION
        checks.append(
            HealthCheck(
                "clock triggers",
                supported,
                f"systemd {running} supports OnTimezoneChange="
                if supported
                else (
                    f"systemd {running} predates OnTimezoneChange= "
                    f"(needs {CLOCK_TRIGGER_MIN_VERSION}+); only the boot backstop fires"
                    if running is not None
                    else "cannot determine the systemd version"
                ),
            )
        )
        return checks
