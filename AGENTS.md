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
- `uv tool install prek` — install the hook runner once per machine.
- `prek install` — one-time per clone; installs the Git hooks defined in
  `.pre-commit-config.yaml`. `uv` and `prek` are per-machine prerequisites
  and are not declared as project dependencies.
- `uv run automationctl` — run the CLI from source.

## Validation

- Full: `prek run --all-files && prek run --all-files --hook-stage pre-push`
- Targeted: `prek run --files <changed-path>... && prek run --files <changed-path>... --hook-stage pre-push`
- Documentation-only: `prek run --files <changed-document-path>... && git diff --check -- <changed-document-path>...`
- `.pre-commit-config.yaml` is the sole definition of mechanical commands and
  their scope. Its `commit-msg` hook validates commit subjects separately from
  source-file validation.
- CI (`.github/workflows/ci.yml`) invokes the hook runner rather than
  restating hook commands. A `lint` job runs the commit-stage hooks once,
  and a matrixed `test` job runs the pre-push stage on every supported
  Python version, then builds and smoke-tests the wheel on the lowest one.

## Git

- Base branch: `main`, tracking `origin/main` on the public GitHub
  repository. Never add another remote or change visibility without explicit
  approval.
- Small focused changes commit directly to `main`. Substantial or multi-commit
  work uses a short-lived `<prefix>/<task-name>` branch merged back to `main`.
- Commit subjects: `prefix: concise imperative summary`, no trailing period,
  no scopes; Git-generated merge subjects beginning with `Merge ` are the only
  exception. Allowed prefixes:
  - `feat` — functionality
  - `fix` — correctness
  - `docs` — documentation
  - `refactor` — behavior-preserving structure
  - `test` — test-only changes
  - `build` — packaging, dependencies, project metadata
  - `ci` — continuous-integration configuration
  - `chore` — other maintenance
- Validate before committing.

## Skill Distribution

- The root `SKILL.md` is the single authoritative product usage skill
  (`automationctl-skill`) and installs as one self-contained file. Keep its
  frontmatter to `name` and `description` only, keep it free of personal
  paths and private references, and update it whenever an agent's operational
  use of the CLI changes. Installing it does not install the binary.

## Publication and Releases

- The repository is public at `https://github.com/zydtiger/automationctl`
  (base branch `main`). Every commit must stay self-contained: no personal
  paths, host names, or private repository references; the pre-publication
  audit standard keeps applying to all new content.
- Version scheme is `0.x` under active development: breaking changes are
  expected between releases.
- Release contract: releases are annotated git tags named `vX.Y.Z` matching
  the `pyproject.toml` version, each with a GitHub Release whose notes
  summarize the changes and disclose known limitations. Nothing is published
  to PyPI. Installation is `uv tool install
  git+https://github.com/zydtiger/automationctl@vX.Y.Z`, or unpinned from
  `main`. To release: bump the version in `pyproject.toml` on `main`, run
  `uv lock` to synchronize `uv.lock`, commit both files, validate, tag that
  commit, push the tag, and publish the GitHub Release from it.
