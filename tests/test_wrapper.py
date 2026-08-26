"""The exec lifecycle: env, locks, timeout, tee, summary, records, notify."""

from __future__ import annotations

import io
import json
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import HOST, Tree

from automationctl import records
from automationctl.catchup import decide
from automationctl.commands import RecordingRunner
from automationctl.config import Automations
from automationctl.errors import ConfigError
from automationctl.locks import named_lock, run_lock
from automationctl.wrapper import (
    DEFAULT_PATH,
    NOTIFY_SUMMARY_BYTES,
    ExecOptions,
    ExecResult,
    _read_result,
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
command = ["/usr/bin/true", "{task}", "{status}", "{exit_code}"]
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


def test_notify_resolves_variables_from_env_files_not_the_ambient_environment(
    tree: Tree, tmp_path: Path
) -> None:
    """The ntfy URL lives in an env file; a timer's ambient environment has none."""
    secrets = tmp_path / "agent-env"
    secrets.write_text("NTFY_URL=https://ntfy.example/alerts\n", encoding="utf-8")
    tree.write_manifest(
        "schema_version = 1\n\n"
        "[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'env_files = ["{secrets}"]\n\n'
        "[notify.ntfy]\n"
        'url_env = "NTFY_URL"\n'
    )
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/false"]\non_failure = ["notify:ntfy"]\n',
    )
    posted: list[tuple[str, bytes]] = []

    def sender(url: str, body: bytes, headers: Mapping[str, str]) -> None:
        posted.append((url, body))

    result, _ = run_one(tree, tmp_path, notify_sender=sender)

    assert result.status == records.STATUS_FAILED
    assert [outcome.ok for outcome in result.notifications] == [True]
    assert posted[0][0] == "https://ntfy.example/alerts"
    assert b"status: failed" in posted[0][1]


def test_notify_still_fires_when_a_task_env_file_is_unreadable(tree: Tree, tmp_path: Path) -> None:
    """A missing env file is exactly the failure that must reach the phone."""
    secrets = tmp_path / "agent-env"
    secrets.write_text("NTFY_URL=https://ntfy.example/alerts\n", encoding="utf-8")
    tree.write_manifest(
        "schema_version = 1\n\n"
        "[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'env_files = ["{secrets}"]\n\n'
        "[notify.ntfy]\n"
        'url_env = "NTFY_URL"\n'
    )
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/true"]\n'
        f'env_files = ["{tmp_path}/absent"]\n'
        'on_failure = ["notify:ntfy"]\n',
    )
    posted: list[str] = []
    result, _ = run_one(tree, tmp_path, notify_sender=lambda url, body, headers: posted.append(url))

    assert result.status == records.STATUS_ERROR
    assert posted == ["https://ntfy.example/alerts"]


def write_ntfy_tree(tree: Tree, tmp_path: Path, url: str) -> None:
    """An ntfy transport whose URL arrives the way a real one does: an env file."""
    secrets = tmp_path / "agent-env"
    secrets.write_text(f"NTFY_URL={url}\n", encoding="utf-8")
    tree.write_manifest(
        "schema_version = 1\n\n"
        "[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'env_files = ["{secrets}"]\n\n'
        "[notify.ntfy]\n"
        'url_env = "NTFY_URL"\n'
    )


def test_an_arbitrary_transport_exception_cannot_take_down_the_wrapper(
    tree: Tree, tmp_path: Path
) -> None:
    """The invariant: no transport failure may ever cost the child's exit code."""

    class WeirdTransportError(Exception):
        """Something no caller could enumerate — an HTTP stack's own quirk."""

    def explode(url: str, body: bytes, headers: Mapping[str, str]) -> None:
        raise WeirdTransportError("bad status line")

    write_ntfy_tree(tree, tmp_path, "https://ntfy.example/alerts")
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/sh", "-c", "exit 7"]\non_failure = ["notify:ntfy"]\n',
    )
    result, _ = run_one(tree, tmp_path, notify_sender=explode)

    assert result.status == records.STATUS_FAILED
    assert result.exit_code == 7
    assert [outcome.ok for outcome in result.notifications] == [False]
    assert result.notifications[0].transport == "ntfy"
    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert "WeirdTransportError" in meta["notifications"][0]["detail"]


