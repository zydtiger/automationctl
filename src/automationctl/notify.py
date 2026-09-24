"""Failure notification transports declared by each task."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .commands import CommandRunner, SubprocessRunner
from .spec import NotifyTransport

NOTIFY_PREFIX = "notify:"


@dataclass(frozen=True)
class NotifyEvent:
    task: str
    status: str
    exit_code: int | None
    title: str
    body: str
    run_dir: str = ""

    def placeholders(self) -> dict[str, str]:
        return {
            "task": self.task,
            "status": self.status,
            "exit_code": "" if self.exit_code is None else str(self.exit_code),
            "title": self.title,
            "body": self.body,
            "run_dir": self.run_dir,
        }


@dataclass(frozen=True)
class NotifyOutcome:
    transport: str
    ok: bool
    detail: str = ""


class HttpSender(Protocol):
    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> None: ...


def post(url: str, body: bytes, headers: Mapping[str, str]) -> None:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def transport_name(reference: str) -> str | None:
    if not reference.startswith(NOTIFY_PREFIX):
        return None
    return reference.removeprefix(NOTIFY_PREFIX).strip() or None


def send(
    transport: NotifyTransport,
    event: NotifyEvent,
    *,
    env: Mapping[str, str],
    runner: CommandRunner | None = None,
    sender: HttpSender | None = None,
) -> NotifyOutcome:
    if transport.kind == "ntfy":
        url = env.get(transport.url_env or "")
        if not url:
            return NotifyOutcome(
                transport.name, False, f"environment variable {transport.url_env} is not set"
            )
        if not url.lower().startswith(("http://", "https://")):
            return NotifyOutcome(
                transport.name, False, f"{transport.url_env} is not an http(s) URL: {url!r}"
            )
        try:
            (sender or post)(url, event.body.encode(), {"Title": transport.title or event.title})
        except (OSError, urllib.error.URLError, ValueError) as exc:
            return NotifyOutcome(transport.name, False, f"post failed: {exc}")
        return NotifyOutcome(transport.name, True, "posted")
    values = event.placeholders()
    if transport.title:
        values["title"] = transport.title
    # Keep each argv boundary and only interpolate notification placeholders.
    argv = tuple(_fill(item, values) for item in transport.command)
    result = (runner or SubprocessRunner()).run(argv, timeout=30)
    return NotifyOutcome(
        transport.name,
        result.ok,
        "command exited 0"
        if result.ok
        else f"command exited {result.returncode}: {result.stderr.strip()}",
    )


def _fill(item: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        item = item.replace("{" + key + "}", value)
    return item


def dispatch(
    references: Sequence[str],
    transports: Mapping[str, NotifyTransport],
    event: NotifyEvent,
    *,
    env: Mapping[str, str],
    runner: CommandRunner | None = None,
    sender: HttpSender | None = None,
) -> list[NotifyOutcome]:
    outcomes: list[NotifyOutcome] = []
    for reference in references:
        name = transport_name(reference)
        if name is None:
            outcomes.append(NotifyOutcome(reference, False, "unsupported on_failure entry"))
        elif name not in transports:
            outcomes.append(NotifyOutcome(name, False, "transport not defined in task"))
        else:
            outcomes.append(send(transports[name], event, env=env, runner=runner, sender=sender))
    return outcomes
