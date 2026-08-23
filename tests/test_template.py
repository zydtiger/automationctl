"""Runner expansion and placeholder substitution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from automationctl.errors import TemplateError
from automationctl.spec import Runner, TaskSpec
from automationctl.template import build_invocation, builtin_values, substitute

NOW = datetime(2026, 8, 23, 4, 30, tzinfo=UTC)
VALUES = builtin_values(task="audit", hostname="workstation", run_dir="/runs/1", now=NOW)


def task(**kwargs: object) -> TaskSpec:
    fields: dict[str, object] = {
        "name": "audit",
        "path": Path("/example/tasks/audit.toml"),
        "description": "d",
    }
    fields.update(kwargs)
    return TaskSpec(**fields)  # type: ignore[arg-type]


def test_builtin_values_cover_the_documented_vocabulary() -> None:
    assert VALUES == {
        "date": "2026-08-23",
        "hostname": "workstation",
        "task": "audit",
        "run_dir": "/runs/1",
    }


def test_substitute_replaces_known_placeholders() -> None:
    assert substitute("{task} on {hostname}", VALUES, where="x") == "audit on workstation"


def test_substitute_supports_literal_braces() -> None:
    assert substitute("{{not-a-placeholder}}", VALUES, where="x") == "{not-a-placeholder}"


def test_substitute_rejects_unknown_placeholder() -> None:
    with pytest.raises(TemplateError, match="unknown placeholder"):
        substitute("{nope}", VALUES, where="x")


def test_command_is_verbatim_argv() -> None:
    invocation = build_invocation(task(command=("echo", "{task}")), None, None, VALUES)
    assert invocation.argv == ("echo", "audit")
    assert invocation.stdin_text is None


def test_leading_tilde_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    invocation = build_invocation(task(command=("rsync", "~/notes/")), None, None, VALUES)
    assert invocation.argv == ("rsync", f"{tmp_path}/notes/")


def test_runner_argv_substitutes_the_prompt() -> None:
    runner = Runner(name="argv", argv=("agent", "exec", "{prompt}"))
    invocation = build_invocation(task(runner="argv"), runner, "hello {task}", VALUES)
    assert invocation.argv == ("agent", "exec", "hello audit")
    assert invocation.stdin_text is None


def test_runner_stdin_delivers_the_prompt_on_stdin() -> None:
    runner = Runner(name="stdin", argv=("agent", "-p"), stdin="prompt")
    invocation = build_invocation(task(runner="stdin"), runner, "hello", VALUES)
    assert invocation.argv == ("agent", "-p")
    assert invocation.stdin_text == "hello"


def test_prompt_content_is_not_rescanned_for_placeholders() -> None:
    runner = Runner(name="argv", argv=("agent", "{prompt}"))
    invocation = build_invocation(task(runner="argv"), runner, "report {mystery}", VALUES)
    assert invocation.argv == ("agent", "report {mystery}")


def test_command_task_cannot_use_the_prompt_placeholder() -> None:
    with pytest.raises(TemplateError, match="unknown placeholder"):
        build_invocation(task(command=("echo", "{prompt}")), None, None, VALUES)