def test_an_uppercase_scheme_is_a_valid_notify_url(tree: Tree, tmp_path: Path) -> None:
    """URL schemes are case-insensitive; the guard must not reject one."""
    write_ntfy_tree(tree, tmp_path, "HTTPS://ntfy.example/alerts")
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/false"]\non_failure = ["notify:ntfy"]\n',
    )
    posted: list[str] = []
    result, _ = run_one(tree, tmp_path, notify_sender=lambda url, body, headers: posted.append(url))
    assert [outcome.ok for outcome in result.notifications] == [True]
    assert posted == ["HTTPS://ntfy.example/alerts"]


def test_a_malformed_notify_url_cannot_take_down_the_wrapper(tree: Tree, tmp_path: Path) -> None:
    """A bad env-file value is a failed notification, not a lost exit code."""
    secrets = tmp_path / "agent-env"
    secrets.write_text("NTFY_URL=ntfy.example/alerts\n", encoding="utf-8")
    tree.write_manifest(
        "schema_version = 1\n\n"
        "[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'env_files = ["{secrets}"]\n\n'
        "[notify.ntfy]\n"
        'url_env = "NTFY_URL"\n'
    )
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/bin/sh", "-c", "exit 7"]\non_failure = ["notify:ntfy"]\n',
    )
    result, _ = run_one(tree, tmp_path)

    assert result.status == records.STATUS_FAILED
    assert result.exit_code == 7
    assert [outcome.ok for outcome in result.notifications] == [False]
    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert "not an http(s) URL" in meta["notifications"][0]["detail"]


def test_missing_env_file_fails_the_run(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(
        "schema_version = 1\n\n[hosts.testhost]\n"
        'tasks = ["hello"]\n'
        f'env_files = ["{tmp_path}/absent"]\n'
    )
    tree.write_task("hello", 'description = "d"\ncommand = ["/usr/bin/true"]\n')
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
    assert stat.S_IMODE((result.run_dir / records.STDOUT_FILE).stat().st_mode) == 0o600
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


def test_meta_records_the_effective_manifest_defaults(tree: Tree, tmp_path: Path) -> None:
    """A run record must explain the timeout that fired, not just the spec's silence."""
    tree.write_manifest(
        "schema_version = 1\n\n"
        "[defaults]\n"
        'timeout = "30m"\n'
        'randomized_delay = "5m"\n'
        'on_failure = ["notify:hook"]\n\n'
        "[hosts.testhost]\n"
        'tasks = ["hello"]\n\n'
        "[notify.hook]\n"
        'type = "command"\n'
        'command = ["/usr/bin/true"]\n'
    )
    tree.write_task(
        "hello", 'description = "d"\ncommand = ["/usr/bin/true"]\nschedule = "daily 03:00"\n'
    )
    result, _ = run_one(tree, tmp_path)
    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert meta["effective"] == {
        "timeout_seconds": 1800,
        "on_failure": ["notify:hook"],
        "randomized_delay_seconds": 300,
        "persistent": True,
    }


def test_notification_summary_omits_an_oversized_result(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    records.write_json(
        run_dir / records.RESULT_FILE,
        {"summary": "x" * (NOTIFY_SUMMARY_BYTES + 1)},
    )

    summary = _read_result(run_dir)

    assert summary.startswith("result omitted from notification (")
    assert summary.endswith(" bytes)")
    assert len(summary) < 100


def test_failing_command_is_recorded_and_notified(tree: Tree, tmp_path: Path) -> None:
    tree.write_manifest(NOTIFY_MANIFEST)
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/false"]\non_failure = ["notify:hook"]\n',
    )
    notify_runner = RecordingRunner()
    result, _ = run_one(tree, tmp_path, notify_runner=notify_runner)

    assert result.status == records.STATUS_FAILED
    assert result.exit_code == 1
    assert notify_runner.calls == [("/usr/bin/true", "hello", "failed", "1")]
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


def test_timeout_fires_while_a_large_prompt_is_still_being_written(
    tree: Tree, tmp_path: Path
) -> None:
    """A prompt larger than the pipe buffer must not outlive the deadline."""
    tree.write_prompt("hello", "x" * 200_000)
    tree.write_task(
        "hello",
        'description = "d"\nrunner = "sleep-runner"\n'
        'prompt_file = "prompts/hello.md"\ntimeout = "2s"\n',
    )
    result, _ = run_one(tree, tmp_path, kill_grace=1.0)
    assert result.status == records.STATUS_TIMEOUT
    assert result.duration_seconds < 15
    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert meta["stdin"] == "prompt"


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


def test_a_second_run_of_an_unlocked_task_skips(tree: Tree, tmp_path: Path) -> None:
    """Two overlapping triggers of a task that declares no lock: one run, one skip."""
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "ran"]\n')
    with run_lock(tree.state / "locks", "hello"):
        result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_SKIPPED
    assert result.exit_code == 0
    assert "already running" in result.reason
    assert not (result.run_dir / records.STDOUT_FILE).exists()


