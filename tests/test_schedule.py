"""The neutral schedule grammar and its lowering to each backend."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import local_moment, local_tz

from automationctl.errors import ScheduleError
from automationctl.schedule import (
    format_duration,
    parse,
    parse_duration,
    previous_occurrence,
    to_launchd,
    to_systemd,
)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("4h", 14400), ("1d", 86400), ("1h30m", 5400)],
)
def test_parse_duration_accepts_compound_units(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "5", "5x", "-3m", "m5"])
def test_parse_duration_rejects_nonsense(text: str) -> None:
    with pytest.raises(ScheduleError):
        parse_duration(text)


def test_format_duration_is_compact() -> None:
    assert format_duration(4) == "4s"
    assert format_duration(90) == "1m 30s"
    assert format_duration(7800) == "2h 10m"


@pytest.mark.parametrize(
    ("text", "kind", "canonical"),
    [
        ("daily 03:00", "daily", "daily 03:00"),
        ("daily 3:05", "daily", "daily 03:05"),
        ("weekly sun 05:00", "weekly", "weekly sun 05:00"),
        ("weekly Monday 09:30", "weekly", "weekly mon 09:30"),
        ("monthly 1 09:00", "monthly", "monthly 1 09:00"),
        ("every 15m", "interval", "every 15m"),
        ("every 6h", "interval", "every 6h"),
    ],
)
def test_grammar_table_parses(text: str, kind: str, canonical: str) -> None:
    schedule = parse(text)
    assert schedule.kind == kind
    assert schedule.text == canonical


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hourly",
        "daily",
        "daily 25:00",
        "daily 3pm",
        "weekly funday 05:00",
        "weekly sun",
        "monthly 0 09:00",
        "monthly 32 09:00",
        "every",
        "every 15",
        "every 0m",
        "every 15d",
    ],
)
def test_grammar_rejects_invalid_forms(text: str) -> None:
    with pytest.raises(ScheduleError):
        parse(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("daily 03:00", "*-*-* 03:00:00"),
        ("weekly sun 05:00", "Sun *-*-* 05:00:00"),
        ("monthly 1 09:00", "*-*-01 09:00:00"),
    ],
)
def test_systemd_calendar_lowering(text: str, expected: str) -> None:
    assert to_systemd(parse(text)).on_calendar == (expected,)


def test_systemd_interval_lowering_sets_boot_and_active() -> None:
    timing = to_systemd(parse("every 15m"))
    assert timing.on_unit_active_sec == "15m"
    assert timing.on_boot_sec == "15m"
    assert timing.on_calendar == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("daily 03:00", ({"Hour": 3, "Minute": 0},)),
        ("weekly sun 05:00", ({"Weekday": 0, "Hour": 5, "Minute": 0},)),
        ("monthly 1 09:00", ({"Day": 1, "Hour": 9, "Minute": 0},)),
    ],
)
def test_launchd_calendar_lowering(text: str, expected: tuple[dict[str, int], ...]) -> None:
    assert to_launchd(parse(text)).start_calendar_interval == expected


def test_launchd_interval_lowering() -> None:
    assert to_launchd(parse("every 15m")).start_interval == 900


def test_escape_hatch_is_backend_scoped() -> None:
    schedule = parse({"systemd": "Mon..Fri *-*-* 09..17:00:00"})
    assert schedule.kind == "raw"
    assert to_systemd(schedule).on_calendar == ("Mon..Fri *-*-* 09..17:00:00",)
    with pytest.raises(ScheduleError):
        to_launchd(schedule)


def test_escape_hatch_launchd_list_of_tables() -> None:
    schedule = parse({"launchd": [{"Weekday": 1, "Hour": 9, "Minute": 0}]})
    assert to_launchd(schedule).start_calendar_interval == ({"Weekday": 1, "Hour": 9, "Minute": 0},)
    with pytest.raises(ScheduleError):
        to_systemd(schedule)


def test_escape_hatch_rejects_unknown_backend_key() -> None:
    with pytest.raises(ScheduleError):
        parse({"cron": "0 9 * * *"})


def moment(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


@pytest.mark.parametrize("tz", ["UTC", "Asia/Shanghai", "America/Los_Angeles"])
@pytest.mark.parametrize(
    ("schedule_text", "now", "expected"),
    [
        ("daily 03:00", "2026-08-23 04:00", "2026-08-23 03:00"),
        ("daily 03:00", "2026-08-23 02:00", "2026-08-22 03:00"),
        # 2026-08-23 is a Sunday.
        ("weekly sun 05:00", "2026-08-23 06:00", "2026-08-23 05:00"),
        ("weekly sun 05:00", "2026-08-23 04:00", "2026-08-16 05:00"),
        ("monthly 1 09:00", "2026-08-23 10:00", "2026-08-01 09:00"),
        ("monthly 31 09:00", "2026-03-05 10:00", "2026-01-31 09:00"),
    ],
)
def test_previous_occurrence_follows_local_wall_clock(
    tz: str, schedule_text: str, now: str, expected: str
) -> None:
    with local_tz(tz):
        assert previous_occurrence(parse(schedule_text), local_moment(now)) == local_moment(
            expected
        )


@pytest.mark.parametrize(
    ("tz", "now_utc", "expected_local", "expected_utc"),
    [
        # UTC+8: 22:00Z is already 06:00 the next local day, so the 03:00 local
        # occurrence has passed and sits at 19:00Z of the previous day.
        ("Asia/Shanghai", "2026-08-22 22:00", "2026-08-23 03:00", "2026-08-22 19:00"),
        # UTC-7: 05:00Z is still the previous local evening, so the most recent
        # 03:00 local occurrence is that same local morning, at 10:00Z.
        ("America/Los_Angeles", "2026-08-23 05:00", "2026-08-22 03:00", "2026-08-22 10:00"),
    ],
)
def test_a_utc_instant_resolves_against_the_local_calendar(
    tz: str, now_utc: str, expected_local: str, expected_utc: str
) -> None:
    """Both backends fire on local time, so a UTC 'now' must not shift the day."""
    with local_tz(tz):
        result = previous_occurrence(parse("daily 03:00"), moment(now_utc))
        assert result is not None
        assert result == moment(expected_utc)
        assert result.strftime("%Y-%m-%d %H:%M") == expected_local


@pytest.mark.parametrize(
    ("schedule_text", "now_utc", "expected_utc", "note"),
    [
        # Europe/Berlin springs forward 2026-03-29 (CET +01:00 → CEST +02:00).
        # Walking back from a CEST "now" to a CET Saturday must not carry the
        # +02:00 offset along: 03:00 on 2026-03-28 is 02:00Z, not 01:00Z.
        ("weekly sat 03:00", "2026-03-31 10:00", "2026-03-28 02:00", "weekly, spring forward"),
        ("monthly 15 03:00", "2026-04-10 10:00", "2026-03-15 02:00", "monthly, spring forward"),
        # Europe/Berlin falls back 2026-10-25 (CEST +02:00 → CET +01:00), so
        # 03:00 on 2026-10-24 is 01:00Z, not 02:00Z.
        ("weekly sat 03:00", "2026-10-27 11:00", "2026-10-24 01:00", "weekly, fall back"),
        ("monthly 20 03:00", "2026-11-05 11:00", "2026-10-20 01:00", "monthly, fall back"),
    ],
)
def test_occurrences_resolve_the_offset_of_their_own_date(
    schedule_text: str, now_utc: str, expected_utc: str, note: str
) -> None:
    """Walking back must not carry today's UTC offset across a DST boundary."""
    with local_tz("Europe/Berlin"):
        result = previous_occurrence(parse(schedule_text), moment(now_utc))
        assert result is not None
        assert result == moment(expected_utc), note
        # Whatever the offset turns out to be, the wall clock is still 03:00.
        assert result.strftime("%H:%M") == "03:00"


def test_daily_occurrence_on_a_transition_day_keeps_the_wall_clock() -> None:
    with local_tz("Europe/Berlin"):
        # 2026-03-29 08:00 CEST is 06:00Z; that day's 03:00 local occurrence
        # is already CEST, at 01:00Z.
        result = previous_occurrence(parse("daily 03:00"), moment("2026-03-29 06:00"))
        assert result is not None
        assert result == moment("2026-03-29 01:00")
        assert result.strftime("%H:%M") == "03:00"


def test_previous_occurrence_is_undefined_for_intervals() -> None:
    assert previous_occurrence(parse("every 15m"), moment("2026-08-23 04:00")) is None
