---
name: automationctl-skill
description: Install, inspect, run, and remove standalone automationctl tasks managed through XDG configuration.
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
