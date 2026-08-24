---
name: automationctl-skill
description: Operate the automationctl CLI to manage declarative scheduled automations — lint and install task manifests to systemd or launchd, run and submit tasks, inspect run records, and recover missed occurrences safely.
---

# Use automationctl

Configuration lives in a separate automations repository (`manifest.toml`, `tasks/*.toml`, `runners.toml`, `prompts/`), defaulting to `~/automations/manifest.toml`; override per command with `--manifest` or `AUTOMATIONCTL_MANIFEST`. The host key defaults to the short hostname; override with `--host`. That repository is the desired state: edit specs there and reinstall, and never edit the generated units under `~/.config/systemd/user` or `~/Library/LaunchAgents` — `install` reconciles and overwrites them by design.

Gate every change: run `automationctl lint`, preview with `automationctl install --dry-run --diff`, then `automationctl install`. Install refuses a repository that fails lint and garbage-collects units for tasks the manifest no longer selects. Run `automationctl doctor` for read-only host probes (backend, PATH resolution, env files, state directory) before the first install or when a task cannot start.

Use `run TASK` for a foreground, streaming debug run; `submit TASK` to start it now through the scheduler; `catch-up` to run missed persistent occurrences serially (idempotent — safe to invoke at any time). `exec` is the substrate entrypoint embedded in generated units, not an operator command. `pause TASK` and `resume TASK` are temporary and the next `install` restores the manifest's state; permanent disabling is `disabled = true` committed in the task spec.

Inspect with `list` (schedules and last outcomes), `status TASK` (recent runs), and `logs TASK` (captured output). Run records live under `$XDG_STATE_HOME/automationctl` (else `~/.local/state/automationctl`) on both platforms; bound retention with `prune --keep-runs N`.

Never put secrets in specs: notification URLs and tokens resolve from `env_files` at run time, and the manifest lint deny-list rejects dangerous argv unless the spec carries the explicit committed `allow_full_access = true` override. Installing this `SKILL.md` does not install the binary; install the CLI with `uv tool install git+https://github.com/zydtiger/automationctl`.
