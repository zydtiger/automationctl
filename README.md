# automationctl

`automationctl` installs one self-contained task at a time into
`$XDG_CONFIG_HOME/automationctl/tasks/` (normally `~/.config/automationctl/tasks/`).
It renders a systemd user unit on Linux or a launchd LaunchAgent on macOS and
executes each run through a short-lived wrapper with records, locks, timeouts,
and optional task-local notifications.

## Quick start

```toml
# hourly-check.toml
schema_version = 2
name = "hourly-check"
description = "Record a simple health check"
command = ["/usr/bin/true"]
schedule = "every 1h"
timeout = "2m"
```

```bash
automationctl lint hourly-check.toml
automationctl install hourly-check.toml --dry-run --diff
automationctl install hourly-check.toml
automationctl status hourly-check
```

Use `install FILE --replace` to update an installed name, `remove TASK` to
delete its definition and scheduler artifacts, and `add NAME --every 1h --
COMMAND...` to create a simple task from the command line. `uninstall`,
`--manifest`, `--host`, and `AUTOMATIONCTL_MANIFEST` are not supported.

Task files are copied into the XDG config directory. A source file, its
working directory, or an external `stdin_file` may subsequently move without
changing the installed definition; `stdin_file` is materialized as `stdin`
during installation. By default a file task runs in `$HOME`; set `cwd`
explicitly when it needs another directory. Command values are argv, never a
shell command; use `sh -c` explicitly when a shell is intended.

`config.toml` in the same XDG directory is optional machine policy. It may
contain the lint deny-list and `catchup_sweep`. The sweep is a launchd-only
fallback; systemd ignores it because its clock and timezone triggers are always
rendered. The policy does not provide task
defaults, runners, host selection, notifications, or environment values.
Generated scheduler artifacts receive the resolved `XDG_CONFIG_HOME` and
`XDG_STATE_HOME`, so interactive and scheduled invocations load the same
installed task set. Run records remain under `$XDG_STATE_HOME/automationctl`
or `~/.local/state/automationctl`.

## Transient execution

Submit a one-time command without installing a reusable task:

```bash
actl submit --transient demo -- /usr/bin/printf 'hello\n'
actl submit --transient demo --after 10m -- /usr/bin/true
actl submit --transient demo --at '2030-01-02T09:00:00-05:00' -- /usr/bin/true
actl submit --transient demo --dry-run --timeout 2m -- /usr/bin/true
```

`--at` requires a future timestamp with a timezone offset; it and `--after`
are mutually exclusive. Submission returns an execution ID such as
`tx:0123456789abcdef0123456789abcdef` after scheduler acceptance and displays
the intended trigger time. Reusing `demo`, including an installed task's name,
creates independent executions. `submit demo` still starts the installed task.

Use `--cwd`, `--timeout`, `--stdin` or `--stdin-file`, repeated `--env KEY=VALUE`,
`--env-file`, `--path-prepend`, and `--lock` to configure the existing wrapper.
The current directory becomes cwd; stdin files are copied into the execution
snapshot. Arguments following `--` are direct argv. Dry-run neither writes
files nor contacts a scheduler.

```bash
actl list
actl status tx:0123456789abcdef0123456789abcdef
actl logs tx:0123456789abcdef0123456789abcdef --both
actl cancel tx:0123456789abcdef0123456789abcdef
actl reconcile tx:0123456789abcdef0123456789abcdef
```

`cancel` stops one transient execution and retains its history. `remove NAME`
continues to remove installed tasks. `reconcile ID` retries cleanup or marks
an execution abandoned when its scheduler registration disappeared; it never
restarts work. Acceptance errors include the ID for inspection and cancellation;
do not automatically resubmit an ambiguously accepted command.

Snapshots, outcome and logs stay under the private XDG state directory, separate
from installed definitions. Workload result and cleanup result are independent;
`cleanup=delegated` means final collection belongs to the scheduler, and
reconciliation confirms `collected`. `prune` retains the newest 50 runs per installed task and seven days of
collected terminal transient history, measured from completion rather than creation.
Transient retention does not group by display name. Override these independently:

```sh
actl prune --keep-runs 20 --transient-older-than 3d
actl prune --only transient --transient-older-than 7d
actl prune --only transient --transient-older-than 0s
actl prune --only scheduled --keep-runs 20
actl prune --dry-run
```

`--only` accepts `all` (default), `scheduled` (installed-task history, including
manual runs), or `transient`. Transient pruning removes the entire execution
directory, including its snapshot and logs, only after terminal status and
confirmed collection. Missing or invalid completion times are retained.
`--dry-run` lists candidate paths and file sizes without writes or scheduler
calls; it does not reconcile, so unresolved collection is excluded from preview.
Actual pruning first retries terminal cleanup. Project workload outputs outside
ACTL history are unaffected.

Linux uses collected systemd user services and one-time timers. macOS uses
private launchd jobs; delayed executions each hold one sleeping wrapper until
the wall-clock deadline. Deadlines catch up after sleep without waking the host.
Registrations do not survive reboot or user-manager/GUI-domain teardown; Linux
logout survival follows the user manager's existing linger configuration.
Native smoke tests passed on macOS 26.6.2, including wrapper SIGKILL and
workload/descendant cleanup. Sleep/wake, logout, and reboot still require
interactive validation; see [the native test procedure](docs/TRANSIENT_TESTING.md).

## Development

```bash
uv sync --locked
uv tool install prek  # once per machine
prek install          # once per clone
uv run automationctl --version
```

Run the full, targeted, or documentation-only validation defined in
`AGENTS.md`; those commands execute the same hook stages as CI. `uv` and `prek`
are machine prerequisites.

Tests are hermetic: rendering is checked against golden units and plists, the
wrapper is exercised against stock POSIX tools, and every `systemctl` or
`launchctl` invocation goes through a command-runner seam that tests replace
with a recorder. No test touches a real scheduler, the user's state directory,
or the network.

## License

MIT
