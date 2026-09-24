"""Wrapper lifecycle behavior retained by standalone task specs."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from automationctl import records
from automationctl.errors import ConfigError
from automationctl.locks import named_lock, run_lock
from automationctl.spec import NotifyTransport, TaskSpec
from automationctl.wrapper import (
    DEFAULT_PATH,
    NOTIFY_SUMMARY_BYTES,
    ExecOptions,
    _read_result,
    build_env,
    exec_task,
    parse_env_file,
)


def task(tmp_path: Path, **kw: object) -> TaskSpec:
    values: dict[str, object] = {
        "name": "hello",
        "path": tmp_path / "hello.toml",
        "description": "d",
        "command": ("/bin/echo", "hello {task}"),
    }
    values.update(kw)
    return TaskSpec(**values)  # type: ignore[arg-type]


def options(tmp_path: Path, **kw: object) -> ExecOptions:
    values: dict[str, object] = {
        "state_dir": tmp_path / "state",
        "hostname": "host",
        "env": {"HOME": str(tmp_path)},
        "capture_versions": False,
        "stdout": io.StringIO(),
        "stderr": io.StringIO(),
    }
    values.update(kw)
    return ExecOptions(**values)  # type: ignore[arg-type]


def test_parse_env_file_handles_comments_exports_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_text('# c\nexport BAZ="qux"\nEMPTY=\n', encoding="utf-8")
    assert parse_env_file(path) == {"BAZ": "qux", "EMPTY": ""}
    path.write_text("BAD\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid env file line 1"):
        parse_env_file(path)


def test_build_env_layers_files_task_values_and_path(tmp_path: Path) -> None:
    envfile = tmp_path / "env"
    envfile.write_text("TOKEN=secret\nSHARED=file\n", encoding="utf-8")
    env = build_env(
        task(
            tmp_path,
            env_files=(str(envfile),),
            path_prepend=(str(tmp_path / "bin"),),
            env={"SHARED": "task"},
        ),
        {"HOME": str(tmp_path), "OTHER": "drop"},
        tmp_path / "run",
    )
    assert (
        env["PATH"] == f"{tmp_path}/bin:{DEFAULT_PATH}"
        and env["TOKEN"] == "secret"
        and env["SHARED"] == "task"
        and "OTHER" not in env
    )


def test_success_records_output_meta_last_and_private_mode(tmp_path: Path) -> None:
    output = io.StringIO()
    result = exec_task(task(tmp_path), options(tmp_path, stdout=output))
    assert (
        result.status == records.STATUS_OK
        and (result.run_dir / records.STDOUT_FILE).read_text() == "hello hello\n"
    )
    assert (
        stat.S_IMODE((result.run_dir / records.STDOUT_FILE).stat().st_mode) == 0o600
        and output.getvalue() == "hello hello\n"
    )
    assert records.read_meta(result.run_dir)["argv"] == ["/bin/echo", "hello hello"]  # type: ignore[index]
    assert records.read_last(tmp_path / "state", "hello")["status"] == "ok"  # type: ignore[index]


def test_stderr_timeout_and_large_stdin_writer_do_not_hang(tmp_path: Path) -> None:
    fail = exec_task(
        task(tmp_path, command=("/bin/sh", "-c", "echo oops >&2; exit 2")), options(tmp_path)
    )
    assert fail.exit_code == 2 and (fail.run_dir / records.STDERR_FILE).read_text() == "oops\n"
    timed = exec_task(
        task(tmp_path, name="slow", command=("/bin/sleep", "10"), timeout_seconds=1),
        options(tmp_path, kill_grace=0.1),
    )
    assert timed.status == records.STATUS_TIMEOUT and timed.duration_seconds < 5
    big = exec_task(
        task(
            tmp_path,
            name="big",
            command=("/bin/sleep", "10"),
            stdin="x" * 200_000,
            timeout_seconds=1,
        ),
        options(tmp_path, kill_grace=0.1),
    )
    assert big.status == records.STATUS_TIMEOUT and big.duration_seconds < 5


def test_lock_contention_and_lock_namespaces_are_preserved(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with run_lock(state / "locks", "hello"):
        skipped = exec_task(task(tmp_path), options(tmp_path))
    assert skipped.status == records.STATUS_SKIPPED and "already running" in skipped.reason
    with named_lock(state / "locks", "hello"):
        skipped = exec_task(task(tmp_path, lock="hello"), options(tmp_path))
    assert skipped.status == records.STATUS_SKIPPED and "lock 'hello'" in skipped.reason
    ok = exec_task(task(tmp_path, lock="hello"), options(tmp_path))
    assert ok.status == records.STATUS_OK
    assert (state / "locks" / "hello.lock").is_file() and (
        state / "locks" / "tasks" / "hello.lock"
    ).is_file()


def test_summary_json_plain_and_bounded_notification_result(tmp_path: Path) -> None:
    result = exec_task(
        task(tmp_path, command=("/bin/echo", '{"result":"ok"}'), summary_cmd=("/bin/cat",)),
        options(tmp_path),
    )
    assert json.loads((result.run_dir / records.RESULT_FILE).read_text()) == {"result": "ok"}
    records.write_json(
        tmp_path / "large" / records.RESULT_FILE, {"summary": "x" * (NOTIFY_SUMMARY_BYTES + 1)}
    )
    assert _read_result(tmp_path / "large").startswith("result omitted from notification")


def test_failure_notifications_use_task_env_and_contain_transport_exceptions(
    tmp_path: Path,
) -> None:
    envfile = tmp_path / "secrets"
    envfile.write_text("URL=HTTPS://ntfy.example/alerts\n", encoding="utf-8")
    sent: list[tuple[str, bytes]] = []
    spec = task(
        tmp_path,
        command=("/bin/sh", "-c", "echo bad >&2; exit 7"),
        env_files=(str(envfile),),
        notify={"n": NotifyTransport("n", "ntfy", url_env="URL")},
        on_failure=("notify:n",),
    )
    result = exec_task(
        spec, options(tmp_path, notify_sender=lambda url, body, headers: sent.append((url, body)))
    )
    assert result.exit_code == 7 and sent[0][0] == "HTTPS://ntfy.example/alerts"
    assert b"stderr tail" in sent[0][1] and result.notifications[0].ok

    def explode(url: str, body: bytes, headers: object) -> None:
        raise RuntimeError("bad")

    result = exec_task(spec, options(tmp_path, notify_sender=explode))
    assert (
        result.exit_code == 7
        and result.notifications[0].ok is False
        and "RuntimeError" in result.notifications[0].detail
    )


def test_missing_inputs_program_or_cwd_become_recorded_errors(tmp_path: Path) -> None:
    missing = exec_task(task(tmp_path, env_files=(str(tmp_path / "missing"),)), options(tmp_path))
    assert missing.status == records.STATUS_ERROR
    program = exec_task(
        task(tmp_path, name="program", command=("no-such-program",)), options(tmp_path)
    )
    assert program.status == records.STATUS_ERROR and "command not found" in program.reason
    cwd = exec_task(task(tmp_path, name="cwd", cwd=str(tmp_path / "missing")), options(tmp_path))
    assert cwd.status == records.STATUS_ERROR and "cwd does not exist" in cwd.reason


def test_jitter_only_applies_at_scheduler_request(tmp_path: Path) -> None:
    slept: list[float] = []
    spec = task(tmp_path, jitter_seconds=3)
    exec_task(spec, options(tmp_path, sleeper=slept.append))
    assert slept == []
    exec_task(spec, options(tmp_path, jitter=True, sleeper=slept.append))
    assert len(slept) == 1 and 0 <= slept[0] <= 3
