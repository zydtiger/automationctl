"""Reconcile only the automationctl tasks declared by Home Manager.

Run by the activation script of nix/home-manager.nix with a plain Python
interpreter, so it uses the standard library only.
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path


def save_owned(path: Path, names: set[str]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(sorted(names), handle)
        handle.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(desired: dict[str, Path], executable: str, backend: str) -> str:
    """Identify one reconciliation input: task contents, automationctl, and backend."""
    value = hashlib.sha256(f"{executable}\0{backend}\0".encode())
    for name, path in sorted(desired.items()):
        value.update(f"{name}\0".encode() + path.read_bytes() + b"\0")
    return value.hexdigest()


def synchronize(
    tasks: Path,
    state: Path,
    executable: str,
    backend: str,
    *,
    dry_run: bool = False,
    run=subprocess.run,
) -> None:
    if not tasks.is_dir():
        raise ValueError(f"task directory does not exist: {tasks}")
    desired = {}
    for path in sorted(tasks.glob("*.toml")):
        name = tomllib.loads(path.read_text())["name"]
        if name != path.stem:
            raise ValueError(f"task name must match filename: {path}")
        desired[name] = path

    def command(*args: str) -> None:
        run([executable, *args, "--backend", backend], check=True)

    def reconcile() -> None:
        owned = set(json.loads(state.read_text())) if state.exists() else set()
        # Preflight every replacement before removing or installing anything.
        for path in desired.values():
            command(
                "install", str(path), "--replace", "--dry-run", *(["--diff"] if dry_run else [])
            )
        for name in sorted(owned - desired.keys()):
            if dry_run:
                print(f"Would remove Home Manager task: {name}", flush=True)
            else:
                command("remove", name)
                owned.remove(name)
                save_owned(state, owned)
        for name, path in desired.items():
            if not dry_run:
                # install can write files before a scheduler command fails.
                # Remember attempted installs so retries and removal still own them.
                owned.add(name)
                save_owned(state, owned)
                command("install", str(path), "--replace")

    if dry_run:
        reconcile()
        return
    state.parent.mkdir(parents=True, exist_ok=True)
    # The last successful reconciliation's inputs; matching inputs skip the scheduler.
    stamp = state.with_suffix(".digest")
    current = digest(desired, executable, backend)
    with state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        owned = set(json.loads(state.read_text())) if state.exists() else set()
        if owned == desired.keys() and stamp.exists() and stamp.read_text() == current:
            print("automation tasks unchanged", flush=True)
            return
        stamp.unlink(missing_ok=True)
        reconcile()
        stamp.write_text(current)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--backend", choices=["launchd", "systemd"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    synchronize(
        args.tasks,
        args.state,
        args.executable,
        args.backend,
        dry_run=args.dry_run or bool(os.environ.get("DRY_RUN")),
    )


if __name__ == "__main__":
    main()
