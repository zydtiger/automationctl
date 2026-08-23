"""Notification transports: ntfy over HTTP and a generic command hook."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from automationctl.commands import CommandResult, RecordingRunner
from automationctl.notify import NotifyEvent, dispatch, send, transport_name
from automationctl.spec import Manifest, NotifyTransport

EVENT = NotifyEvent(
    task="audit",
    status="failed",
    exit_code=3,
    title="automationctl: audit failed",
    body="exit 3",
    run_dir="/runs/1",
)


def manifest(**transports: NotifyTransport) -> Manifest:
    return Manifest(
        path=Path("/example/manifest.toml"),
        root=Path("/example"),
        schema_version=1,
        notify=dict(transports),
    )


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, Mapping[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        self.calls.append((url, body, headers))


def test_transport_name_parses_references() -> None:
    assert transport_name("notify:ntfy") == "ntfy"
    assert transport_name("email") is None
    assert transport_name("notify:") is None


def test_ntfy_posts_to_the_url_from_the_environment() -> None:
    recorder = Recorder()
    transport = NotifyTransport(name="ntfy", kind="ntfy", url_env="NTFY_URL")
    outcome = send(transport, EVENT, env={"NTFY_URL": "https://example/topic"}, sender=recorder)
    assert outcome.ok
    assert recorder.calls[0][0] == "https://example/topic"
    assert recorder.calls[0][1] == b"exit 3"
    assert recorder.calls[0][2]["Title"] == EVENT.title


def test_ntfy_reports_a_missing_environment_variable() -> None:
    transport = NotifyTransport(name="ntfy", kind="ntfy", url_env="NTFY_URL")
    outcome = send(transport, EVENT, env={}, sender=Recorder())
    assert not outcome.ok
    assert "NTFY_URL is not set" in outcome.detail


@pytest.mark.parametrize("url", ["ntfy.example/alerts", "not a url", "ftp://example/x", "/topic"])
def test_ntfy_rejects_a_url_that_is_not_http(url: str) -> None:
    """A typo'd env-file value must be a failed notification, not an exception."""
    transport = NotifyTransport(name="ntfy", kind="ntfy", url_env="NTFY_URL")
    outcome = send(transport, EVENT, env={"NTFY_URL": url}, sender=Recorder())
    assert not outcome.ok
    assert "not an http(s) URL" in outcome.detail


@pytest.mark.parametrize("url", ["HTTPS://example/topic", "Http://example/topic"])
def test_ntfy_accepts_an_uppercase_scheme(url: str) -> None:
    """URL schemes are case-insensitive; the guard must not reject a valid one."""
    recorder = Recorder()
    transport = NotifyTransport(name="ntfy", kind="ntfy", url_env="NTFY_URL")
    outcome = send(transport, EVENT, env={"NTFY_URL": url}, sender=recorder)
    assert outcome.ok
    assert recorder.calls[0][0] == url


def test_ntfy_survives_a_sender_that_raises_value_error() -> None:
    """urllib raises a bare ValueError for URLs it cannot classify."""

    def picky(url: str, body: bytes, headers: Mapping[str, str]) -> None:
        raise ValueError("unknown url type: 'https://'")

    transport = NotifyTransport(name="ntfy", kind="ntfy", url_env="NTFY_URL")
    outcome = send(transport, EVENT, env={"NTFY_URL": "https://"}, sender=picky)
    assert not outcome.ok
    assert "unknown url type" in outcome.detail


def test_ntfy_reports_a_transport_failure() -> None:
    def broken(url: str, body: bytes, headers: Mapping[str, str]) -> None:
        raise OSError("connection refused")

    transport = NotifyTransport(name="ntfy", kind="ntfy", url_env="NTFY_URL")
    outcome = send(transport, EVENT, env={"NTFY_URL": "https://example"}, sender=broken)
    assert not outcome.ok
    assert "connection refused" in outcome.detail


def test_command_transport_fills_placeholders() -> None:
    runner = RecordingRunner()
    transport = NotifyTransport(
        name="desktop", kind="command", command=("notify-send", "{task} {status}", "{body}")
    )
    outcome = send(transport, EVENT, env={}, runner=runner)
    assert outcome.ok
    assert runner.calls == [("notify-send", "audit failed", "exit 3")]


def test_command_transport_reports_a_nonzero_exit() -> None:
    argv = ("false",)
    runner = RecordingRunner(responses={argv: CommandResult(argv, 1, stderr="boom")})
    transport = NotifyTransport(name="hook", kind="command", command=argv)
    outcome = send(transport, EVENT, env={}, runner=runner)
    assert not outcome.ok
    assert "boom" in outcome.detail


def test_dispatch_reports_undefined_and_unsupported_references() -> None:
    outcomes = dispatch(
        ["notify:ghost", "email"],
        manifest(),
        EVENT,
        env={},
        runner=RecordingRunner(),
    )
    assert [item.ok for item in outcomes] == [False, False]
    assert "not defined in manifest" in outcomes[0].detail
    assert "unsupported on_failure entry" in outcomes[1].detail
