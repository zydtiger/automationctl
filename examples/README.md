# Standalone task examples

These files are source inputs. Install one explicitly; they are not a manifest
and automationctl never scans this directory.

```bash
automationctl lint examples/tasks/hourly-check.toml
automationctl install examples/tasks/hourly-check.toml --dry-run --diff
```
