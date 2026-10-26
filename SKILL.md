---
name: automationctl-skill
description: Install standalone tasks and submit, inspect, cancel, and reconcile transient automationctl executions.
---

# Use automationctl

Treat a task file as install input, not runtime configuration. Run
`automationctl lint FILE`, preview with `automationctl install FILE --dry-run
--diff`, then install with `automationctl install FILE`; an existing name
requires `--replace`. Each successful installation stores a complete schema-v2
definition at `$XDG_CONFIG_HOME/automationctl/tasks/<name>.toml` (default
`~/.config/automationctl/tasks/`). Do not edit generated scheduler files.

Use `automationctl add NAME --every 1h -- COMMAND...` for a basic task; it
captures the current absolute directory as `cwd`. Use `automationctl remove
NAME` to stop its trigger and workload, delete its definition and generated
artifacts, and retain historical run records. Deleting a source file does not
remove an installed task.

Run `automationctl run NAME` for foreground output, `submit NAME` to start it
through the scheduler, and `exec NAME` only for scheduler-facing execution.
`list`, `status`, `logs`, `doctor`, `catch-up`, `pause`, `resume`, and `prune`
all discover installed tasks from the XDG config directory. `status NAME` and
`logs NAME` can still read records after removal. `pause` and `resume` are
temporary schedule control; reinstalling a task reasserts its configured
enabled or disabled state.

Task `command` is direct argv. `stdin` is an inline string; `stdin_file` is
resolved relative to the source task file and copied into `stdin` at install
time. `cwd`, `env_files`, and `path_prepend` must be absolute or home-relative.
Task-local `[notify.<name>]` entries are referenced by `on_failure =
["notify:<name>"]`; credentials belong in machine-local env files. A task
uses `$HOME` as its cwd when no `cwd` is supplied, and the wrapper records
stdout, stderr, timeout, locks, final argv, and outcome under
`$XDG_STATE_HOME/automationctl`.

An optional `$XDG_CONFIG_HOME/automationctl/config.toml` contains only the
lint deny-list and shared catch-up sweep setting. `catchup_sweep` is a launchd-only
fallback and has no effect on systemd artifacts. The scheduler units propagate
the resolved standard XDG config/state homes. There is no manifest, runner,
host alias, current-directory configuration discovery, `--manifest`, `--host`,
or `uninstall` compatibility command.

## Transient commands

Use `actl submit --transient NAME [--after DURATION | --at OFFSET_TIMESTAMP] --
COMMAND...` for one-time execution without installing a reusable task. `--at`
must be future and timezone-aware. Omit timing options to run immediately.
Preview with `--dry-run`; no directories or scheduler jobs are created.
`--cwd`, `--timeout`, `--stdin`/`--stdin-file`, repeated `--env KEY=VALUE`,
`--env-file`, `--path-prepend`, and `--lock` configure the existing wrapper.

Save the returned `tx:<32 hex digits>` ID. Repeated display names are independent,
even when the name matches an installed task. Use `status ID`, `logs ID --both`,
and `cancel ID`; `remove NAME` applies only to installed tasks. `reconcile ID`
confirms collection, retries cleanup, or records a disappeared execution as
abandoned. It never retries the workload. On uncertain scheduler acceptance,
inspect the reported ID rather than submitting again. Workload result and
cleanup state are separate. Logs remain after registration cleanup; `prune`
retains terminal collected execution history per display name.

Transient registrations are boot/session scoped. On macOS each delayed job
has a sleeping launchd-owned wrapper until its absolute deadline, and GUI logout
ends the job. On Linux logout behavior follows the existing user manager;
reboot does not restore pending jobs. Native macOS validation is pending;
consult the repository's transient testing procedure before relying on it.
