# automationctl design contract

## Model

`automationctl` is a control plane and a short-lived per-run wrapper. It has
no resident process. A scheduler artifact invokes `automationctl exec TASK`,
which loads one installed task by name from the standard XDG configuration
location. Task source repositories are intentionally outside the runtime
boundary.

An installed task is `schema_version = 2` and has an explicit safe `name`,
description, and non-empty argv `command`. It owns its schedule, timeout,
jitter, persistence, paths, environment files, locks, summary command,
disable state, outcome notifications, and sandbox opt-in. `stdin` and
`stdin_file` are mutually exclusive; installation reads a source-relative
`stdin_file` and writes inline `stdin` into the installed definition. Unknown
fields and every schema other than v2 are rejected.

`$XDG_CONFIG_HOME/automationctl/config.toml` is optional machine policy. It is
schema v2 and contains only `[lint] forbidden_argv` and `catchup_sweep`. It
does not select tasks or supply defaults. `catchup_sweep` applies only to launchd;
systemd always renders its clock and timezone triggers and ignores the sweep. An absent
policy is an empty policy.

## Lifecycle

`install FILE` parses, materializes stdin, lints, and plans one named task.
It never scans or reconciles a source repository. Existing names require
`--replace`. Before any mutation it produces the complete plan; dry-run does
not create lock files, config directories, definitions, artifacts, or
scheduler changes. Actual mutation uses a process lock, private directories,
atomic task replacement, and target-scoped scheduler reconciliation.

A task transition from scheduled to manual removes only that task's old timer.
Adding, replacing, disabling, or removing A does not start, re-enable, or
delete B. The sole shared artifact is catch-up: it is computed from all valid
installed tasks, while malformed unrelated definitions do not block a
single-task run or installation. Removing the last qualifying task removes the
shared artifact. Scheduler rejection during deactivation preserves the old
definition and artifact. If reload/activation fails after desired files were
written, the command fails and retains desired state so the same `install
--replace` can retry.

`remove TASK` accepts a safe name even when its definition is missing or
corrupt, stops scheduler triggers and workload before deleting the known
artifact names, then removes the definition. It retains state history.

## Scheduler and wrapper

systemd and launchd artifacts contain only the schedule and `exec TASK` argv,
plus explicitly resolved `XDG_CONFIG_HOME` and `XDG_STATE_HOME`. They do not
store source paths or host aliases. Launchd activation tracks accepted content
per label and merges activation metadata so operating one task cannot discard
another label's retry state.

The wrapper creates private run records, takes an implicit per-task lock and
optional named lock, builds a minimal environment, resolves argv placeholders,
streams stdout/stderr, enforces timeout, runs summary extraction, records
program version, and sends task-local failure and success notifications; skipped
runs notify neither. Default cwd is
HOME. `{date}`, `{hostname}`, `{task}`, and `{run_dir}` expand strictly in argv
and leniently in stdin so JSON and code braces remain intact.

## Transient executions

`submit --transient NAME [--after DURATION | --at OFFSET_TIMESTAMP] -- COMMAND...`
creates a private execution snapshot, not an installed definition. NAME is a
reusable display name; `tx:<32 lowercase hexadecimal digits>` identifies one
execution. IDs occupy a separate namespace from installed names. `submit NAME`
retains its installed-task meaning. Transient command options include cwd,
timeout, stdin/stdin-file, env/env-file, path-prepend and a named lock. They use
the existing schema, materialization and lint policy. Dry-run performs no writes
or scheduler calls.

Both deadline options resolve to a single absolute wall-clock instant. `--at`
requires a future ISO 8601 timestamp with an explicit UTC offset. Submission
prints the execution ID and offset-aware trigger time only after acceptance.
A scheduler error retains the ID and acceptance evidence; it is never retried
automatically because the scheduler may already have accepted the command.

Execution snapshots and records live below the private state root's
`transient/<id-without-prefix>/`. A short state lock serializes transitions;
a durable claim is written before entering the existing wrapper. Repeated
delivery cannot rerun a claimed execution, including after a crash. A crash
between claiming and starting may lose an invocation; this is not an
exactly-once side-effect guarantee. Workload outcome, scheduler acceptance,
and cleanup outcome are separate fields. Existing installed-task locks and
records remain separate. Explicit named locks are shared with installed tasks.

