"""Run records, last-run pointers, and retention."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from automationctl import records


def make_run(state: Path, task: str, run_id: str, status: str = "ok") -> Path:
    run_dir = records.create_run_dir(state, task, run_id)
    records.write_meta(
        run_dir,
        {
            "task": task,
            "run_id": run_id,
            "status": status,
            "exit_code": 0 if status == "ok" else 1,
            "started_at": "2026-08-23T03:00:00Z",
            "finished_at": "2026-08-23T03:00:11Z",
            "duration_seconds": 11.0,
        },
    )
    return run_dir


def test_run_id_is_sortable_and_unique() -> None:
    now = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)
    first = records.new_run_id(now)
    second = records.new_run_id(now)
    assert first.startswith("20260823T030000Z-")
    assert first != second


def test_recent_runs_are_newest_first(tmp_path: Path) -> None:
    for run_id in ("20260821T030000Z-aaaaaa", "20260822T030000Z-bbbbbb"):
        make_run(tmp_path, "audit", run_id)
    runs = records.recent_runs(tmp_path, "audit")
    assert [item.run_id for item in runs] == [
        "20260822T030000Z-bbbbbb",
        "20260821T030000Z-aaaaaa",
    ]
    assert records.latest_run(tmp_path, "audit") is not None


def test_last_run_pointer_round_trips(tmp_path: Path) -> None:
    records.write_last(tmp_path, "audit", {"task": "audit", "status": "ok"})
    assert records.read_last(tmp_path, "audit") == {"task": "audit", "status": "ok"}
    assert records.read_last(tmp_path, "missing") is None


def test_prune_keeps_the_newest_runs(tmp_path: Path) -> None:
    ids = [f"2026082{index}T030000Z-aaaaaa" for index in range(1, 6)]
    for run_id in ids:
        make_run(tmp_path, "audit", run_id)
    removed = records.prune(tmp_path, keep_runs=2)
    assert len(removed) == 3
    remaining = [path.name for path in records.run_dirs(tmp_path, "audit")]
    assert remaining == ids[-2:]


def test_prune_walks_every_recorded_task(tmp_path: Path) -> None:
    make_run(tmp_path, "a", "20260821T030000Z-aaaaaa")
    make_run(tmp_path, "b", "20260821T030000Z-bbbbbb")
    assert records.known_tasks(tmp_path) == ["a", "b"]
    assert len(records.prune(tmp_path, keep_runs=0)) == 2


def test_temp_names_are_unique_per_call(tmp_path: Path) -> None:
    """Two runs of one task can rewrite last/<task>.json at the same moment."""
    target = tmp_path / "last" / "audit.json"
    names = {records.temp_name(target).name for _ in range(50)}
    assert len(names) == 50
    assert all(name.startswith("audit.json.") and name.endswith(".tmp") for name in names)


def test_atomic_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "value.json"
    records.write_json(path, {"a": 1})
    assert [entry.name for entry in path.parent.iterdir()] == ["value.json"]


def test_timestamps_round_trip() -> None:
    moment = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)
    assert records.parse_isoformat(records.isoformat(moment)) == moment
