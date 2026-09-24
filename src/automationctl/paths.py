"""XDG locations used by standalone tasks."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

UNIT_DIR_ENV = "AUTOMATIONCTL_UNIT_DIR"
BACKEND_ENV = "AUTOMATIONCTL_BACKEND"
EXECUTABLE_ENV = "AUTOMATIONCTL_EXECUTABLE"
Env = Mapping[str, str]


def _env(env: Env | None) -> Env:
    return os.environ if env is None else env


def expand(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(str(path)))


def config_home(env: Env | None = None) -> Path:
    values = _env(env)
    return expand(values.get("XDG_CONFIG_HOME", "~/.config"))


def config_dir(env: Env | None = None) -> Path:
    return config_home(env) / "automationctl"


def tasks_dir(env: Env | None = None) -> Path:
    return config_dir(env) / "tasks"


def policy_path(env: Env | None = None) -> Path:
    return config_dir(env) / "config.toml"


def state_dir(env: Env | None = None) -> Path:
    values = _env(env)
    return expand(values.get("XDG_STATE_HOME", "~/.local/state")) / "automationctl"


def default_unit_dir(backend: str, env: Env | None = None) -> Path:
    values = _env(env)
    if values.get(UNIT_DIR_ENV):
        return expand(values[UNIT_DIR_ENV])
    if backend == "systemd":
        return config_home(values) / "systemd" / "user"
    if backend == "launchd":
        return expand("~/Library/LaunchAgents")
    raise ValueError(f"unknown backend: {backend}")


def executable(env: Env | None = None) -> str:
    values = _env(env)
    if values.get(EXECUTABLE_ENV):
        return values[EXECUTABLE_ENV]
    found = shutil.which("automationctl")
    if found:
        return found
    candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    return (
        str(candidate)
        if candidate and candidate.name in {"automationctl", "actl"}
        else "automationctl"
    )
