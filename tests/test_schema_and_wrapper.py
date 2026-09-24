from __future__ import annotations

import io
import sys
from pathlib import Path

from conftest import write_task

from automationctl import records
from automationctl.commands import RecordingRunner
from automationctl.config import dump_task, load_task
from automationctl.spec import NotifyTransport, TaskSpec
from automationctl.wrapper import ExecOptions, exec_task


def test_schema_requires_standalone_identity_and_rejects_legacy(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('schema_version = 1\ndescription = "x"\ncommand = ["true"]\n', encoding="utf-8")
    try:
        load_task(bad)
    except Exception as error:
        assert "unsupported schema_version" in str(error)
    else:
        raise AssertionError("legacy schema unexpectedly accepted")


def test_task_round_trip_quotes_multiline_stdin_and_raw_schedule(tmp_path: Path) -> None:
    source = write_task(
        tmp_path / "quoted.toml",
        "quoted",
        command='["/bin/echo", ""]',
        extra=(
            'stdin = """line one\\nline two\\n"""\n'
            'env = { "A key" = "value" }\n'
            'schedule = { systemd = "Mon *-*-* 01:00:00" }\n'
            '[notify."dot.name"]\ntype = "command"\ncommand = ["/bin/true"]'
        ),
    )
    task = load_task(source)
    installed = tmp_path / "installed.toml"
    installed.write_text(dump_task(task), encoding="utf-8")
    loaded = load_task(installed)
    assert loaded.stdin == "line one\nline two\n"
    assert loaded.env["A key"] == "value"
    assert loaded.schedule is not None and loaded.schedule.kind == "raw"
    assert "dot.name" in loaded.notify


def test_wrapper_preserves_argv_stdin_timeout_records_and_notify(tmp_path: Path) -> None:
    task = TaskSpec(
        name="run",
        path=tmp_path / "task.toml",
        description="run",
        command=(
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read()); print(sys.argv[1])",
            "",
        ),
        stdin='code braces: {"a": 1}; {task}',
        timeout_seconds=10,
        notify={"hook": NotifyTransport("hook", "command", command=("/bin/true",))},
        on_failure=("notify:hook",),
    )
    output = io.StringIO()
    result = exec_task(
        task,
        ExecOptions(
            state_dir=tmp_path / "state",
            hostname="host",
            env={"HOME": str(tmp_path)},
            stdout=output,
            stderr=io.StringIO(),
            capture_versions=False,
            notify_runner=RecordingRunner(),
        ),
    )
    assert result.status == records.STATUS_OK
    meta = records.read_meta(result.run_dir)
    assert meta is not None
    assert meta["argv"][-1] == ""
    assert 'code braces: {"a": 1}; run' in output.getvalue()
    assert records.read_last(tmp_path / "state", "run")["host"] == "host"  # type: ignore[index]
