# Repository Guidelines

## Purpose

- `automationctl` is an agent-neutral automation runner: declarative task
  specs are compiled to systemd user units on Linux and launchd LaunchAgents
  on macOS, executed through a short-lived per-run wrapper, with no resident
  daemon.
- Read `docs/DESIGN.md` before implementation work. It is the authoritative
  design contract: architecture decisions D1–D8, configuration schema, exec
  lifecycle, and milestones M0–M4.
- Keep the repository self-contained and public-ready at all times: no
  personal paths, host names, private repository references, or
  machine-specific defaults in code, tests, examples, or documentation.

## Layout

- `src/automationctl/` — the package: CLI, spec models, backends, exec wrapper.
- `tests/` — pytest suite; backend rendering is verified with golden files.
- `docs/DESIGN.md` — design contract.
- `examples/` — generic sample automations layout (arrives with M0).

## Setup and Commands

- `uv sync` — create or refresh the development environment.
- `prek install` — one-time per clone; installs the Git hooks defined in
  `.pre-commit-config.yaml`. `uv` and `prek` are per-machine prerequisites
  and are not declared as project dependencies.
- `uv run automationctl` — run the CLI from source.

## Validation

- Full: `prek run --all-files && uv run pytest`
- Targeted: `uv run pytest tests/<file>` or a `::<test>` selector.
- Mechanical scope — lint, format, types, file hygiene — is defined solely by
  `.pre-commit-config.yaml`; do not restate those commands or their scopes
  elsewhere.

## Git

- Base branch: `main`. No remote is configured; never create a remote or
  publish without explicit approval.
- Small focused changes commit directly to `main`. Substantial or multi-commit
  work uses a short-lived `<prefix>/<task-name>` branch merged back to `main`.
- Commit subjects: `prefix: concise imperative summary`, no trailing period,
  no scopes. Allowed prefixes:
  - `feat` — functionality
  - `fix` — correctness
  - `docs` — documentation
  - `refactor` — behavior-preserving structure
  - `test` — test-only changes
  - `build` — packaging, dependencies, project metadata
  - `chore` — other maintenance
- Validate before committing.

## Publication and Releases

- The repository is public at `https://github.com/zydtiger/automationctl`
  (base branch `main`). Every commit must stay self-contained: no personal
  paths, host names, or private repository references; the pre-publication
  audit standard keeps applying to all new content.
- Version scheme is `0.x` under active development: breaking changes are
  expected. There are no releases or tags yet; the release contract is decided
  before the first release tag.
