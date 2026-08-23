"""The policy engine: schema gates, reference checks, and the deny-list."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import Tree

from automationctl.lint import ERROR, WARNING, lint


def messages(tree: Tree, level: str = ERROR, host: str = "testhost") -> list[str]:
    report = lint(tree.load(host), backend="systemd")
    return [item.message for item in report.diagnostics if item.level == level]


def test_clean_repository_passes(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "hi"]\n')
    report = lint(tree.load(), backend="systemd")
    assert report.ok
    assert report.errors == ()


def test_command_and_runner_are_mutually_exclusive(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\nrunner = "argv-runner"\nprompt = "hi"\n',
    )
    assert any("mutually exclusive" in item for item in messages(tree))


def test_neither_command_nor_runner_is_an_error(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\n')
    assert any("exactly one of command or runner" in item for item in messages(tree))


def test_prompt_and_prompt_file_are_mutually_exclusive(tree: Tree) -> None:
    tree.write_prompt("hello", "text")
    tree.write_task(
        "hello",
        'description = "d"\nrunner = "argv-runner"\nprompt = "a"\n'
        'prompt_file = "prompts/hello.md"\n',
    )
    assert any("prompt and prompt_file are mutually exclusive" in item for item in messages(tree))


def test_runner_requires_a_prompt(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\nrunner = "argv-runner"\n')
    assert any("runner requires prompt or prompt_file" in item for item in messages(tree))


def test_prompt_requires_a_runner(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nprompt = "hi"\n')
    assert any("require a runner" in item for item in messages(tree))


def test_unknown_runner_is_reported(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\nrunner = "nope"\nprompt = "hi"\n')
    assert any("unknown runner: nope" in item for item in messages(tree))


def test_missing_prompt_file_is_reported(tree: Tree) -> None:
    tree.write_task(
        "hello", 'description = "d"\nrunner = "argv-runner"\nprompt_file = "prompts/gone.md"\n'
    )
    assert any("prompt_file not found" in item for item in messages(tree))


def test_undefined_notify_transport_is_reported(tree: Tree) -> None:
    tree.write_task(
        "hello", 'description = "d"\ncommand = ["true"]\non_failure = ["notify:pager"]\n'
    )
    assert any("undefined notify transport: pager" in item for item in messages(tree))


def test_unsupported_on_failure_entry_is_reported(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\non_failure = ["email"]\n')
    assert any("unsupported on_failure entry" in item for item in messages(tree))


def test_forbidden_argv_is_rejected(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "--danger"]\n')
    assert any("forbidden entry '--danger'" in item for item in messages(tree))


def test_allow_full_access_downgrades_the_deny_list_hit(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/echo", "--danger"]\nallow_full_access = true\n',
    )
    assert messages(tree) == []
    assert any("permitted by allow_full_access" in item for item in messages(tree, WARNING))


def test_runner_level_allow_full_access_is_honoured(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\nrunner = "full-runner"\nprompt = "hi"\n')
    assert messages(tree) == []


def test_schedule_unexpressible_by_the_backend_is_rejected(tree: Tree) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\n\n'
        "[schedule]\nlaunchd = [{ Weekday = 1, Hour = 9, Minute = 0 }]\n",
    )
    report = lint(tree.load(), backend="systemd")
    assert any("not expressible by the systemd backend" in item.message for item in report.errors)
    assert lint(tree.load(), backend="launchd").ok


def test_unparsable_task_file_is_surfaced(tree: Tree) -> None:
    tree.write_task("hello", "description = 3\n")
    assert any("description must be a string" in item for item in messages(tree))


def test_host_selecting_an_unknown_task_is_an_error(tree: Tree) -> None:
    tree.write_manifest('schema_version = 1\n\n[hosts.testhost]\ntasks = ["hello", "ghost"]\n')
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\n')
    assert any("selects unknown task: ghost" in item for item in messages(tree))


def test_undeclared_host_is_a_warning_not_an_error(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\n')
    report = lint(tree.load("otherhost"), backend="systemd")
    assert report.ok
    assert any("is not declared in the manifest" in item.message for item in report.warnings)


def test_invalid_lock_name_is_rejected(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nlock = "../escape"\n')
    assert any("invalid lock name" in item for item in messages(tree))


def test_lint_can_target_specific_tasks(tree: Tree) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\n')
    tree.write_task("broken", 'description = "d"\n')
    assert lint(tree.load(), backend="systemd", tasks=["hello"]).ok
    assert not lint(tree.load(), backend="systemd", tasks=["broken"]).ok


def test_targeted_lint_ignores_another_task_that_fails_to_load(tree: Tree) -> None:
    """A malformed spec belonging to another host must not block this one."""
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\n')
    tree.write_task("theirs", "description = 3\n")
    assert not lint(tree.load(), backend="systemd").ok
    assert lint(tree.load(), backend="systemd", tasks=["hello"]).ok


def test_reserved_task_name_is_rejected(tree: Tree) -> None:
    tree.write_task("catchup", 'description = "d"\ncommand = ["true"]\n')
    assert any("reserved task name" in item for item in messages(tree))


def test_invalid_task_name_is_rejected_at_load(tree: Tree) -> None:
    tree.write_task("bad name", 'description = "d"\ncommand = ["true"]\n')
    assert any("invalid task name" in item for item in messages(tree))


def test_prompt_file_may_be_written_with_a_tilde(
    tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lint must resolve prompt_file exactly as the runtime does."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "shared.md").write_text("body", encoding="utf-8")
    tree.write_task(
        "hello",
        'description = "d"\nrunner = "argv-runner"\nprompt_file = "~/prompts/shared.md"\n',
    )
    assert messages(tree) == []
