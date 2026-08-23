"""Run records, last-run pointers, and retention.

Layout under the state directory::

    runs/<task>/<UTC-timestamp>-<shortid>/{meta.json,stdout.log,stderr.log,result.json}
    last/<task>.json

Records are plain JSON so that any tool — or a human with ``jq`` — can read
them without this package.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

FAILURE_STATUSES = frozenset({STATUS_FAILED, STATUS_TIMEOUT, STATUS_ERROR})

META_FILE = "meta.json"
STDOUT_FILE = "stdout.log"
STDERR_FILE = "stderr.log"
RESULT_FILE = "result.json"

_RUN_ID_TIME = "%Y%m%dT%H%M%SZ"


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_isoformat(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def new_run_id(now: datetime | None = None) -> str:
    """Return a sortable run identifier: UTC timestamp plus a short random suffix."""
    moment = now if now is not None else utcnow()
    return f"{moment.astimezone(UTC).strftime(_RUN_ID_TIME)}-{secrets.token_hex(3)}"


def task_runs_dir(state_dir: Path, task: str) -> Path:
    return state_dir / "runs" / task


def create_run_dir(state_dir: Path, task: str, run_id: str) -> Path:
    """Create and return the directory for one run."""
    run_dir = task_runs_dir(state_dir, task) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def last_path(state_dir: Path, task: str) -> Path:
    return state_dir / "last" / f"{task}.json"


def temp_name(path: Path) -> Path:
    """Return a per-process, per-call temporary sibling of ``path``."""
    return path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")


def write_json(path: Path, data: Mapping[str, Any]) -> Path:
    """Write JSON atomically.

    The temporary name carries a pid and a random suffix. Two runs of the same
    task can finish at the same moment — a timer firing while a manual run is
    still going — and both rewrite ``last/<task>.json``; a shared temporary
    name would let one truncate the other's file mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_name(path)
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_meta(run_dir: Path, data: Mapping[str, Any]) -> Path:
    return write_json(run_dir / META_FILE, data)


def read_meta(run_dir: Path) -> dict[str, Any] | None:
    return read_json(run_dir / META_FILE)


def write_last(state_dir: Path, task: str, data: Mapping[str, Any]) -> Path:
    return write_json(last_path(state_dir, task), data)


def read_last(state_dir: Path, task: str) -> dict[str, Any] | None:
    return read_json(last_path(state_dir, task))


@dataclass(frozen=True)
class RunSummary:
    """A compact view of one recorded run."""

    task: str
    run_id: str
    run_dir: Path
    status: str
    exit_code: int | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None

    @property
    def started(self) -> datetime | None:
        if self.started_at is None:
            return None
        try:
            return parse_isoformat(self.started_at)
        except ValueError:
            return None


def summarize(task: str, run_dir: Path, meta: Mapping[str, Any]) -> RunSummary:
    exit_code = meta.get("exit_code")
    duration = meta.get("duration_seconds")
    return RunSummary(
        task=task,
        run_id=str(meta.get("run_id", run_dir.name)),
        run_dir=run_dir,
        status=str(meta.get("status", "unknown")),
        exit_code=exit_code if isinstance(exit_code, int) else None,
        started_at=meta.get("started_at") if isinstance(meta.get("started_at"), str) else None,
        finished_at=meta.get("finished_at") if isinstance(meta.get("finished_at"), str) else None,
        duration_seconds=float(duration) if isinstance(duration, int | float) else None,
    )


def run_dirs(state_dir: Path, task: str) -> list[Path]:
    """Return the run directories of a task, oldest first."""
    base = task_runs_dir(state_dir, task)
    if not base.is_dir():
        return []
    return sorted((entry for entry in base.iterdir() if entry.is_dir()), key=lambda p: p.name)


def recent_runs(state_dir: Path, task: str, limit: int | None = None) -> list[RunSummary]:
    """Return recorded runs of a task, newest first."""
    summaries: list[RunSummary] = []
    for run_dir in reversed(run_dirs(state_dir, task)):
        meta = read_meta(run_dir)
        if meta is None:
            continue
        summaries.append(summarize(task, run_dir, meta))
        if limit is not None and len(summaries) >= limit:
            break
    return summaries


def latest_run(state_dir: Path, task: str) -> RunSummary | None:
    runs = recent_runs(state_dir, task, limit=1)
    return runs[0] if runs else None


def known_tasks(state_dir: Path) -> list[str]:
    """Return every task name that has recorded runs."""
    base = state_dir / "runs"
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir() if entry.is_dir())


def prune(state_dir: Path, keep_runs: int, tasks: list[str] | None = None) -> list[Path]:
    """Delete all but the newest ``keep_runs`` run directories per task."""
    if keep_runs < 0:
        raise ValueError("keep_runs must not be negative")
    removed: list[Path] = []
    for task in tasks if tasks is not None else known_tasks(state_dir):
        directories = run_dirs(state_dir, task)
        surplus = directories[: max(0, len(directories) - keep_runs)]
        for run_dir in surplus:
            shutil.rmtree(run_dir, ignore_errors=True)
            removed.append(run_dir)
    return removed
