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
disable state, failure notifications, and sandbox opt-in. `stdin` and
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
program version, and sends task-local failure notifications. Default cwd is
HOME. `{date}`, `{hostname}`, `{task}`, and `{run_dir}` expand strictly in argv
and leniently in stdin so JSON and code braces remain intact.
