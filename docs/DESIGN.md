# automationctl — Design

Status: approved design. This document is the authoritative plan for the
`automationctl` tool and its companion private `automations` configuration
repository. Examples are generic; real specs live in the private repository.

---

## 1. Summary

`automationctl` is an agent-neutral automation runner for personal machines.
It executes scheduled or on-demand tasks — agent CLI invocations (`claude`,
`codex`, `pi`, …) and arbitrary shell commands — without a custom daemon, by
compiling declarative task specs into the platform's native scheduler:

- Linux → systemd user units (`.service` + `.timer`)
- macOS → launchd user LaunchAgents (`.plist`)

The tool is a **control plane plus a short-lived per-run wrapper**. No
`automationctl` process is ever resident; persistence, scheduling, and
supervision are delegated to the init system, which is already supervising
everything else on the machine.

### Core decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | No custom daemon | systemd/launchd already provide scheduling, persistence, missed-run handling, logging, resource control, and singleton semantics. A daemon would reimplement them behind a new failure surface, and on macOS would itself need launchd to stay alive. |
| D2 | No agent adapters as code | The universal contract is a process: `argv + env + cwd + stdin + exit code`. Agent headless modes are just command lines. The tool ships zero agent-specific code. |
| D3 | Runner templates are data, not code | DRY across tasks comes from user-defined argv expansion tables in the private config repo. `command = [...]` verbatim is always available and is the ground truth. |
| D4 | Thick wrapper, dumb substrate | Everything platform-divergent (env loading, locks, timeout, logging, catch-up, notify) lives in the cross-platform `exec` wrapper. Units and plists only start the wrapper. |
| D5 | Two repositories | `automationctl` (code, public-bound) + a private `automations` configuration repository — the same public-renderer / private-manifest split as `agent-bootstrap`. |
| D6 | Python + uv | Same stack and distribution as `agent-bootstrap` (`uv tool install`). The work is subprocess orchestration, TOML, and template rendering. Portable locks via `fcntl` in-process. |
| D7 | Manifest owns host selection | Task specs do not carry `hosts = [...]`. The manifest's `hosts.<name>.tasks` list is the single selection authority. |
| D8 | Generated output is never hand-edited | Units and plists are compiled artifacts, reconciled by `install`. |

### Non-goals

- Cross-machine orchestration, queues, or leader election. (Escape hatches: a
  task's command can be `ssh`; `pueue` can become a backend if queueing is
  ever needed.)
