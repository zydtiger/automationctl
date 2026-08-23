"""Neutral schedule grammar and its lowering to each backend.

The grammar is deliberately limited to what systemd timers and launchd agents
both express natively::

    daily HH:MM
    weekly <dow> HH:MM
    monthly <day> HH:MM
    every <N>m | every <N>h

Anything more exotic uses the per-backend escape hatch table, which is passed
through verbatim and is only valid for the backends it names.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from .errors import ScheduleError

ScheduleKind = Literal["daily", "weekly", "monthly", "interval", "raw"]

WEEKDAY_NAMES: dict[str, int] = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}
SYSTEMD_WEEKDAY = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

_DURATION_RE = re.compile(r"(\d+)\s*([smhd])")
_TIME_RE = re.compile(r"^([0-2]?\d):([0-5]\d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Parse ``30m``, ``4h``, ``1h30m`` and friends into whole seconds."""
    compact = text.strip().lower().replace(" ", "")
    if not re.fullmatch(r"(\d+[smhd])+", compact):
        raise ScheduleError(f"invalid duration: {text!r}")
    return sum(int(amount) * _UNIT_SECONDS[unit] for amount, unit in _DURATION_RE.findall(compact))


def format_duration(seconds: int) -> str:
    """Render whole seconds as a compact human duration such as ``2h 10m``."""
    if seconds < 60:
        return f"{seconds}s"
    parts: list[str] = []
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts)


@dataclass(frozen=True)
class Schedule:
    """A parsed schedule in neutral form."""

    text: str
    kind: ScheduleKind
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
    day: int | None = None
    interval_seconds: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_calendar(self) -> bool:
        return self.kind in {"daily", "weekly", "monthly"}


