# Example automations repository

A complete, generic automations layout. Copy it into your own private
repository — conventionally `~/automations` — and edit it there. Nothing in
this directory is used by `automationctl` at runtime; it exists so that a new
host has a working shape to start from.

```
manifest.toml       schema version, host selection, defaults, notify, lint policy
runners.toml        data-only argv expansion tables
tasks/*.toml        one declarative task per file; the file name is the task name
prompts/*.md        long prompts kept out of the task specs
```

Try it without installing anything into the platform scheduler:

```bash
automationctl lint --manifest examples/manifest.toml --host workstation
automationctl list --manifest examples/manifest.toml --host workstation
automationctl install --manifest examples/manifest.toml --host workstation \
  --unit-dir ./generated-units --dry-run --diff
```

`--host` is only needed here because the example manifest declares
`workstation` and `laptop`; on a real host the short hostname selects the
section automatically.

Two rules make this layout safe to keep in git:

- Secrets never appear in it. `env_files` names machine-local files; ntfy is
  configured with `url_env`, the *name* of an environment variable.
- Full-access agent invocations require a committed `allow_full_access = true`
  marker, so `git log` shows exactly when unattended full access was granted.
