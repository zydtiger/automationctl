# automationctl

Agent-neutral automation runner for personal machines. Declarative task specs
— agent CLI invocations (`claude`, `codex`, …) and arbitrary commands — are
compiled to the platform's native scheduler: systemd user units on Linux,
launchd LaunchAgents on macOS. There is no resident daemon; a short-lived
per-run wrapper owns environment construction, named locks, timeouts, run
records, and failure notifications.

Status: pre-M0 scaffold. See [docs/DESIGN.md](docs/DESIGN.md) for the full
design, including the architecture decisions and milestone plan.

## Development

```bash
uv sync
prek install        # one-time per clone: installs the Git hooks
uv run automationctl --version
uv run pytest
```

`uv` and `prek` are machine prerequisites.

## License

MIT
