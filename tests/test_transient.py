"""Transient tests use a stateful fake scheduler; no live manager is contacted."""

from __future__ import annotations

import io
import plistlib
import signal
import threading
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from automationctl import backends, paths, records, transient
from automationctl.backends import Backend
from automationctl.cli import app
from automationctl.commands import CommandResult
from automationctl.config import load_task
from automationctl.errors import AutomationctlError
from automationctl.spec import TaskSpec
from automationctl.wrapper import ExecOptions


class Scheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.present: set[str] = set()
        self.reject = False
        self.fail_cleanup = False
        self.domain_available = True

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        args = tuple(argv)
        self.calls.append(args)
        if args[0] == "systemd-run":
            service = next(a.removeprefix("--unit=") for a in args if a.startswith("--unit="))
            self.present.add(service)
            if any(a.startswith("--on-calendar=") for a in args):
                self.present.add(service.replace(".service", ".timer"))
            # Rejection can be ambiguous: objects may already exist.
            return CommandResult(
                args, 1 if self.reject else 0, stderr="rejected" if self.reject else ""
            )
        if args[0] == "systemctl" and "show" in args:
            return CommandResult(args, 0, "loaded" if args[3] in self.present else "not-found")
        if args[0] == "launchctl" and args[1] == "bootstrap":
            plist = plistlib.loads(Path(args[-1]).read_bytes())
            self.present.add(args[2] + "/" + plist["Label"])
            return CommandResult(args, 0)
        if args[0] == "launchctl" and args[1] == "print":
            if args[-1].count("/") == 1:
                return CommandResult(args, 0 if self.domain_available else 1)
            return CommandResult(args, 0 if args[-1] in self.present else 113)
        if "stop" in args or "bootout" in args:
            if self.fail_cleanup:
                return CommandResult(args, 1, stderr="cleanup rejected")
            self.present.discard(args[-1])
        return CommandResult(args, 0)


def scheduler(tmp_path: Path, name: str = "systemd") -> tuple[Backend, Scheduler]:
    runner = Scheduler()
    backend = backends.create(
        name,
        runner=runner,
        executable="/usr/bin/automationctl",
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        unit_dir=tmp_path / "units",
    )
    return backend, runner


def task(
    tmp_path: Path, *, command: tuple[str, ...] = ("/bin/echo", "hello"), timeout: int | None = None
) -> TaskSpec:
    return TaskSpec(
        "demo", tmp_path / "demo.toml", "demo", command, cwd=str(tmp_path), timeout_seconds=timeout
    )


def options(state: Path) -> ExecOptions:
    return ExecOptions(
        state,
        "testhost",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        capture_versions=False,
        kill_grace=0.1,
    )


@pytest.mark.parametrize("backend_name", ["systemd", "launchd"])
def test_dry_run_writes_nothing(tmp_path: Path, backend_name: str) -> None:
    backend, runner = scheduler(tmp_path, backend_name)
    execution_id = transient.submit(task(tmp_path), backend, None, dry_run=True)
    assert transient.is_id(execution_id)
    assert not runner.calls
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("backend_name", ["systemd", "launchd"])
@pytest.mark.parametrize(
    ("command", "expected", "exit_code"),
    [
        (("/bin/echo", "hello"), "ok", 0),
        (("/bin/sh", "-c", "echo bad >&2; exit 7"), "failed", 7),
        (("/bin/sleep", "5"), "timeout", -signal.SIGTERM),
    ],
)
def test_execution_and_retained_history(
    tmp_path: Path, backend_name: str, command: tuple[str, ...], expected: str, exit_code: int
) -> None:
    backend, runner = scheduler(tmp_path, backend_name)
    execution_id = transient.submit(task(tmp_path, command=command, timeout=1), backend, None)
    transient.execute(backend.state_dir, execution_id, options(backend.state_dir), backend)
    data = transient.read(backend.state_dir, execution_id)
    assert data["status"] == expected and data["exit_code"] == exit_code
    assert records.latest_run(
        transient.directory(backend.state_dir, execution_id) / "history", "demo"
    )
    assert not backend.unit_dir.exists()
    assert not (backend.config_home / "automationctl" / "tasks").exists()
    if backend_name == "launchd":
        assert not runner.present
        assert not (transient.directory(backend.state_dir, execution_id) / "job.plist").exists()
    else:
        transient.finish(backend.state_dir, execution_id, backend, {})
        runner.present.clear()  # manager collection happens after ExecStopPost exits
    transient.reconcile(backend.state_dir, execution_id, backend)
    assert transient.read(backend.state_dir, execution_id)["cleanup"] == "collected"


