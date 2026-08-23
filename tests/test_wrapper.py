"""The exec lifecycle: env, locks, timeout, tee, summary, records, notify."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from conftest import HOST, Tree

from automationctl import records
from automationctl.commands import RecordingRunner
from automationctl.config import Automations
from automationctl.errors import ConfigError
from automationctl.locks import named_lock
from automationctl.wrapper import (
    DEFAULT_PATH,
    ExecOptions,
    ExecResult,
    build_env,
    exec_task,
    parse_env_file,
)

NOTIFY_MANIFEST = """\
schema_version = 1

[hosts.testhost]
tasks = ["hello"]

[notify.hook]
type = "command"
command = ["/bin/true", "{task}", "{status}", "{exit_code}"]
"""


def options(tree: Tree, tmp_path: Path, **kwargs: object) -> ExecOptions:
    defaults: dict[str, object] = {
        "state_dir": tree.state,
        "hostname": HOST,
        "env": {"HOME": str(tmp_path)},
        "capture_versions": False,
        "stdout": io.StringIO(),
        "stderr": io.StringIO(),
    }
    defaults.update(kwargs)
    return ExecOptions(**defaults)  # type: ignore[arg-type]


def run_one(tree: Tree, tmp_path: Path, **kwargs: object) -> tuple[ExecResult, Automations]:
    automations = tree.load()
    result = exec_task(automations, automations.tasks["hello"], options(tree, tmp_path, **kwargs))
    return result, automations


# -- environment -----------------------------------------------------------


def test_parse_env_file_handles_comments_exports_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_text('# comment\n\nFOO=bar\nexport BAZ="qux"\nEMPTY=\n', encoding="utf-8")
    assert parse_env_file(path) == {"FOO": "bar", "BAZ": "qux", "EMPTY": ""}


def test_parse_env_file_rejects_a_malformed_line(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_text("NOT_AN_ASSIGNMENT\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid env file line 1"):
        parse_env_file(path)


def test_build_env_layers_base_path_files_and_task_env(tree: Tree, tmp_path: Path) -> None:
    secrets = tmp_path / "agent-env"
    secrets.write_text("TOKEN=secret\nSHARED=from-file\n", encoding="utf-8")
    tree.write_manifest(
        "schema_version = 1\n\n"
        "[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'path_prepend = ["{tmp_path}/bin"]\n'
        f'env_files = ["{secrets}"]\n'
    )
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["true"]\nenv = { SHARED = "from-task" }\n',
    )
    automations = tree.load()
    env = build_env(
        automations,
        automations.tasks["hello"],
        {"HOME": str(tmp_path), "UNRELATED": "dropped"},
        tmp_path / "run",
    )
    assert env["PATH"] == f"{tmp_path}/bin:{DEFAULT_PATH}"
    assert env["TOKEN"] == "secret"
    assert env["SHARED"] == "from-task"
    assert env["AUTOMATIONCTL_TASK"] == "hello"
    assert env["AUTOMATIONCTL_RUN_DIR"] == str(tmp_path / "run")
    assert "UNRELATED" not in env


def test_missing_env_file_fails_the_run(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(
        "schema_version = 1\n\n[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'env_files = ["{tmp_path}/absent"]\n'
    )
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/true"]\n')
    result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_ERROR
    assert result.exit_code == 1


# -- lifecycle -------------------------------------------------------------


def test_successful_run_records_meta_last_and_output(tree: Tree, tmp_path: Path) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "hello {task}"]\n')
    stdout = io.StringIO()
    result, _ = run_one(tree, tmp_path, stdout=stdout)

    assert result.status == records.STATUS_OK
    assert result.exit_code == 0
    assert (result.run_dir / records.STDOUT_FILE).read_text() == "hello hello\n"
    assert stdout.getvalue() == "hello hello\n"

    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert meta["argv"] == ["/bin/echo", "hello hello"]
    assert meta["status"] == "ok"
    assert meta["spec"]["description"] == "d"

    last = records.read_last(tree.state, "hello")
    assert last is not None
    assert last["run_id"] == result.run_id
    assert last["status"] == "ok"


def test_failing_command_is_recorded_and_notified(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(NOTIFY_MANIFEST)
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/false"]\non_failure = ["notify:hook"]\n',
    )
    notify_runner = RecordingRunner()
    result, _ = run_one(tree, tmp_path, notify_runner=notify_runner)

    assert result.status == records.STATUS_FAILED
    assert result.exit_code == 1
    assert notify_runner.calls == [("/bin/true", "hello", "failed", "1")]
    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert meta["notifications"] == [
        {"transport": "hook", "ok": True, "detail": "command exited 0"}
    ]


def test_stderr_is_teed_to_the_run_directory(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/sh", "-c", "echo oops >&2; exit 2"]\n',
    )
    stderr = io.StringIO()
    result, _ = run_one(tree, tmp_path, stderr=stderr)
    assert result.exit_code == 2
    assert (result.run_dir / records.STDERR_FILE).read_text() == "oops\n"
    assert "oops" in stderr.getvalue()


def test_timeout_terminates_the_child(tree: Tree, tmp_path: Path) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/sleep", "30"]\ntimeout = "1s"\n')
    result, _ = run_one(tree, tmp_path, kill_grace=1.0)
    assert result.status == records.STATUS_TIMEOUT
    assert result.duration_seconds < 10
    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert meta["timeout_seconds"] == 1
    assert "timed out" in meta["reason"]


def test_contended_lock_records_a_skip_and_exits_zero(tree: Tree, tmp_path: Path) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "ran"]\nlock = "gpu"\n')
    with named_lock(tree.state / "locks", "gpu"):
        result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_SKIPPED
    assert result.exit_code == 0
    assert not (result.run_dir / records.STDOUT_FILE).exists()


def test_uncontended_lock_lets_the_run_proceed(tree: Tree, tmp_path: Path) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "ran"]\nlock = "gpu"\n')
    result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_OK


def test_summary_cmd_writes_result_json(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        'description = "d"\n'
        'command = ["/bin/echo", "{\\"result\\": \\"all good\\"}"]\n'
        'summary_cmd = ["/bin/cat"]\n',
    )
    result, _ = run_one(tree, tmp_path)
    payload = json.loads((result.run_dir / records.RESULT_FILE).read_text())
    assert payload == {"result": "all good"}


def test_summary_cmd_wraps_non_json_output(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/echo", "plain"]\nsummary_cmd = ["/bin/cat"]\n',
    )
    result, _ = run_one(tree, tmp_path)
    payload = json.loads((result.run_dir / records.RESULT_FILE).read_text())
    assert payload == {"summary": "plain"}


def test_runner_prompt_is_delivered_on_stdin(tree: Tree, tmp_path: Path) -> None:
    tree.write_prompt("hello", "prompt body for {task}")
    tree.write_task(
        "hello",
        'description = "d"\nrunner = "stdin-runner"\nprompt_file = "prompts/hello.md"\n',
    )
    result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_OK
    assert (result.run_dir / records.STDOUT_FILE).read_text() == "prompt body for hello"


def test_runner_prompt_can_be_delivered_in_argv(tree: Tree, tmp_path: Path) -> None:
    tree.write_task("hello", 'description = "d"\nrunner = "argv-runner"\nprompt = "hi there"\n')
    result, _ = run_one(tree, tmp_path)
    assert (result.run_dir / records.STDOUT_FILE).read_text() == "hi there\n"


def test_missing_program_is_a_wrapper_error(tree: Tree, tmp_path: Path) -> None:
    tree.write_task("hello", 'description = "d"\ncommand = ["automationctl-no-such-binary"]\n')
    result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_ERROR
    assert "command not found" in result.reason


def test_missing_cwd_is_a_wrapper_error(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        f'description = "d"\ncommand = ["/bin/true"]\ncwd = "{tmp_path}/absent"\n',
    )
    result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_ERROR
    assert "cwd does not exist" in result.reason


def test_jitter_sleeps_within_the_configured_delay(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/true"]\nrandomized_delay = "5m"\n',
    )
    slept: list[float] = []
    run_one(tree, tmp_path, jitter=True, sleeper=slept.append)
    assert len(slept) == 1
    assert 0 <= slept[0] <= 300


def test_jitter_is_not_applied_without_the_flag(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/true"]\nrandomized_delay = "5m"\n',
    )
    slept: list[float] = []
    run_one(tree, tmp_path, sleeper=slept.append)
    assert slept == []
