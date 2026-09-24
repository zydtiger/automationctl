"""Hermetic fixtures for installed standalone task tests."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from automationctl import backends
from automationctl.commands import RecordingRunner

HOST = "testhost"


@contextmanager
def local_tz(name: str) -> Iterator[None]:
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def local_moment(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    config, state, units, home = (
        tmp_path / "config home",
        tmp_path / "state home",
        tmp_path / "units",
        tmp_path / "home",
    )
    home.mkdir()
    env = {
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(state),
        "AUTOMATIONCTL_BACKEND": "systemd",
        "AUTOMATIONCTL_UNIT_DIR": str(units),
        "HOME": str(home),
    }
    recorder = RecordingRunner()
    original = backends.create

    def recorded(name: str, **kwargs: object) -> backends.Backend:
        kwargs["runner"] = recorder
        return original(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends, "create", recorded)
    yield env


def write_task(
    path: Path, name: str, *, command: str = '["/bin/echo", "ok"]', extra: str = ""
) -> Path:
    path.write_text(
        "\n".join(
            [
                "schema_version = 2",
                f'name = "{name}"',
                'description = "test task"',
                f"command = {command}",
                extra,
            ]
        ),
        encoding="utf-8",
    )
    return path