- A web UI or API server. Observability is CLI + files + notifications.
- Windows support.
- Interactive/attended agent sessions. Tasks are headless by definition.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────┐
│ automations repo (private, git)                            │
│   manifest.toml · runners.toml · tasks/*.toml · prompts/   │  desired state
├────────────────────────────────────────────────────────────┤
│ automationctl CLI (public tool)                            │
│   install: validate → lint → render → reconcile → enable   │  control plane
│   exec:    env → lock → spawn → record → notify            │  per-run wrapper
├────────────────────────────────────────────────────────────┤
│ Scheduler backend (per host)                               │
│   systemd user units (Linux) │ launchd LaunchAgents (mac)  │  dumb substrate
├────────────────────────────────────────────────────────────┤
│ ~/.local/state/automationctl/                              │
│   runs/ · last/ · locks/                                   │  observed state
└────────────────────────────────────────────────────────────┘
```

### Four-tier content boundary

| Tier | Lives in | Examples |
|------|----------|----------|
| Code | `automationctl` (public git) | CLI, backends, schema validation, lint engine |
| Config | `automations` (private git) | task specs, runner templates, host manifest, prompts, lint deny-list |
| Secrets | machine-local env files, never git | proxy variables, API keys, notification tokens |
| State | `~/.local/state/automationctl/`, never git | run records, logs, lock files, last-run pointers |

The litmus test for the code/config boundary: a stranger clones
`automationctl`, points it at their own `automations` repo, and everything
works. Any personal default baked into the tool is a boundary violation.

State-dir location is uniform on both platforms (`$XDG_STATE_HOME` else
`~/.local/state`): operational uniformity beats macOS platform purism for a
personal tool.

---

## 3. Repository layouts

### 3.1 `automationctl` (this repository)

```
automationctl/
├── AGENTS.md                    # repo conventions
├── README.md                    # install, quickstart, generic examples
├── LICENSE                      # MIT
├── pyproject.toml               # console scripts: automationctl + actl (alias)
├── uv.lock
├── src/automationctl/
│   ├── __init__.py
│   ├── cli.py                   # command surface (typer)
│   ├── config.py                # manifest/spec discovery and loading
│   ├── spec.py                  # task + manifest models, schema_version checks
│   ├── template.py              # runner expansion, {placeholder} substitution
│   ├── lint.py                  # policy engine (deny-list driven)
│   ├── wrapper.py               # `exec` lifecycle (see §7)
│   ├── records.py               # run records, last-run pointers, prune
│   ├── locks.py                 # fcntl named locks
│   ├── notify.py                # transports: ntfy, command hook
│   ├── schedule.py              # neutral grammar → per-backend forms
│   ├── catchup.py               # missed-run decisions from schedule + records
│   ├── doctor.py                # read-only host probes
│   ├── commands.py              # the scheduler-command seam (see §11)
│   ├── paths.py                 # state dir, unit dir, manifest resolution
│   ├── errors.py                # shared exception types
│   └── backends/
│       ├── __init__.py          # interface, reconciliation, backend factory
│       ├── systemd.py           # render units, systemctl verbs, reconcile
│       └── launchd.py           # render plists, launchctl verbs
├── tests/                       # golden-file rendering tests, wrapper tests
└── examples/                    # a complete generic automations layout
```

Because the tool contains zero agent-specific code, CI needs no agent CLIs:
tests exercise rendering, linting, and the wrapper against `sleep`/`false`.

### 3.2 `automations` (private companion, illustrative)

```
automations/
├── AGENTS.md                    # conventions; explicit ban on secrets
├── manifest.toml                # schema_version, hosts, defaults, lint, notify
├── runners.toml                 # data-only argv expansion tables
├── tasks/
│   ├── canary.toml
│   ├── nightly-repo-audit.toml
│   ├── morning-brief.toml
│   ├── weekly-benchmark.toml
│   └── mirror-notes.toml
└── prompts/
    ├── nightly-repo-audit.md
    └── morning-brief.md
```

One repo, every host clones it; `hosts.*` sections express divergence.
Never branch per host.

### 3.3 Machine-local (generated / mutable, never git)

```
~/.local/state/automationctl/
├── runs/<task>/<UTC-timestamp>-<shortid>/
│   ├── meta.json                # spec snapshot, final argv, timing, exit, versions
│   ├── stdout.log
│   ├── stderr.log
│   └── result.json              # summary_cmd output, when configured
├── last/<task>.json             # last outcome pointer (status, catch-up)
├── activated/<backend>.json     # content hash the scheduler last accepted (§11.14)
└── locks/<name>.lock

~/.config/systemd/user/automationctl-*.{service,timer}     # Linux, generated
~/Library/LaunchAgents/automationctl.*.plist               # macOS, generated
```

---

## 4. Configuration reference

### 4.1 `manifest.toml`

```toml
schema_version = 1

[defaults]
timeout = "30m"
on_failure = ["notify:ntfy"]
randomized_delay = "5m"

[hosts.workstation]
tasks = [
  "canary",
  "nightly-repo-audit",
  "weekly-benchmark",
  "mirror-notes",
]
path_prepend = ["~/.local/bin"]
env_files = ["~/.config/agent-env"]    # secrets stay machine-local, never git

[hosts.laptop]
tasks = ["canary", "morning-brief"]
path_prepend = ["~/.local/bin", "/opt/homebrew/bin"]
env_files = ["~/.config/agent-env"]

[notify.ntfy]
url_env = "NTFY_URL"                   # actual URL+topic resolved from env

[lint]
# Deny-list is config, not tool code: the tool ships the mechanism,
# the private repo decides what "dangerous" means.
forbidden_argv = [
  "--dangerously-skip-permissions",
  "--sandbox=danger-full-access",
  "danger-full-access",
]
```

### 4.2 `runners.toml` — data-only expansion tables

```toml
schema_version = 1

# The tool knows nothing about these programs. {prompt} substitutes the
# task's prompt; stdin = "prompt" delivers it on stdin instead.
# Flag drift lands here as a config edit, never as a code release.

[runners.claude-ro]
argv = ["claude", "-p", "--output-format", "json", "--permission-mode", "plan"]
stdin = "prompt"

[runners.claude-edit]
argv = ["claude", "-p", "--output-format", "json", "--permission-mode", "acceptEdits"]
stdin = "prompt"

[runners.codex-ro]
argv = ["codex", "exec", "--json", "--sandbox", "read-only", "{prompt}"]

[runners.pi]
argv = ["pi", "-p", "{prompt}"]

[runners.claude-yolo]
argv = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
stdin = "prompt"
allow_full_access = true     # explicit marker; without it, lint rejects
```

Flag names above are illustrative; verify against installed CLI versions.
That verification cost is precisely why templates are config, not code.

### 4.3 Task specs

Plain shell task:

```toml
# tasks/mirror-notes.toml
description = "Mirror the notes directory into local backups"

command = ["rsync", "-a", "--delete", "~/notes/", "~/backups/notes/"]

schedule = "daily 04:30"
timeout = "10m"
```

Agent task via runner template, long prompt in a file:

```toml
# tasks/nightly-repo-audit.toml
description = "Nightly audit of open PRs and stale branches"

runner = "claude-ro"
prompt_file = "prompts/nightly-repo-audit.md"
cwd = "~/projects/acme"

schedule = "daily 03:00"
timeout = "45m"
lock = "agents"
summary_cmd = ["jq", "-r", ".result"]
on_failure = ["notify:ntfy"]
```

Canary — the cheapest possible agent round trip, because the most common
silent death of unattended agent systems is expired auth or broken proxy:

```toml
# tasks/canary.toml
description = "Detect broken agent auth/proxy before real tasks hit it"

runner = "claude-ro"
prompt = "Reply with exactly: ok"

schedule = "daily 08:00"
timeout = "5m"
```

GPU-bound task with a named lock (mutual exclusion against other `gpu` tasks):

```toml
# tasks/weekly-benchmark.toml
description = "Weekly one-epoch training benchmark"

command = ["uv", "run", "train.py", "--epochs", "1"]
cwd = "~/projects/train-bench"

schedule = "weekly sun 05:00"
timeout = "4h"
lock = "gpu"
```

### 4.4 Task spec field reference

| Field | Type | Notes |
|-------|------|-------|
| `description` | str | required |
| `command` | [str] | verbatim argv; mutually exclusive with `runner` |
| `runner` | str | key into `runners.toml`; requires `prompt` or `prompt_file` |
| `prompt` / `prompt_file` | str | inline text, or path relative to the automations repo |
| `cwd` | str | tilde-expanded; existence checked by `doctor` |
| `env` | table | inline non-secret vars, e.g. `env = { FOO = "bar" }` |
| `env_files` | [str] | extends host-level list |
| `schedule` | str | neutral grammar (§4.5); omit for manual-only tasks |
| `timeout` | str | wrapper-enforced; unit backstop = timeout + 5m |
| `randomized_delay` | str | jitter; systemd `RandomizedDelaySec`, mac wrapper sleep |
| `persistent` | bool | default true for calendar schedules; run missed occurrences |
| `lock` | str | named mutex; contended run exits as `skipped`, not failure |
| `summary_cmd` | [str] | post-run argv, receives stdout, produces `result.json` |
| `on_failure` | [str] | notify transports; overrides defaults |
| `allow_full_access` | bool | lint override marker |
| `disabled` | bool | keep spec, render nothing |

Built-in placeholders available in argv and prompts: `{date}`, `{hostname}`,
`{task}`, `{run_dir}`. Deliberately minimal.

### 4.5 Schedule grammar

Neutral grammar limited to what both backends express natively:

| Spec | systemd | launchd |
|------|---------|---------|
| `daily 03:00` | `OnCalendar=*-*-* 03:00:00` | `StartCalendarInterval {Hour 3, Minute 0}` |
| `weekly sun 05:00` | `OnCalendar=Sun *-*-* 05:00:00` | `{Weekday 0, Hour 5, Minute 0}` |
| `monthly 1 09:00` | `OnCalendar=*-*-01 09:00:00` | `{Day 1, Hour 9, Minute 0}` |
| `every 15m` | `OnUnitActiveSec=15m` (+`OnBootSec`) | `StartInterval 900` |

Times are **local wall clock** on both platforms, matching what `OnCalendar`
and `StartCalendarInterval` actually do; see §11.9.

Escape hatch for exotic needs, per backend:

```toml
[schedule]
systemd = "Mon..Fri *-*-* 09..17:00:00"
launchd = [{ Weekday = 1, Hour = 9, Minute = 0 }]
```

---

## 5. Generated artifacts (illustrative)

```ini
# ~/.config/systemd/user/automationctl-nightly-repo-audit.service  (GENERATED)
[Unit]
Description=automationctl: nightly-repo-audit

[Service]
Type=oneshot
ExecStart=/home/user/.local/bin/automationctl exec --manifest /home/user/automations/manifest.toml nightly-repo-audit
RuntimeMaxSec=3000                           # 45m task timeout + 5m backstop
```

```ini
# ~/.config/systemd/user/automationctl-nightly-repo-audit.timer  (GENERATED)
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

```xml
<!-- ~/Library/LaunchAgents/automationctl.morning-brief.plist  (GENERATED) -->
<key>Label</key><string>automationctl.morning-brief</string>
<key>ProgramArguments</key>
<array>
  <string>/Users/user/.local/bin/automationctl</string>
  <string>exec</string>
  <string>--manifest</string><string>/Users/user/automations/manifest.toml</string>
  <string>morning-brief</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
```

macOS additionally gets one `automationctl.catchup` agent with
`RunAtLoad=true`: it compares each persistent task's schedule against
`last/<task>.json` and runs anything missed while the machine was off —
launchd coalesces runs missed during sleep, but not across power-off, so
catch-up lives in the wrapper layer where both platforms behave identically.

Units and plists are reconciled by filename prefix (`automationctl-` /
`automationctl.`): `install` adds, updates, and garbage-collects stale ones.
Hand edits are overwritten by design.

---

## 6. CLI reference and interaction

### 6.1 One-time host setup

```
$ uv tool install automationctl        # or: uv tool install git+<repo-url>
$ git clone <private-remote>/automations ~/automations
$ automationctl doctor
✓ backend: systemd user manager (running, 0 failed units)
✓ linger: enabled for current user
✓ manifest: ~/automations/manifest.toml (schema 1, host workstation, 4 tasks)
✓ env files: ~/.config/agent-env readable
✓ binaries: claude ✓  codex ✓  jq ✓  rsync ✓   (with configured path_prepend)
✗ state dir: created ~/.local/state/automationctl
$ automationctl install --dry-run --diff
$ automationctl install
```

`doctor` exists because the two predictable first-day failures are PATH
(agent CLIs invisible to non-interactive contexts) and environment (missing
proxy vars). It probes both before any timer ever fires.

Host defaults to the short hostname; `--host` overrides. `--manifest`
defaults to `~/automations/manifest.toml`, overridable via
`AUTOMATIONCTL_MANIFEST`.

### 6.2 Daily operation

```
$ automationctl list
TASK                  SCHEDULE           ENABLED   LAST         RESULT
canary                daily 08:00        yes       today 08:02  ok (4s)
nightly-repo-audit    daily 03:00        yes       today 03:04  ok (11m)
weekly-benchmark      weekly sun 05:00   yes       Aug 17       ok (2h 10m)
mirror-notes          daily 04:30        yes       today 04:30  ok (8s)

$ automationctl status nightly-repo-audit      # recent runs, exits, durations
$ automationctl logs nightly-repo-audit        # last run's stdout/stderr
$ automationctl logs -f nightly-repo-audit     # follow journald live (Linux)

$ automationctl run nightly-repo-audit         # foreground, streaming (debugging)
$ automationctl submit nightly-repo-audit      # background now, via substrate

$ automationctl pause weekly-benchmark         # temporary; next install restores
$ automationctl resume weekly-benchmark
```

`pause` is deliberately temporary: the git repo is the desired state, and
`install` re-asserts it. Permanent disabling is `disabled = true` in the spec,
committed.

### 6.3 Changing automations (the standing workflow)

```
$ cd ~/automations && git pull
$ $EDITOR tasks/nightly-repo-audit.toml
$ automationctl lint
$ automationctl install --dry-run --diff       # review rendered changes
$ automationctl install
$ git commit -am "Tighten audit prompt" && git push
```

Identical rhythm to `agent-bootstrap install` — same flags, same
generated-output discipline.

### 6.4 Maintenance

```
$ automationctl prune --keep-runs 50           # per-task run-record retention
$ automationctl uninstall weekly-benchmark     # remove generated units for one task
$ automationctl uninstall --all                # remove everything managed
```

---

## 7. The `exec` wrapper lifecycle

Every unit/plist runs `automationctl exec --manifest <path> <task>`. The
wrapper is the only cross-platform component, and the only nontrivial runtime
code:

1. Load manifest + spec; snapshot both into `meta.json`.
2. Acquire the named lock (`fcntl`, non-blocking). Contended → record
   `skipped`, exit 0 (a skip is not a failure).
3. Build environment: minimal base + `path_prepend` + `env_files` + task `env`.
   Env construction is wrapper-side so both platforms behave identically and
   units stay free of secrets.
4. Expand runner template and placeholders into final argv; resolve prompt
   (inline or file) and wire stdin. The prompt is written from its own thread,
   so a prompt larger than the pipe buffer cannot hold the wrapper past its
   own deadline when the child never reads stdin.
5. Spawn the child in `cwd`. Enforce `timeout` in-process:
   SIGTERM → 30s grace → SIGKILL.
6. Tee child stdout/stderr to the run directory *and* to the wrapper's own
   stdio — files give uniform cross-platform logs, passthrough feeds journald
   on Linux for `logs -f`.
7. On exit, run `summary_cmd` over captured stdout → `result.json`.
8. Write `meta.json` (argv, timings, exit, `--version` of the invoked binary
   when cheaply obtainable, and the task fields *after* manifest defaults are
   applied), update `last/<task>.json`.
9. On failure or timeout: fire `on_failure` transports with the exit reason,
   stderr tail, and summary. Transports resolve their variables — an ntfy URL,
   a webhook token — from the same environment the child was given, because
   that is where `env_files` put them; a timer's ambient environment has none
   of it. In-wrapper notify is primary (works on both platforms); the systemd
   `OnFailure=` hook is a Linux-only backup for the case where the wrapper
   itself dies. The whole dispatch is wrapped in a catch-all: a transport
   reaches arbitrary third-party code, and step 10 is not negotiable.
10. Exit with the child's code, so the substrate's own status reflects truth.
    No notification failure may ever change this — not a refused connection,
    not a malformed URL, not an exception nobody enumerated. A transport that
    breaks is recorded as a failed notification in `meta.json` and nothing
    more.

---

## 8. Policy and lint

`lint` runs standalone and as a hard gate inside `install`:

- Schema: unknown fields, missing required fields, `schema_version` mismatch
  (tool refuses specs newer than it understands).
- Exclusivity: `command` xor `runner`; `prompt` xor `prompt_file`.
- References: runner exists, `prompt_file` exists, notify transport defined.
- Policy: expanded final argv is scanned against `[lint].forbidden_argv`;
  a hit is an error unless the task or runner carries
  `allow_full_access = true`. The deny-list lives in the private manifest —
  the tool ships the mechanism, never opinions about specific agents.
- Schedule: grammar parses and is expressible by the target backend.

Unattended full-access agent runs therefore require a visible, greppable,
committed marker — reviewable in the `automations` git history.

---

## 9. Milestones

| Milestone | Delivers | Proves |
|-----------|----------|--------|
| M0 ✅ | spec/manifest/runners models, template expansion, `lint`, `run` (foreground), run records | the agent-neutral contract works end to end with zero substrate |
| M1 ✅ | full `exec` lifecycle: locks, env building, timeout, tee logging, `summary_cmd`, notify; `status`, `logs`, `doctor` | unattended-grade runtime behavior |
| M2 ✅ | systemd backend: `install --dry-run --diff`, reconcile/GC, timers, `submit`, `pause`/`resume`, `uninstall` | daily-usable on the Linux host |
| M3 ✅ | launchd backend + catch-up agent; backend interface solidified from two real implementations; `prune` | mac parity |
| M4 (maybe never) | `watch_path` triggers, socket/webhook activation, pueue backend, cross-host record aggregation | only if a real need appears |

M0–M3 are implemented. The launchd backend is rendered and reconciled by the
same code path as systemd but has not yet been exercised on a real Mac; its
control verbs are covered only by recorded-command tests.

Sequencing notes: the backend interface is deliberately *not* abstracted in
M2 — it is extracted in M3 when the second implementation exists. The private
`automations` repo starts at M0 (specs are testable via `run` before any
scheduling exists). The repository stays private with public-grade discipline;
publication around M2–M3 after a self-containment audit.

## 10. Open questions (to settle during implementation)

- PyPI name availability for `automationctl` (fallback: install from git).
- Notify transports beyond ntfy: macOS `osascript` desktop notifications?
  email? (Transport interface makes this additive.)
- Whether `logs` should page/merge historical runs or only show the latest.
- Journald rate limiting for very chatty agent output (tee already guarantees
  the file copy is complete).
- Retention defaults: prune policy shipped as a scheduled task in
  `automations` itself (the system maintaining itself) vs. manual.

---

## 11. Implementation notes

Decisions taken while implementing M0–M3 that refine or depart from the
sections above. Each stays inside the design's principles — agent-neutral,
thick wrapper over a dumb substrate, scope-explicit, data not code — and this
section is the authoritative record of them.

### 11.1 Additional modules

Five modules exist beyond §3.1's tree, each carrying a concern that would
otherwise be duplicated or would fatten `cli.py`:

- `paths.py` — every filesystem location (state dir, generated-unit dir,
  manifest, the `automationctl` path embedded in units), each overridable by
  an environment variable so that tests and dry runs never touch real state.
- `errors.py` — the shared exception hierarchy, imported by every module.
- `commands.py` — the seam described in §11.2. A backend's state directory is
  a required constructor argument for the same reason the seam exists: a
  default that quietly reached for the real one is the single way a test could
  write to the user's own state.
- `catchup.py` — missed-run decisions; it needs both `schedule` and `records`
  and belongs to neither.
- `doctor.py` — read-only host probes, kept out of the command surface.

### 11.2 The scheduler-command seam

Every `systemctl` and `launchctl` invocation goes through a `CommandRunner`
protocol. Production uses `SubprocessRunner`; tests inject `RecordingRunner`,
which records argv and returns canned results. This is what makes the install,
reconcile, pause, and submit paths testable on a machine whose real scheduler
must not be touched, and it costs one indirection.

### 11.3 No systemd `OnFailure=` hook

§5 showed `OnFailure=automationctl-notify@%n.service` as a Linux-only backup
for a wrapper that dies before it can notify. Rendering that line without also
generating the template unit it names would make every failure produce a
second, spurious failure. In-wrapper notification is primary and covers both
platforms; the backup hook is deferred until it has a unit to point at.

### 11.4 Jitter on launchd

launchd has no `RandomizedDelaySec`. Where a **scheduled** task configures
`randomized_delay`, the generated plist starts `automationctl exec --jitter`,
and the wrapper sleeps a uniform random interval before acquiring the lock.
systemd timers keep `RandomizedDelaySec` and never pass `--jitter`, so jitter
is applied exactly once on either platform.

A task with no schedule never carries `--jitter`. Its agent exists only so
that `submit` has something to kick, and for such a task `submit` means now on
both platforms.

For a **scheduled** task with `randomized_delay`, `submit` diverges, and the
divergence is inherent rather than chosen. systemd's `submit` starts the
service directly, so it is immediate. launchd has no way to start an agent
other than through the argv in its plist, so `launchctl kickstart` re-runs
`exec --jitter` and the submitted run waits out the jitter like a scheduled
one. The alternative — a second, jitter-free agent per task purely so that
`submit` can bypass the first — doubles the generated surface to paper over a
one-line difference.

`run` is the immediate path on macOS: it is the foreground, streaming verb,
never applies jitter unless asked, and is what "start this now and watch it"
should mean on either platform anyway.

### 11.5 Strict argv, lenient prompts

Placeholder expansion is strict in argv — `{{` and `}}` are escapes for
literal braces, and an unknown `{name}` is an error, because a typo there
silently changes a command line.

It is lenient in prompt text. Known placeholders are substituted; every other
brace is left exactly as written, doubled ones included. A prompt is prose,
routinely containing JSON, shell brace expansion, or code samples, and this
tool has no business rewriting an author's braces to deliver an escape
convention the author never opted into. Prompt text is substituted into argv
in the same single pass, so an expanded prompt is never rescanned.

### 11.6 Process-group termination

Timeout enforcement starts the child in a new session and signals the whole
process group (SIGTERM, 30s grace, SIGKILL). A bare signal to the direct child
would leave a `sh -c` wrapper's grandchildren running past the timeout.

### 11.7 CLI option placement and scope

`--manifest`, `--host`, `--backend`, and `--unit-dir` are per-command options
rather than global ones, which is how typer expresses options that only some
verbs need. `--backend` and `--unit-dir` also make cross-platform rendering
reviewable from either host.

`run` and `exec` accept any task in `tasks/`, not only those the current host
selects — debugging a spec before adding it to a host list is the common case.
`install` renders only the host's selection, so the manifest remains the sole
authority over what is scheduled.

`list` reports a task as installed based on the presence of its generated
files and only then asks the substrate whether it is enabled; it never queries
the scheduler about a task it has not installed.

### 11.8 Configuration surface added during implementation

- `[notify.<name>]` accepts an explicit `type` (`ntfy` or `command`, otherwise
  inferred) and an optional `title`. The `command` transport fills `{title}`,
  `{body}`, `{task}`, `{status}`, `{exit_code}`, and `{run_dir}`.
- Runners accept an optional `description`.
- Task specs may carry `schema_version`; it is validated when present.
- `meta.json` carries the spec snapshot under `spec` and the values that
  actually governed the run under `effective` (timeout, `on_failure`,
  randomized delay, persistence). §7 step 1 says "manifest + spec"; recording
  the resolved values rather than the whole manifest is what lets a record
  explain a timeout the spec never mentions.
- Task names must match `[A-Za-z0-9][A-Za-z0-9._-]*`, since a task name
  becomes a unit name, a launchd label, and a run-directory component. The
  rule is enforced where specs are loaded, not only in `lint`, so no code path
  can reach the filesystem with a name lint would reject. `catchup` is
  additionally reserved: automationctl generates an agent of that name.
- `env_files` are parsed as `KEY=value` with `#` comments, optional `export`,
  and optional surrounding quotes. A missing env file fails the run rather
  than starting a task without its secrets.
- Run statuses are `ok`, `failed`, `timeout`, `skipped`, and `error`, where
  `error` marks a wrapper-level failure (missing binary, missing `cwd`,
  unreadable env file) that never reached the child process.

### 11.9 Schedules are local wall clock

`OnCalendar` and `StartCalendarInterval` both fire against the machine's local
time, so `daily 03:00` means 03:00 local on both platforms — not 03:00 UTC.
Catch-up converts the current instant to local time before applying a
schedule's wall-clock fields, and compares the resulting instant against the
run record. Records themselves stay in UTC: an instant is an instant, and only
the calendar arithmetic is local. Without this, catch-up on any host west or
east of UTC both fires runs that were not missed and misses runs that were, by
exactly the UTC offset.

The calendar arithmetic itself runs on *naive* local wall-clock values, and
the zone is re-attached only once a candidate date is settled. An aware
datetime carries the concrete UTC offset in force at its own instant;
subtracting days from it keeps that offset frozen, so walking back across a
daylight-saving boundary lands every earlier candidate an hour off the wall
clock the scheduler actually fired on. Resolving the offset per candidate date
is what keeps "03:00" meaning 03:00 on both sides of a transition, and it is
why the comparison happens on naive values rather than on the aware ones the
records supply.

Interval schedules are unaffected — elapsed time has no calendar.

### 11.10 Missed-run replay for escape-hatch schedules

A per-backend `[schedule]` table defaults to `persistent = true`, exactly like
the neutral calendar forms. Both documented escape forms — a systemd
`OnCalendar` expression and a launchd `StartCalendarInterval` list — are
calendar-shaped, and silently dropping `Persistent=` for them would make the
escape hatch quietly weaker than the grammar it exists to extend. An explicit
`persistent` in the spec still wins.

The wrapper's own catch-up declines these schedules: their contents are opaque
to the neutral grammar, so it cannot compute a previous occurrence. It says so
explicitly rather than reporting "not due", and `status` prints the catch-up
decision and its reason for every task.

The reason names the backend in play, because what happens to a missed
occurrence then depends entirely on it: systemd's `Persistent=` replays one,
and launchd has no equivalent. A single message claiming "the backend's own
missed-run handling applies" would be a promise only one platform keeps.

### 11.11 Reconcile refuses an undeclared host

An undeclared host selects no tasks, so a reconcile computes an empty desired
state and garbage-collects every managed unit on the machine. `install`
therefore refuses outright when the manifest has no `[hosts.<name>]` section
for the target host: no plan is computed and nothing is executed. A typo in
`--host` must never uninstall an installation, and `doctor` already treats the
same condition as a failed check.

`uninstall --all` keeps no such guard. It is an explicit request that names
every file it removes, and it is the right tool for cleaning up a host the
manifest no longer declares.

### 11.12 The install gate lints this host's selection

Standalone `automationctl lint` scans the whole repository. The gate inside
`install` scans only the tasks this host selects, plus the manifest-level
checks that bear on that selection. One repository serves every host (D7), so
a spec written for another machine — a launchd-only escape hatch, say — must
not block this machine from installing. The full scan is still one command
away, and is what a pre-commit hook in the automations repository should run.

### 11.13 A refused scheduler command fails the verb

`install`, `uninstall`, `pause`, `resume`, and `submit` exit non-zero if any
control command failed, after printing a warning line for each. The files on
disk still describe the desired state; what failed is the substrate's
agreement with it, and a green exit code on a host whose timers were never
enabled is worse than no exit code at all.

### 11.14 launchd reloads only what it has not already activated

launchd cannot redefine a loaded agent in place: a changed plist means
`bootout` then `bootstrap`, which kills whatever that agent is running. Only
agents whose definition launchd has not already accepted are reloaded.

A label is reloaded when **either** of two independent signals says so, and it
takes both to be correct.

*Recorded activation.* `activated/<backend>.json` in the state directory holds
a content hash per label, written only after that label's `bootstrap`
succeeded. A hash that differs from the desired content — or is missing —
means reload. This is what a file diff cannot see: `apply` has already written
the files by the time activation runs, so an install whose activation failed
or was interrupted would look identical to a successful one on the next pass,
and launchd would keep running the old definition forever behind green
installs.

*The reconcile's own rewrites.* `install` passes the plan's created and
updated filenames, captured before `apply` writes them. This is what the
activation record cannot see: a plist hand-edited on disk and loaded by
someone else — login loads everything in `~/Library/LaunchAgents` — still
hashes to what we last activated, so the record says "nothing to do" while
launchd runs the edit. Rewriting the file back is exactly the moment to
reconverge it, and D8 says generated output is never hand-edited: hand edits
are overwritten by design, which has to include the loaded copy.

The union costs at most a needless reload, always in the safe direction.

Every desired agent still gets `launchctl enable`, which is what clears the
persistent disable override left by `pause` and makes "install re-asserts the
repository state" true on macOS; agents that are not loaded at all are
bootstrapped regardless of their hash.

Load state comes from a read-only `launchctl print` probe with three answers,
not two: loaded, not loaded, and **unknown**. Unknown is treated as needing a
reload in both directions — `activate` boots out an unknown label before
bootstrapping it, exactly as `deactivate` boots one out rather than skipping
it. Treating "the probe failed" as "there is nothing to stop" is how a totally
broken substrate turns into an `uninstall` that deletes every plist, issues no
commands, and exits 0 while the agents keep running against files that no
longer exist. Each command's own result then flows into §11.13 and fails the
verb, which is the outcome that tells the truth.

Only launchctl's "could not find service" code (113) is a definite no, and
only when the domain itself answers: launchctl reports the same code for a
label in a domain it cannot reach as for a label that genuinely is not loaded.
So a 113 is qualified by a `launchctl print <domain>` probe, run lazily and at
most once per verb — nothing pays for it unless a 113 actually turns up — and
a domain that will not answer downgrades every 113 to unknown.

systemd needs none of this: `enable --now` is idempotent and does not
interrupt a running service, `daemon-reload` has already taught it the new
definitions, so every scheduled timer is re-asserted on every install and no
activation memory is kept.

### 11.15 Catch-up runs serially

`catch-up` evaluates every selected task against one instant and then runs the
due ones one at a time, in the foreground. A machine returning from a week
offline should not start every missed agent job at once, and tasks sharing a
named lock would turn most of them into skips anyway.