def test_claim_blocks_second_delivery_and_version_probe(tmp_path: Path) -> None:
    backend, _ = scheduler(tmp_path)
    marker = tmp_path / "invocations"
    source = task(tmp_path, command=("/bin/sh", "-c", f"echo once >> {marker}"))
    execution_id = transient.submit(source, backend, None)
    for _ in range(2):
        transient.execute(backend.state_dir, execution_id, options(backend.state_dir), backend)
    assert marker.read_text() == "once\n"
    other = transient.submit(source, backend, None)
    assert other != execution_id
    transient.execute(backend.state_dir, other, options(backend.state_dir), backend)
    assert marker.read_text() == "once\nonce\n"


def test_crash_after_claim_never_reruns(tmp_path: Path) -> None:
    backend, runner = scheduler(tmp_path)
    execution_id = transient.submit(task(tmp_path), backend, None)
    with transient.locked(backend.state_dir, execution_id) as data:
        data.update(claimed=True, status="running")
    transient.execute(backend.state_dir, execution_id, options(backend.state_dir), backend)
    runner.present.clear()
    transient.reconcile(backend.state_dir, execution_id, backend)
    assert transient.read(backend.state_dir, execution_id)["status"] == "abandoned"


@pytest.mark.parametrize("backend_name", ["systemd", "launchd"])
def test_pending_cancellation_is_scoped_and_idempotent(tmp_path: Path, backend_name: str) -> None:
    backend, runner = scheduler(tmp_path, backend_name)
    first = transient.submit(task(tmp_path), backend, datetime.now(UTC) + timedelta(minutes=5))
    second = transient.submit(task(tmp_path), backend, None)
    for _ in range(2):
        transient.reconcile(backend.state_dir, first, backend, cancel=True)
    assert transient.read(backend.state_dir, first)["status"] == "cancelled"
    assert transient.read(backend.state_dir, second)["status"] == "scheduled"
    assert len(runner.present) == 1
    transient.execute(backend.state_dir, first, options(backend.state_dir), backend)
    assert not (transient.directory(backend.state_dir, first) / "history").exists()


def test_cleanup_failure_does_not_overwrite_result(tmp_path: Path) -> None:
    backend, runner = scheduler(tmp_path)
    execution_id = transient.submit(task(tmp_path), backend, None)
    transient.execute(backend.state_dir, execution_id, options(backend.state_dir), backend)
    runner.fail_cleanup = True
    with pytest.raises(AutomationctlError):
        transient.reconcile(backend.state_dir, execution_id, backend)
    data = transient.read(backend.state_dir, execution_id)
    assert data["status"] == "ok" and data["exit_code"] == 0 and data["cleanup"] == "failed"
    runner.fail_cleanup = False
    transient.reconcile(backend.state_dir, execution_id, backend)
    assert transient.read(backend.state_dir, execution_id)["cleanup"] == "collected"


def test_ambiguous_acceptance_retained_without_retry(tmp_path: Path) -> None:
    backend, runner = scheduler(tmp_path)
    runner.reject = True
    with pytest.raises(AutomationctlError, match="not confirmed"):
        transient.submit(task(tmp_path), backend, None)
    (data,) = transient.discover(backend.state_dir)
    assert data["acceptance"] == "unknown" and data["scheduler_returncode"] == 1
    assert len(runner.calls) == 1
    transient.reconcile(backend.state_dir, data["id"], backend, cancel=True)
    assert not runner.present


def test_deadlines_reject_ambiguous_and_past_times() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert transient.deadline("10m", None, now) == now + timedelta(minutes=10)
    assert transient.deadline(None, "2030-01-01T01:00:00+01:00", now - timedelta(seconds=1)) == now
    for after, at in [
        ("1m", "2031-01-01Z"),
        (None, "2031-01-01T09:00"),
        (None, "nonsense"),
        (None, "2029-12-31T00:00Z"),
    ]:
        with pytest.raises(AutomationctlError):
            transient.deadline(after, at, now)


