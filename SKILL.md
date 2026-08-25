---
name: automationctl-skill
description: Operate the automationctl CLI to manage declarative scheduled automations — lint and install task manifests to systemd or launchd, run and submit tasks, inspect run records, and recover missed occurrences safely.
---

# Use automationctl

Select the configuration manifest with `--manifest` or `AUTOMATIONCTL_MANIFEST` when needed. The host key defaults to the short hostname; override it with `--host`. Treat the selected configuration as desired state: edit its specs and reinstall, and never edit generated scheduler units — `install` reconciles and overwrites them by design.

Gate every change: run `automationctl lint`, preview with `automationctl install --dry-run --diff`, then `automationctl install`. Install refuses configuration that fails lint and garbage-collects units for tasks the manifest no longer selects. Run `automationctl doctor` for read-only host probes (backend, PATH resolution, env files, state directory) before the first install or when a task cannot start.

Use `run TASK` for a foreground, streaming debug run; `submit TASK` to start it now through the scheduler; `catch-up` to run missed persistent occurrences serially (idempotent — safe to invoke at any time). `install` also generates the triggers that invoke `catch-up` automatically when the host selects a persistent calendar task: on Linux after a boot, a timezone change, or a clock step (systemd 242+); on macOS at load and on timezone changes, where clock steps are covered only by the opt-in `[defaults] catchup_sweep = "<duration>"` interval in the manifest. A manual sweep is therefore a diagnostic rather than routine maintenance; `doctor` reports whether those triggers are installed, current, and supported by the running scheduler. Overlapping triggers are safe: every run holds an implicit per-task lock, so a duplicate records `skipped` instead of running twice. `exec` is the substrate entrypoint embedded in generated units, not an operator command. `pause TASK` and `resume TASK` are temporary and the next `install` restores the manifest's state; for permanent disabling, set `disabled = true` in the task spec.

Inspect with `list` (schedules and last outcomes), `status TASK` (recent runs), and `logs TASK` (captured output). Run records live in automationctl's state directory, which `doctor` reports; bound retention with `prune --keep-runs N`.

Never put secrets in specs: notification URLs and tokens resolve from `env_files` at run time, and the manifest lint deny-list rejects dangerous argv unless the spec carries the explicit `allow_full_access = true` override.