`list` includes executions. `status ID` and `logs ID` read retained records.
`cancel ID` stops a pending trigger and any running workload, retaining history;
`remove NAME` continues to operate only on installed tasks. `reconcile ID`
checks a single scheduler registration, records an abandoned execution when it
has disappeared, and retries cleanup of terminal executions. Neither command
restarts a workload. Query commands are read-only. `prune` retains the newest 50 runs per installed task (`--keep-runs N`) and
seven days of transient history (`--transient-older-than DURATION`). Transient
age uses `finished_at`, never creation time or per-name counts. Only terminal,
collected executions with valid offset-aware completion times are removed, as
entire execution directories. `0s` removes all eligible completed executions.
`--only all|scheduled|transient` selects the history scope. `--dry-run` lists
eligible paths and file sizes without writes, backend calls or reconciliation;
actual pruning reconciles terminal registrations first. Active or unresolved
executions are never pruned.

### systemd transient lifecycle

Use a unique `actl-tx-<hex>.service` via `systemd-run --user --collect` with
`Type=exec`, and a one-time UTC calendar timer when delayed. Timers use
`RemainAfterElapse=false` and `CollectMode=inactive-or-failed`; no persistent
unit files are written. Calendar timers catch up after sleep without waking
the host. A relative timer for the same delay covers a deadline that passes during
registration; the wrapper rechecks the wall clock before executing. The durable claim prevents duplicate delivery.

The wrapper completes logs, result and notifications before exiting.
`ExecStopPost` records abrupt termination and requests timer removal; systemd
collects the service after the post-stop process exits. Cleanup is recorded as
`delegated` until an explicit reconciliation observes both units absent.
Cancellation stops the timer before the service, using systemd's cgroup
termination with a bounded grace period. The wrapper handles SIGTERM and
records cancellation; the post-stop hook covers SIGKILL and startup failure.

Registrations last only for the current user-manager lifetime. Logout survives
only when the user manager remains alive (for example, linger is enabled).
Reboot or manager shutdown loses pending registrations. Reconciliation marks
those records abandoned and never recreates them. Closing the submitting
terminal does not affect an accepted execution.

### launchd transient lifecycle

A private plist is bootstrapped into the backend's existing `gui/<uid>` domain.
It uses RunAtLoad and LaunchOnlyOnce, never `kickstart -k`, calendar recurrence,
or login-persistent LaunchAgents. An unavailable domain is an error, not proof
that an execution was collected. Each delayed job has one launchd-owned wrapper
waiting on its absolute deadline with bounded sleeps; it checks wall-clock time
after wake. This costs one sleeping process per pending execution but avoids
local calendar ambiguity, repeated annual matches and timezone-change races.
Logout ends this GUI-domain lifetime; pending work is not restored on login.

After completing the workload, draining logs, finishing notifications and
atomically persisting its terminal outcome, the wrapper unlinks only its own
private plist and durably records cleanup delegation. Its final operation is
`launchctl bootout` of its own unique target. This may terminate the wrapper;
no result writing or notification is required after that call, and its scheduler
exit status is not the workload result. There is no resident daemon. While a workload runs, a per-execution supervisor
owns its process group and watches a pipe held only by the wrapper. Wrapper
death closes the pipe and the supervisor kills its group, including descendants.
It remains alive after command exit until group cleanup, preserving this guarantee
during result delivery. The workload exit code is returned separately to the wrapper. A failed bootout retains cleanup failure separately from the
workload result. Abrupt death can leave a temporary plist or registration;
`reconcile ID` inspects and removes that execution's stale artifacts.

`scripts/smoke_transient.py --run` checks
immediate success/failure, timeout, pending/running cancellation, absolute and
relative deadlines, retained logs, and automatic scheduler collection before
reconciliation, plus workload and descendant cleanup after wrapper SIGKILL.
Run it on the target macOS version and inspect its installed
`launchd.plist(5)` and `launchctl(1)` manual pages before accepting this backend.
Sleep/wake and logout require separate interactive tests. Linux mocks validate
argv and state ordering only, not launchd's native unload or process behavior.