def test_systemd_timer_and_command_quoting(tmp_path: Path) -> None:
    backend, _ = scheduler(tmp_path)
    backend.executable = "/opt/a b/$app%/automationctl"
    argv = transient.submission_argv(backend, "tx:" + "a" * 32, datetime(2031, 1, 1, tzinfo=UTC))
    assert "--collect" in argv and "--service-type=exec" in argv
    assert "--timer-property=RemainAfterElapse=false" in argv
    assert any(arg.startswith("--on-calendar=2031-01-01") and arg.endswith(" UTC") for arg in argv)
    assert argv[-3:] == [backend.executable, "_transient-exec", "tx:" + "a" * 32]
    assert "$$app%%" in next(arg for arg in argv if "ExecStopPost=" in arg)


def test_launchd_uses_private_plist_and_does_not_kickstart(tmp_path: Path) -> None:
    backend, runner = scheduler(tmp_path, "launchd")
    execution_id = transient.submit(task(tmp_path), backend, None)
    root = transient.directory(backend.state_dir, execution_id)
    data = plistlib.loads((root / "job.plist").read_bytes())
    assert data["LaunchOnlyOnce"] and data["RunAtLoad"]
    assert "StartCalendarInterval" not in data
    assert runner.calls[0][0:2] == ("launchctl", "bootstrap")
    assert not any("kickstart" in args for args in runner.calls)
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "task.toml").stat().st_mode & 0o777 == 0o600


def test_launchd_unavailable_domain_is_not_absence(tmp_path: Path) -> None:
    backend, runner = scheduler(tmp_path, "launchd")
    execution_id = transient.submit(task(tmp_path), backend, None)
    runner.present.clear()
    runner.domain_available = False
    with pytest.raises(AutomationctlError, match="cannot inspect"):
        transient.reconcile(backend.state_dir, execution_id, backend)
    assert transient.read(backend.state_dir, execution_id)["status"] == "scheduled"


def test_prune_preserves_active_and_cleanup_failed(tmp_path: Path) -> None:
    backend, runner = scheduler(tmp_path)
    done = transient.submit(task(tmp_path), backend, None)
    transient.execute(backend.state_dir, done, options(backend.state_dir), backend)
    active = transient.submit(task(tmp_path), backend, None)
    runner.fail_cleanup = True
    assert transient.prune(backend.state_dir, 0, backend) == 0
    runner.fail_cleanup = False
    assert transient.prune(backend.state_dir, 0, backend) == 1
    assert transient.read(backend.state_dir, active)["status"] == "scheduled"


def test_cli_submission_materializes_input_and_reads_history(
    tmp_path: Path,
    runner: CliRunner,
    isolated_env: dict[str, str],
) -> None:
    prompt = tmp_path / "prompt"
    prompt.write_text("payload")
    result = runner.invoke(
        app,
        [
            "submit",
            "--transient",
            "demo",
            "--stdin-file",
            str(prompt),
            "--env",
            "FOO=bar",
            "--",
            "/bin/cat",
        ],
        env=isolated_env,
    )
    assert result.exit_code == 0, result.output
    state = paths.state_dir(isolated_env)
    (data,) = transient.discover(state)
    execution_id = data["id"]
    source = load_task(transient.directory(state, execution_id) / "task.toml")
    assert source.stdin == "payload" and source.env == {"FOO": "bar"}
    prompt.unlink()
    executed = runner.invoke(app, ["_transient-exec", execution_id], env=isolated_env)
    assert executed.exit_code == 0, executed.output
    assert "payload" in runner.invoke(app, ["logs", execution_id], env=isolated_env).output
    assert '"status": "ok"' in runner.invoke(app, ["status", execution_id], env=isolated_env).output
    assert execution_id in runner.invoke(app, ["list"], env=isolated_env).output


