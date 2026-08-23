"""Runner expansion and placeholder substitution.

The tool knows nothing about the programs it launches. A runner is a user-owned
argv table; ``command`` is verbatim argv and is always the ground truth. The
placeholder vocabulary is deliberately tiny: ``{date}``, ``{hostname}``,
``{task}``, ``{run_dir}``, plus ``{prompt}`` inside runner argv.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .errors import TemplateError
from .spec import Runner, TaskSpec

BUILTIN_PLACEHOLDERS = ("date", "hostname", "task", "run_dir")
PROMPT_PLACEHOLDER = "prompt"

_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def builtin_values(task: str, hostname: str, run_dir: str, now: datetime) -> dict[str, str]:
    """Build the built-in placeholder map for one run."""
    return {
        "date": now.strftime("%Y-%m-%d"),
        "hostname": hostname,
        "task": task,
        "run_dir": run_dir,
    }


def substitute(text: str, values: Mapping[str, str], *, where: str, strict: bool = True) -> str:
    """Replace ``{name}`` placeholders; ``{{`` and ``}}`` are literal braces.

    Argv is expanded strictly: an unknown placeholder there is almost always a
    typo that would silently change a command line. Prompt text is expanded
    leniently, because prose legitimately contains braces.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = match.group(1)
        if name not in values:
            if not strict:
                return token
            known = ", ".join(sorted(values))
            raise TemplateError(f"{where}: unknown placeholder {{{name}}} (known: {known})")
        return values[name]

    return _TOKEN_RE.sub(replace, text)


def _expand_item(item: str, values: Mapping[str, str], where: str) -> str:
    expanded = substitute(item, values, where=where)
    if item.startswith("~"):
        return os.path.expanduser(expanded)
    return expanded


@dataclass(frozen=True)
class Invocation:
    """The final process contract for one run."""

    argv: tuple[str, ...]
    stdin_text: str | None = None


def expand_argv(
    items: tuple[str, ...], values: Mapping[str, str], *, where: str
) -> tuple[str, ...]:
    """Expand placeholders and leading ``~`` across an argv list."""
    return tuple(_expand_item(item, values, where) for item in items)


def build_invocation(
    task: TaskSpec,
    runner: Runner | None,
    prompt: str | None,
    values: Mapping[str, str],
) -> Invocation:
    """Produce the final argv and stdin payload for a task."""
    where = f"task {task.name}"
    if task.command is not None:
        return Invocation(argv=expand_argv(task.command, values, where=where))
    if runner is None:
        raise TemplateError(f"{where}: neither command nor runner is available")

    prompt_text = None
    if prompt is not None:
        prompt_text = substitute(prompt, values, where=f"{where} prompt", strict=False)

    argv_values = dict(values)
    if prompt_text is not None:
        argv_values[PROMPT_PLACEHOLDER] = prompt_text
    argv = expand_argv(runner.argv, argv_values, where=f"runner {runner.name}")

    stdin_text = prompt_text if runner.stdin == PROMPT_PLACEHOLDER else None
    return Invocation(argv=argv, stdin_text=stdin_text)
