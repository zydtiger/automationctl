"""Placeholder substitution for standalone task argv and stdin."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .errors import TemplateError
from .spec import TaskSpec

_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def builtin_values(task: str, hostname: str, run_dir: str, now: datetime) -> dict[str, str]:
    return {
        "date": now.strftime("%Y-%m-%d"),
        "hostname": hostname,
        "task": task,
        "run_dir": run_dir,
    }


def substitute(text: str, values: Mapping[str, str], *, where: str, strict: bool = True) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in {"{{", "}}"}:
            return token[0] if strict else token
        name = match.group(1)
        assert name is not None
        if name not in values:
            if not strict:
                return token
            raise TemplateError(f"{where}: unknown placeholder {{{name}}}")
        return values[name]

    return _TOKEN_RE.sub(replace, text)


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    stdin_text: str | None = None


def build_invocation(task: TaskSpec, values: Mapping[str, str]) -> Invocation:
    argv = tuple(
        os.path.expanduser(substitute(item, values, where=f"task {task.name}"))
        if item.startswith("~")
        else substitute(item, values, where=f"task {task.name}")
        for item in task.command
    )
    stdin = (
        substitute(task.stdin, values, where=f"task {task.name} stdin", strict=False)
        if task.stdin is not None
        else None
    )
    return Invocation(argv, stdin)
