"""Standalone task argv and stdin substitution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from automationctl.errors import TemplateError
from automationctl.spec import TaskSpec
from automationctl.template import build_invocation, builtin_values, substitute

VALUES = builtin_values("audit", "workstation", "/runs/1", datetime(2026, 8, 23, 4, 30, tzinfo=UTC))


def task(**kwargs: object) -> TaskSpec:
    fields: dict[str, object] = {
        "name": "audit",
        "path": Path("/example/audit.toml"),
        "description": "d",
        "command": ("echo",),
    }
    fields.update(kwargs)
    return TaskSpec(**fields)  # type: ignore[arg-type]


def test_builtin_values_and_strict_argv_expansion() -> None:
    assert VALUES["date"] == "2026-08-23"
    assert build_invocation(task(command=("echo", "{task}", "")), VALUES).argv == (
        "echo",
        "audit",
        "",
    )
    assert substitute("{{literal}}", VALUES, where="x") == "{literal}"
    with pytest.raises(TemplateError):
        build_invocation(task(command=("echo", "{unknown}")), VALUES)


def test_stdin_is_lenient_about_json_braces() -> None:
    invocation = build_invocation(task(stdin='emit {{"ok": true}} for {task}'), VALUES)
    assert invocation.stdin_text == 'emit {{"ok": true}} for audit'
