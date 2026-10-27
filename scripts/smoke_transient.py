"""Explicit native scheduler smoke test: python scripts/smoke_transient.py --run.

Creates only unique transient jobs and a disposable XDG state/config root.
Keeps evidence on failure. Never operates on installed tasks or shared jobs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize isolated native smoke jobs")
    parser.add_argument("--executable", default=shutil.which("automationctl"))
    args = parser.parse_args()
    if not args.run or not args.executable:
        parser.error("pass --run and make automationctl available (or supply --executable)")
    root = Path(tempfile.mkdtemp(prefix="actl-transient-smoke-"))
    env = dict(
        os.environ,
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_STATE_HOME=str(root / "state"),
        AUTOMATIONCTL_EXECUTABLE=str(Path(args.executable).resolve()),
    )
    env.pop("AUTOMATIONCTL_BACKEND", None)
    env.pop("AUTOMATIONCTL_UNIT_DIR", None)
    ids: list[str] = []
    workload_pids: list[int] = []

    def cli(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [args.executable, *argv],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=check,
        )

    def submit(*options: str, command: tuple[str, ...] = ("/bin/echo", "smoke")) -> str:
        result = cli("submit", "--transient", "smoke", *options, "--", *command)
        match = re.search(r"tx:[0-9a-f]{32}", result.stdout)
        assert match, result
        ids.append(match[0])
        return match[0]

    def state(execution_id: str) -> dict[str, object]:
        return json.loads(cli("status", execution_id).stdout)  # type: ignore[no-any-return]

    def wait(execution_id: str, expected: str) -> None:
        until = time.monotonic() + 25
        while time.monotonic() < until:
            data = state(execution_id)
            if data["status"] == expected and data["cleanup"] in {"delegated", "collected"}:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(data)
        # Verify automatic collection BEFORE asking reconcile to repair anything.
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if sys.platform == "darwin":
                probe = subprocess.run(
                    [
                        "launchctl",
                        "print",
                        f"gui/{os.getuid()}/automationctl.transient.{execution_id[3:]}",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                absent = probe.returncode == 113
            else:
                absent = True
                for suffix in ("service", "timer"):
                    probe = subprocess.run(
                        [
                            "systemctl",
                            "--user",
                            "show",
                            f"actl-tx-{execution_id[3:]}.{suffix}",
                            "--property=LoadState",
                            "--value",
                        ],
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    absent &= probe.stdout.strip() == "not-found"
            if absent:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"scheduler registration remains: {execution_id}")
        cli("reconcile", execution_id)
        assert state(execution_id)["cleanup"] == "collected"
        print(f"PASS {execution_id} {expected}", flush=True)

    try:
        successful = submit()
        failed = submit(command=("/bin/sh", "-c", "echo failure >&2; exit 7"))
        timed = submit("--timeout", "1s", command=("/bin/sleep", "30"))
        delayed = submit("--after", "2s")
        absolute = submit("--at", (datetime.now(UTC) + timedelta(seconds=3)).isoformat())
        pending = submit("--after", "5m")
        cli("cancel", pending)
        running = submit(command=("/bin/sleep", "30"))
        until = time.monotonic() + 10
        while state(running)["status"] != "running" and time.monotonic() < until:
            time.sleep(0.1)
        cli("cancel", running)
        for execution_id, expected in [
            (successful, "ok"),
            (failed, "failed"),
            (timed, "timeout"),
            (delayed, "ok"),
            (absolute, "ok"),
            (pending, "cancelled"),
            (running, "cancelled"),
        ]:
            wait(execution_id, expected)
        if sys.platform == "darwin":
            marker = root / "workload-pids.json"
            child = (
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
            )
            parent = (
                "import os,subprocess,sys,time,json,pathlib; "
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid])); "
                "time.sleep(120)"
            )
            killed = submit(command=(sys.executable, "-c", parent))
            until = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.1)
            workload_pids = json.loads(marker.read_text())
            subprocess.run(
                [
                    "launchctl",
                    "kill",
                    "SIGKILL",
                    f"gui/{os.getuid()}/automationctl.transient.{killed[3:]}",
                ],
                check=True,
                timeout=5,
            )
            until = time.monotonic() + 5
            while time.monotonic() < until:
                states = [
                    subprocess.run(
                        ["ps", "-o", "stat=", "-p", str(pid)],
                        text=True,
                        capture_output=True,
                        timeout=5,
                    ).stdout.strip()
                    for pid in workload_pids
                ]
                if all(not value or value.startswith("Z") for value in states):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f"workload survived wrapper SIGKILL: {states}")
            cli("reconcile", killed)
            assert state(killed)["status"] == "abandoned"
            assert state(killed)["cleanup"] == "collected"
            assert not (
                root / "state" / "automationctl" / "transient" / killed[3:] / "job.plist"
            ).exists()
            print(f"PASS {killed} wrapper SIGKILL cleaned workload and descendant", flush=True)
        assert "smoke" in cli("logs", successful).stdout
        assert "failure" in cli("logs", failed).stdout
        assert state(failed)["exit_code"] == 7
        assert not (root / "config").exists()
    except BaseException:
        for execution_id in ids:
            cli("cancel", execution_id, check=False)
        for pid in workload_pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        print(f"Evidence retained at {root}", file=sys.stderr)
        raise
    else:
        shutil.rmtree(root)
        print("All native transient smoke checks passed")


if __name__ == "__main__":
    main()
