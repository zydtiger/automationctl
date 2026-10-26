# Native transient validation

Unit tests use fake schedulers. The explicit smoke runner creates isolated,
uniquely named native jobs and disposable XDG directories; it never touches
installed tasks. Run from this checkout without installing a new binary:

```bash
uv sync --locked
uv run python scripts/smoke_transient.py --run
```

The runner checks immediate success and failure, retained exit status and logs,
timeout, relative and offset-aware absolute deadlines, pending and running
cancellation, and independent submissions sharing a display name. It verifies
that both systemd service/timer objects, or the launchd job, disappear **before**
calling reconciliation. Successful runs remove their temporary data; failures
cancel only their own jobs and print the retained evidence directory.

On macOS, first inspect `man launchd.plist` and `man launchctl`, record `sw_vers`,
and run from a logged-in GUI session with the `gui/<uid>` domain available.
Verify that final bootout terminates the wrapper only after the retained workload
result and logs are complete. Also check that no `job.plist` remains under the
printed execution directory and no transient plist appears in LaunchAgents.
The macOS runner also kills a running wrapper with SIGKILL, verifies that its
workload and descendant exit before reconciliation, then checks abandoned status
and removal of the private plist. Linux tests cannot certify launchd semantics.

For interactive sleep/wake validation, use a disposable XDG root and schedule a
job a few minutes ahead, sleep past the deadline, then wake. Confirm one run and
one output line, retained status, and no remaining native registration. For
logout/reboot, use the same isolated roots after logging in again: pending jobs
are not promised to survive manager teardown. `reconcile ID` must mark a lost
execution abandoned and must not execute it. Check Linux logout both with and
without an already configured lingering user manager; this test does not change
linger settings. Keep the roots and execution IDs until all records and native
objects have been inspected.

Cancellation tests should include a child that ignores SIGTERM, a running
command with descendants, and a wrapper forcibly terminated by the scheduler.
Confirm process cleanup as well as recorded outcome. Never use a real production
job to exercise failure injection or teardown.
