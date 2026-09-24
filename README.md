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

## Development

```bash
uv sync
uv run pytest
prek run --all-files
prek run --all-files --hook-stage pre-push
```

The test suite uses recording scheduler commands; it does not access the real
scheduler, network, or user configuration.