@pytest.mark.parametrize(
    "arguments",
    [
        ["submit", "--transient", "demo"],
        ["submit", "demo", "--after", "10m"],
        ["submit", "demo", "--", "/bin/echo", "wrong"],
        ["submit", "--transient", "demo", "--stdin", "x", "--stdin-file", "x", "--", "/bin/cat"],
        ["submit", "--transient", "demo", "--at", "2030-01-01T00:00", "--", "/bin/true"],
    ],
)
def test_cli_invalid_forms_do_not_write(
    runner: CliRunner, isolated_env: dict[str, str], arguments: list[str]
) -> None:
    result = runner.invoke(app, arguments, env=isolated_env)
    assert result.exit_code != 0
    assert not paths.state_dir(isolated_env).exists()


def test_cli_dry_run_no_files(runner: CliRunner, isolated_env: dict[str, str]) -> None:
    result = runner.invoke(
        app,
        [
            "submit",
            "--transient",
            "demo",
            "--dry-run",
            "--after",
            "1m",
            "--",
            "/bin/echo",
            "--help",
            "$literal",
            "",
        ],
        env=isolated_env,
    )
    assert result.exit_code == 0, result.output
    assert not paths.state_dir(isolated_env).exists()
    assert not paths.tasks_dir(isolated_env).exists()


def test_cancellation_event_stops_child(tmp_path: Path) -> None:
    from automationctl.wrapper import exec_task

    cancellation = threading.Event()
    execution_options = options(tmp_path / "state")
    execution_options.cancel_event = cancellation
    thread = threading.Timer(0.15, cancellation.set)
    thread.start()
    try:
        result = exec_task(task(tmp_path, command=("/bin/sleep", "30")), execution_options)
    finally:
        thread.join()
    assert result.status == "cancelled" and result.duration_seconds < 2


def test_cancellation_before_start_does_not_launch(tmp_path: Path) -> None:
    from automationctl.wrapper import exec_task

    cancellation = threading.Event()
    cancellation.set()
    opts = options(tmp_path / "state")
    opts.cancel_event = cancellation
    marker = tmp_path / "ran"
    result = exec_task(task(tmp_path, command=("/usr/bin/touch", str(marker))), opts)
    assert result.status == "cancelled" and not marker.exists()


