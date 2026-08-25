# automationctl

Agent-neutral automation runner for personal machines. Declarative task specs
— agent CLI invocations (`claude`, `codex`, …) and arbitrary commands — are
compiled to the platform's native scheduler: systemd user units on Linux,
launchd LaunchAgents on macOS. There is no resident daemon; a short-lived
per-run wrapper owns environment construction, named locks, timeouts, run
records, and failure notifications.

The tool contains zero agent-specific code. A runner is a table of argv in
your own configuration repository, so a new agent CLI — or a changed flag — is
a configuration edit, never a release.

Status: milestones M0–M3 implemented (models, lint, wrapper lifecycle, systemd
and launchd backends, catch-up, full CLI). See
[docs/DESIGN.md](docs/DESIGN.md) for the design contract, the architecture
decisions, and the milestone plan.

## Quickstart

Task specs live in a separate configuration directory. Copy
[`examples/`](examples/) to get a working shape.

```bash
uv tool install git+https://github.com/zydtiger/automationctl
git clone <your-remote>/automations automations
cd automations

automationctl doctor                 # read-only host probes: PATH, env, backend
automationctl lint                   # schema, references, and policy
automationctl install --dry-run --diff
automationctl install
```

Day to day:

```bash
automationctl list                   # tasks, schedules, last outcome
automationctl run <task>             # foreground, streaming, for debugging
automationctl submit <task>          # background now, through the scheduler
automationctl status <task>          # recent runs, exit codes, durations
automationctl logs <task>            # the last run's captured output
automationctl pause <task>           # temporary; the next install restores it
automationctl prune --keep-runs 50   # run-record retention
```

The manifest defaults to `./manifest.toml` in the current working directory.
Override it per command with `--manifest` or with `AUTOMATIONCTL_MANIFEST`.
The host key defaults to the short hostname and can be overridden with
`--host`. Run records live under `$XDG_STATE_HOME/automationctl` (else
`~/.local/state/automationctl`) on both platforms.

## Development

```bash
uv sync
prek install        # one-time per clone: installs the Git hooks
uv run automationctl --version
uv run pytest
```

`uv` and `prek` are machine prerequisites.

Tests are hermetic: rendering is checked against golden units and plists, the
wrapper is exercised against stock POSIX tools, and every `systemctl` or
`launchctl` invocation goes through a command-runner seam that tests replace
with a recorder. No test touches a real scheduler, the user's state directory,
or the network.

## License

MIT
