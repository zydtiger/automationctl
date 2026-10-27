# Standalone task examples

These files are source inputs. Install one explicitly; they are not a manifest
and automationctl never scans this directory.

```bash
automationctl lint examples/tasks/hourly-check.toml
automationctl install examples/tasks/hourly-check.toml --dry-run --diff
```

For a one-time command, no task file is needed:

```bash
actl submit --transient sample --after 10m -- /usr/bin/true
```

Retain the printed execution ID for `status`, `logs`, `cancel`, or `reconcile`.
See the main README for session lifetime and native platform validation.
