"""Task-local notification transports retain HTTP and command behavior."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from automationctl.commands import CommandResult, RecordingRunner
from automationctl.notify import NotifyEvent, dispatch, send, transport_name
from automationctl.spec import NotifyTransport

EVENT = NotifyEvent("audit", "failed", 3, "automationctl: audit failed", "exit 3", "/runs/1")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, Mapping[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        self.calls.append((url, body, headers))


def test_transport_name_and_ntfy_environment_resolution() -> None:
    recorder = Recorder()
    transport = NotifyTransport("ntfy", "ntfy", url_env="NTFY_URL")
    assert transport_name("notify:ntfy") == "ntfy"
    assert send(transport, EVENT, env={}).ok is False
    outcome = send(transport, EVENT, env={"NTFY_URL": "HTTPS://example/topic"}, sender=recorder)
    assert outcome.ok and recorder.calls[0][1] == b"exit 3"


@pytest.mark.parametrize("url", ["ntfy.example/alerts", "ftp://example/x", "/topic"])
def test_ntfy_rejects_non_http_url(url: str) -> None:
    outcome = send(NotifyTransport("ntfy", "ntfy", url_env="URL"), EVENT, env={"URL": url})
    assert not outcome.ok and "not an http(s) URL" in outcome.detail


def test_command_notifications_keep_argv_boundaries_and_report_failure() -> None:
    recorder = RecordingRunner()
    transport = NotifyTransport("hook", "command", command=("notify-send", "{task} {status}", ""))
    assert send(transport, EVENT, env={}, runner=recorder).ok
    assert recorder.calls == [("notify-send", "audit failed", "")]
    failed = RecordingRunner(responses={("false",): CommandResult(("false",), 1, stderr="boom")})
    assert not send(
        NotifyTransport("hook", "command", command=("false",)), EVENT, env={}, runner=failed
    ).ok


def test_dispatch_only_resolves_transports_on_the_task() -> None:
    outcomes = dispatch(["notify:ghost", "email"], {}, EVENT, env={})
    assert [item.ok for item in outcomes] == [False, False]
