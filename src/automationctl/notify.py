"""Failure notification transports.

Two transports ship with the tool, both generic: ``ntfy`` (an HTTP POST to a
URL resolved from an environment variable named by the manifest) and
``command`` (any argv the user chooses). Neither knows anything about the task
it reports on.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .commands import CommandRunner, SubprocessRunner
from .spec import Manifest, NotifyTransport

NOTIFY_PREFIX = "notify:"


@dataclass(frozen=True)
class NotifyEvent:
    """What happened, in transport-neutral form."""

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
    """Whether one transport delivered the event."""

    transport: str
    ok: bool
    detail: str = ""


class HttpSender(Protocol):
    """Posts a notification body to a URL."""

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> None: ...


def post(url: str, body: bytes, headers: Mapping[str, str]) -> None:
    """Default HTTP sender: a plain POST with no dependencies."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def _fill(items: Sequence[str], values: Mapping[str, str]) -> tuple[str, ...]:
    filled: list[str] = []
    for item in items:
        text = item
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        filled.append(text)
    return tuple(filled)


def send(
    transport: NotifyTransport,
    event: NotifyEvent,
    *,
    env: Mapping[str, str],
    runner: CommandRunner | None = None,
    sender: HttpSender | None = None,
) -> NotifyOutcome:
    """Deliver one event through one transport."""
    if transport.kind == "ntfy":
        if not transport.url_env:
            return NotifyOutcome(transport.name, False, "transport has no url_env")
        url = env.get(transport.url_env)
        if not url:
            return NotifyOutcome(
                transport.name, False, f"environment variable {transport.url_env} is not set"
            )
        if not url.startswith(("http://", "https://")):
            return NotifyOutcome(
                transport.name, False, f"{transport.url_env} is not an http(s) URL: {url!r}"
            )
        title = transport.title or event.title
        deliver = sender if sender is not None else post
        try:
            deliver(url, event.body.encode("utf-8"), {"Title": title})
        except (OSError, urllib.error.URLError, ValueError) as exc:
            # ValueError is in the list deliberately: urllib raises a bare one
            # for a URL it cannot classify. A notification must never be able
            # to kill the wrapper after the run records are already written and
            # take the child's exit code with it.
            return NotifyOutcome(transport.name, False, f"post failed: {exc}")
        return NotifyOutcome(transport.name, True, "posted")

    values = event.placeholders()
    if transport.title:
        values["title"] = transport.title
    argv = _fill(transport.command, values)
    execute = runner if runner is not None else SubprocessRunner()
    result = execute.run(argv, timeout=30)
    if result.ok:
        return NotifyOutcome(transport.name, True, "command exited 0")
    return NotifyOutcome(
        transport.name, False, f"command exited {result.returncode}: {result.stderr.strip()}"
    )


def dispatch(
    references: Sequence[str],
    manifest: Manifest,
    event: NotifyEvent,
    *,
    env: Mapping[str, str],
    runner: CommandRunner | None = None,
    sender: HttpSender | None = None,
) -> list[NotifyOutcome]:
    """Deliver an event through every ``notify:<name>`` reference."""
    outcomes: list[NotifyOutcome] = []
    for reference in references:
        name = transport_name(reference)
        if name is None:
            outcomes.append(NotifyOutcome(reference, False, "unsupported on_failure entry"))
            continue
        transport = manifest.notify.get(name)
        if transport is None:
            outcomes.append(NotifyOutcome(name, False, "transport not defined in manifest"))
            continue
        outcomes.append(send(transport, event, env=env, runner=runner, sender=sender))
    return outcomes


def transport_name(reference: str) -> str | None:
    """Return the transport name of a ``notify:<name>`` reference, else ``None``."""
    if not reference.startswith(NOTIFY_PREFIX):
        return None
    name = reference[len(NOTIFY_PREFIX) :].strip()
    return name or None
