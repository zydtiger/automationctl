"""Exception types shared by every automationctl module."""

from __future__ import annotations

from pathlib import Path


class AutomationctlError(Exception):
    """Base class for every error the tool reports to the user."""


class ConfigError(AutomationctlError):
    """A manifest, runner table, or task spec could not be loaded."""

    def __init__(self, message: str, path: Path | None = None) -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path is not None else message)
        self.message = message


class ScheduleError(AutomationctlError):
    """A schedule expression is not valid neutral grammar."""


class TemplateError(AutomationctlError):
    """A placeholder could not be expanded."""


class BackendError(AutomationctlError):
    """A scheduler backend refused an operation."""


class LockBusy(AutomationctlError):
    """A named lock is held by another run."""


class LintFailed(AutomationctlError):
    """Lint found errors; the caller should stop."""
