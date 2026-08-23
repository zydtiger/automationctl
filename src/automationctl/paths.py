"""Filesystem locations owned by automationctl.

The state directory is uniform across platforms (``$XDG_STATE_HOME`` else
``~/.local/state``, subpath ``automationctl/``). Every location is overridable
through an environment variable so that tests and dry runs never touch the
machine's real scheduler or state.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

MANIFEST_ENV = "AUTOMATIONCTL_MANIFEST"
STATE_DIR_ENV = "AUTOMATIONCTL_STATE_DIR"
UNIT_DIR_ENV = "AUTOMATIONCTL_UNIT_DIR"
BACKEND_ENV = "AUTOMATIONCTL_BACKEND"
EXECUTABLE_ENV = "AUTOMATIONCTL_EXECUTABLE"

DEFAULT_MANIFEST = "~/automations/manifest.toml"

Env = Mapping[str, str]


def _env(env: Env | None) -> Env:
    return os.environ if env is None else env


def expand(path: str | os.PathLike[str]) -> Path:
    """Expand ``~`` and return an absolute-friendly :class:`Path`."""
    return Path(os.path.expanduser(str(path)))


def state_dir(env: Env | None = None) -> Path:
    """Return the state directory holding runs, last-run pointers, and locks."""
    values = _env(env)
    override = values.get(STATE_DIR_ENV)
    if override:
        return expand(override)
    xdg = values.get("XDG_STATE_HOME")
    base = expand(xdg) if xdg else expand("~/.local/state")
    return base / "automationctl"


def runs_dir(env: Env | None = None) -> Path:
    return state_dir(env) / "runs"


def last_dir(env: Env | None = None) -> Path:
    return state_dir(env) / "last"


def locks_dir(env: Env | None = None) -> Path:
    return state_dir(env) / "locks"


def default_manifest_path(env: Env | None = None) -> Path:
    """Return the manifest path from the environment, else the documented default."""
    values = _env(env)
    override = values.get(MANIFEST_ENV)
    return expand(override) if override else expand(DEFAULT_MANIFEST)


def default_unit_dir(backend: str, env: Env | None = None) -> Path:
    """Return the directory a backend writes generated units into."""
    values = _env(env)
    override = values.get(UNIT_DIR_ENV)
    if override:
        return expand(override)
    if backend == "systemd":
        xdg = values.get("XDG_CONFIG_HOME")
        base = expand(xdg) if xdg else expand("~/.config")
        return base / "systemd" / "user"
    if backend == "launchd":
        return expand("~/Library/LaunchAgents")
    raise ValueError(f"unknown backend: {backend}")


def executable(env: Env | None = None) -> str:
    """Return the ``automationctl`` command generated units should invoke."""
    values = _env(env)
    override = values.get(EXECUTABLE_ENV)
    if override:
        return override
    found = shutil.which("automationctl")
    if found:
        return found
    candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if candidate is not None and candidate.name in {"automationctl", "actl"}:
        return str(candidate)
    return "automationctl"