def _parse_time(text: str) -> tuple[int, int]:
    match = _TIME_RE.match(text)
    if match is None:
        raise ScheduleError(f"invalid time-of-day: {text!r} (expected HH:MM)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23:
        raise ScheduleError(f"invalid time-of-day: {text!r} (hour out of range)")
    return hour, minute


def parse(value: str | Mapping[str, Any]) -> Schedule:
    """Parse a schedule value from a task spec."""
    if isinstance(value, Mapping):
        return _parse_raw(value)
    return _parse_text(value)


def _parse_raw(value: Mapping[str, Any]) -> Schedule:
    known = {"systemd", "launchd"}
    unknown = sorted(set(value) - known)
    if unknown:
        raise ScheduleError(f"unknown schedule backend key(s): {', '.join(unknown)}")
    if not value:
        raise ScheduleError("schedule table must name at least one backend")
    return Schedule(text="custom", kind="raw", raw=dict(value))


def _parse_text(value: str) -> Schedule:
    tokens = value.split()
    if not tokens:
        raise ScheduleError("empty schedule")
    head = tokens[0].lower()
    if head == "daily":
        if len(tokens) != 2:
            raise ScheduleError(f"invalid schedule: {value!r} (expected 'daily HH:MM')")
        hour, minute = _parse_time(tokens[1])
        return Schedule(
            text=f"daily {hour:02d}:{minute:02d}", kind="daily", hour=hour, minute=minute
        )
    if head == "weekly":
        if len(tokens) != 3:
            raise ScheduleError(f"invalid schedule: {value!r} (expected 'weekly <dow> HH:MM')")
        name = tokens[1].lower()
        if name not in WEEKDAY_NAMES:
            raise ScheduleError(f"unknown weekday: {tokens[1]!r}")
        weekday = WEEKDAY_NAMES[name]
        hour, minute = _parse_time(tokens[2])
        return Schedule(
            text=f"weekly {SYSTEMD_WEEKDAY[weekday].lower()} {hour:02d}:{minute:02d}",
            kind="weekly",
            weekday=weekday,
            hour=hour,
            minute=minute,
        )
    if head == "monthly":
        if len(tokens) != 3:
            raise ScheduleError(f"invalid schedule: {value!r} (expected 'monthly <day> HH:MM')")
        if not tokens[1].isdigit():
            raise ScheduleError(f"invalid day of month: {tokens[1]!r}")
        day = int(tokens[1])
        if not 1 <= day <= 31:
            raise ScheduleError(f"day of month out of range: {day}")
        hour, minute = _parse_time(tokens[2])
        return Schedule(
            text=f"monthly {day} {hour:02d}:{minute:02d}",
            kind="monthly",
            day=day,
            hour=hour,
            minute=minute,
        )
    if head == "every":
        if len(tokens) != 2:
            raise ScheduleError(
                f"invalid schedule: {value!r} (expected 'every <N>m' or 'every <N>h')"
            )
        spec = tokens[1].lower()
        match = re.fullmatch(r"(\d+)([mh])", spec)
        if match is None:
            raise ScheduleError(f"invalid interval: {tokens[1]!r} (expected <N>m or <N>h)")
        amount = int(match.group(1))
        if amount <= 0:
            raise ScheduleError(f"invalid interval: {tokens[1]!r}")
        seconds = amount * _UNIT_SECONDS[match.group(2)]
        return Schedule(
            text=f"every {amount}{match.group(2)}", kind="interval", interval_seconds=seconds
        )
    raise ScheduleError(
        f"unknown schedule form: {value!r} "
        "(expected daily/weekly/monthly/every, or a per-backend table)"
    )


@dataclass(frozen=True)
class SystemdTiming:
    on_calendar: tuple[str, ...] = ()
    on_unit_active_sec: str | None = None
    on_boot_sec: str | None = None


@dataclass(frozen=True)
class LaunchdTiming:
    start_calendar_interval: tuple[dict[str, int], ...] = ()
    start_interval: int | None = None


def to_systemd(schedule: Schedule) -> SystemdTiming:
    """Lower a schedule to systemd timer directives."""
    if schedule.kind == "daily":
        assert schedule.hour is not None and schedule.minute is not None
        return SystemdTiming(on_calendar=(f"*-*-* {schedule.hour:02d}:{schedule.minute:02d}:00",))
    if schedule.kind == "weekly":
        assert schedule.weekday is not None
        assert schedule.hour is not None and schedule.minute is not None
        day = SYSTEMD_WEEKDAY[schedule.weekday]
        return SystemdTiming(
            on_calendar=(f"{day} *-*-* {schedule.hour:02d}:{schedule.minute:02d}:00",)
        )
    if schedule.kind == "monthly":
        assert schedule.day is not None
        assert schedule.hour is not None and schedule.minute is not None
        return SystemdTiming(
            on_calendar=(f"*-*-{schedule.day:02d} {schedule.hour:02d}:{schedule.minute:02d}:00",)
        )
    if schedule.kind == "interval":
        assert schedule.interval_seconds is not None
        value = f"{schedule.interval_seconds}s"
        return SystemdTiming(on_unit_active_sec=value, on_boot_sec=value)
    raw = schedule.raw.get("systemd")
    if raw is None:
        raise ScheduleError("schedule table has no 'systemd' entry")
    if isinstance(raw, str):
        return SystemdTiming(on_calendar=(raw,))
    if isinstance(raw, Sequence):
        entries = tuple(str(item) for item in raw)
        if not entries:
            raise ScheduleError("empty 'systemd' schedule table entry")
        return SystemdTiming(on_calendar=entries)
    raise ScheduleError("'systemd' schedule entry must be a string or list of strings")


def to_launchd(schedule: Schedule) -> LaunchdTiming:
    """Lower a schedule to launchd start conditions."""
    if schedule.kind == "daily":
        assert schedule.hour is not None and schedule.minute is not None
        return LaunchdTiming(
            start_calendar_interval=({"Hour": schedule.hour, "Minute": schedule.minute},)
        )
    if schedule.kind == "weekly":
        assert schedule.weekday is not None
        assert schedule.hour is not None and schedule.minute is not None
        return LaunchdTiming(
            start_calendar_interval=(
                {"Weekday": schedule.weekday, "Hour": schedule.hour, "Minute": schedule.minute},
            )
        )
    if schedule.kind == "monthly":
        assert schedule.day is not None
        assert schedule.hour is not None and schedule.minute is not None
        return LaunchdTiming(
            start_calendar_interval=(
                {"Day": schedule.day, "Hour": schedule.hour, "Minute": schedule.minute},
            )
        )
    if schedule.kind == "interval":
        assert schedule.interval_seconds is not None
        return LaunchdTiming(start_interval=schedule.interval_seconds)
    raw = schedule.raw.get("launchd")
    if raw is None:
        raise ScheduleError("schedule table has no 'launchd' entry")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return LaunchdTiming(start_interval=raw)
    if isinstance(raw, Mapping):
        return LaunchdTiming(start_calendar_interval=({str(k): int(v) for k, v in raw.items()},))
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        entries: list[dict[str, int]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ScheduleError("'launchd' schedule list entries must be tables")
            entries.append({str(k): int(v) for k, v in item.items()})
        if not entries:
            raise ScheduleError("empty 'launchd' schedule table entry")
        return LaunchdTiming(start_calendar_interval=tuple(entries))
    raise ScheduleError("'launchd' schedule entry must be an integer, table, or list of tables")


def _weekday_index(moment: datetime) -> int:
    """Return Sunday-based weekday index, matching launchd's convention."""
    return (moment.weekday() + 1) % 7


def previous_occurrence(schedule: Schedule, now: datetime) -> datetime | None:
    """Return the most recent scheduled moment at or before ``now``.

    Interval and escape-hatch schedules return ``None``: the former is handled
    by elapsed-time comparison, the latter is opaque to the neutral grammar.
    """
    if not schedule.is_calendar:
        return None
    assert schedule.hour is not None and schedule.minute is not None
    candidate = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
    if schedule.kind == "daily":
        if candidate > now:
            candidate -= timedelta(days=1)
        return candidate
    if schedule.kind == "weekly":
        assert schedule.weekday is not None
        for back in range(0, 8):
            probe = candidate - timedelta(days=back)
            if _weekday_index(probe) == schedule.weekday and probe <= now:
                return probe
        return None
    assert schedule.day is not None
    probe = candidate
    for _ in range(0, 14):
        try:
            dated = probe.replace(day=schedule.day)
        except ValueError:
            dated = None
        if dated is not None and dated <= now:
            return dated
        first = probe.replace(day=1)
        probe = (first - timedelta(days=1)).replace(
            hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
        )
    return None