def test_timeout_kills_child_that_ignores_term(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    from automationctl.wrapper import exec_task

    child_pid = tmp_path / "child.pid"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent = (
        "import subprocess,sys,time,pathlib; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    result = exec_task(
        task(tmp_path, command=(sys.executable, "-c", parent), timeout=1),
        options(tmp_path / "state"),
    )
    assert result.status == "timeout"
    pid = int(child_pid.read_text())
    try:
        # An unreaped zombie has already exited and cannot do more work.
        observed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
        assert not observed or observed.startswith("Z")
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def test_recovery_preserves_completed_wrapper_result(tmp_path: Path) -> None:
    backend, runner = scheduler(tmp_path)
    execution_id = transient.submit(task(tmp_path), backend, None)
    transient.execute(backend.state_dir, execution_id, options(backend.state_dir), backend)
    with transient.locked(backend.state_dir, execution_id) as data:
        data.update(status="running", exit_code=None)
    runner.present.clear()
    transient.reconcile(backend.state_dir, execution_id, backend)
    data = transient.read(backend.state_dir, execution_id)
    assert data["status"] == "ok" and data["exit_code"] == 0


def test_launchd_terminal_result_precedes_bootout(tmp_path: Path) -> None:
    backend, recorder = scheduler(tmp_path, "launchd")
    execution_id = transient.submit(task(tmp_path), backend, None)
    original = recorder.run

    def observe(argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        if "bootout" in argv:
            data = transient.read(backend.state_dir, execution_id)
            assert data["status"] == "ok" and data["cleanup"] == "delegated"
            assert (Path(data["run_dir"]) / "stdout.log").read_text() == "hello\n"
            assert not (transient.directory(backend.state_dir, execution_id) / "job.plist").exists()
        return original(argv, timeout=timeout)

    recorder.run = observe  # type: ignore[method-assign]
    transient.execute(backend.state_dir, execution_id, options(backend.state_dir), backend)


def test_same_display_name_as_installed_task_is_independent(
    runner: CliRunner,
    isolated_env: dict[str, str],
) -> None:
    from automationctl.config import load_installed

    assert (
        runner.invoke(
            app, ["add", "demo", "--", "/bin/echo", "installed"], env=isolated_env
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["submit", "--transient", "demo", "--", "/bin/echo", "transient"], env=isolated_env
        ).exit_code
        == 0
    )
    assert load_installed("demo", isolated_env).command == ("/bin/echo", "installed")
    assert runner.invoke(app, ["submit", "demo"], env=isolated_env).exit_code == 0


def test_simultaneous_submissions_and_duplicate_dispatch(tmp_path: Path) -> None:
    import subprocess
    import sys

    backend, _ = scheduler(tmp_path)
    marker = tmp_path / "runs"
    source = task(tmp_path, command=("/bin/sh", "-c", f"echo once >> {marker}; sleep 0.3"))
    execution_id = transient.submit(source, backend, None)
    env = {
        **__import__("os").environ,
        "XDG_CONFIG_HOME": str(backend.config_home),
        "XDG_STATE_HOME": str(backend.state_home),
    }
    # exec only: the Linux wrapper does not contact the scheduler itself.
    code = "from automationctl.cli import entrypoint; entrypoint()"
    argv = [sys.executable, "-c", code, "_transient-exec", execution_id]
    processes = [
        subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    for process in processes:
        _, err = process.communicate(timeout=10)
        assert process.returncode == 0, err
    assert marker.read_text() == "once\n"


@pytest.mark.parametrize("during_stop", [False, True])
def test_supervised_workload_dies_with_wrapper(tmp_path: Path, during_stop: bool) -> None:
    import os
    import subprocess
    import sys
    import time

    marker = tmp_path / "pids"
    # Both workload generations ignore TERM; only group cleanup can stop them.
    child = (
        "import os,signal,time,pathlib; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getppid())+' '+str(os.getpid())); "
        "time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)"
    )
    source = (
        "from pathlib import Path; from automationctl.spec import TaskSpec; "
        "from automationctl.wrapper import ExecOptions,exec_task; "
        f"task=TaskSpec('demo',Path({str(tmp_path / 'demo.toml')!r}),'demo',"
        f"({sys.executable!r},'-c',{parent!r})); "
        f"exec_task(task,ExecOptions(Path({str(tmp_path / 'state')!r}),'host',"
        "supervise_workload=True,capture_versions=False))"
    )
    wrapper = subprocess.Popen([sys.executable, "-c", source])
    pids: list[int] = []
    try:
        until = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < until:
            time.sleep(0.05)
        pids = [int(value) for value in marker.read_text().split()]
        if during_stop:
            os.killpg(os.getpgid(pids[0]), signal.SIGTERM)
        wrapper.kill()
        wrapper.wait(timeout=5)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            states = [
                subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                for pid in pids
            ]
            if all(not state or state.startswith("Z") for state in states):
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"workload survived wrapper SIGKILL: {states}")
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.wait(timeout=5)
        for pid in pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("supervised", [False, True])
@pytest.mark.parametrize("workload_exit_code", [0, 7])
def test_cancellation_preserves_workload_exit_code(
    tmp_path: Path, supervised: bool, workload_exit_code: int
) -> None:
    import sys
    import time

    from automationctl.wrapper import exec_task

    ready = tmp_path / "ready"
    command = (
        "import pathlib,signal,sys,time; "
        f"signal.signal(signal.SIGTERM, lambda *_: sys.exit({workload_exit_code})); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(30)"
    )
    cancellation = threading.Event()
    opts = options(tmp_path / "state")
    opts.cancel_event = cancellation
    opts.supervise_workload = supervised
    opts.kill_grace = 2

    def cancel_when_ready() -> None:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancellation.set()

    canceller = threading.Thread(target=cancel_when_ready)
    canceller.start()
    try:
        result = exec_task(task(tmp_path, command=(sys.executable, "-c", command)), opts)
    finally:
        canceller.join(timeout=6)
    assert ready.exists(), "workload did not install its SIGTERM handler"
    assert result.status == "cancelled"
    assert result.exit_code == workload_exit_code
    meta = records.read_meta(result.run_dir)
    last = records.read_last(opts.state_dir, "demo")
    assert meta is not None and last is not None
    assert meta["status"] == last["status"] == "cancelled"
    assert meta["exit_code"] == last["exit_code"] == workload_exit_code