def test_the_implicit_run_lock_cannot_collide_with_a_named_lock(tree: Tree, tmp_path: Path) -> None:
    """A user lock named after the task is a different lock, in a flat namespace."""
    tree.write_task("hello", 'description = "d"\ncommand = ["/bin/echo", "ran"]\nlock = "hello"\n')
    with named_lock(tree.state / "locks", "hello"):
        contended, _ = run_one(tree, tmp_path)
    assert contended.status == records.STATUS_SKIPPED
    assert "lock 'hello'" in contended.reason

    result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_OK
    assert (tree.state / "locks" / "hello.lock").is_file()
    assert (tree.state / "locks" / "tasks" / "hello.lock").is_file()


def test_a_skipped_run_does_not_cover_the_missed_occurrence(tree: Tree, tmp_path: Path) -> None:
    """The loser of a duplicate trigger must not cancel the catch-up still owed."""
    tree.write_task(
        "hello", 'description = "d"\ncommand = ["/bin/echo", "ran"]\nschedule = "daily 03:00"\n'
    )
    with run_lock(tree.state / "locks", "hello"):
        result, automations = run_one(tree, tmp_path)
    assert result.status == records.STATUS_SKIPPED

    last = records.read_last(tree.state, "hello")
    assert last is not None
    assert last["status"] == "skipped", "the skip stays visible to list and status"

    decision = decide(automations, automations.tasks["hello"], state_dir=tree.state)
    assert decision.due is True
    assert "skipped" in decision.reason


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
        f'description = "d"\ncommand = ["/usr/bin/true"]\ncwd = "{tmp_path}/absent"\n',
    )
    result, _ = run_one(tree, tmp_path)
    assert result.status == records.STATUS_ERROR
    assert "cwd does not exist" in result.reason


def test_jitter_sleeps_within_the_configured_delay(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/true"]\nrandomized_delay = "5m"\n',
    )
    slept: list[float] = []
    run_one(tree, tmp_path, jitter=True, sleeper=slept.append)
    assert len(slept) == 1
    assert 0 <= slept[0] <= 300


def test_jitter_is_not_applied_without_the_flag(tree: Tree, tmp_path: Path) -> None:
    tree.write_task(
        "hello",
        'description = "d"\ncommand = ["/usr/bin/true"]\nrandomized_delay = "5m"\n',
    )
    slept: list[float] = []
    run_one(tree, tmp_path, sleeper=slept.append)
    assert slept == []
