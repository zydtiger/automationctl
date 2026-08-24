"""Schema validation for the manifest, runner table, and task specs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from conftest import Tree

from automationctl.errors import ConfigError
from automationctl.spec import (
    TaskSpec,
    effective_persistent,
    effective_randomized_delay,
    effective_timeout,
    parse_manifest,
    parse_runners,
    parse_task,
)

FAKE = Path("/example/manifest.toml")


def parse_task_text(text: str, name: str = "sample") -> TaskSpec:
    return parse_task(tomllib.loads(text), FAKE, name)


def test_manifest_round_trip() -> None:
    manifest = parse_manifest(
        {
            "schema_version": 1,
            "defaults": {"timeout": "30m", "on_failure": ["notify:ntfy"]},
            "hosts": {"a": {"tasks": ["x"], "path_prepend": ["~/bin"]}},
            "notify": {"ntfy": {"url_env": "NTFY_URL"}},
            "lint": {"forbidden_argv": ["--danger"]},
        },
        FAKE,
    )
    assert manifest.defaults.timeout_seconds == 1800
    assert manifest.hosts["a"].tasks == ("x",)
    assert manifest.notify["ntfy"].kind == "ntfy"
    assert manifest.lint.forbidden_argv == ("--danger",)


def test_catchup_sweep_accepts_a_duration() -> None:
    manifest = parse_manifest({"schema_version": 1, "defaults": {"catchup_sweep": "6h"}}, FAKE)
    assert manifest.defaults.catchup_sweep_seconds == 21600


def test_catchup_sweep_defaults_to_off() -> None:
    assert parse_manifest({"schema_version": 1}, FAKE).defaults.catchup_sweep_seconds is None


@pytest.mark.parametrize("value", ["soon", "6", 6, "-1h"])
def test_catchup_sweep_rejects_garbage(value: object) -> None:
    with pytest.raises(ConfigError):
        parse_manifest({"schema_version": 1, "defaults": {"catchup_sweep": value}}, FAKE)


def test_catchup_sweep_rejects_a_zero_length_period() -> None:
    """launchd rejects a non-positive StartInterval; "off" is spelled by omission."""
    with pytest.raises(ConfigError, match="must be a positive duration"):
        parse_manifest({"schema_version": 1, "defaults": {"catchup_sweep": "0s"}}, FAKE)


def test_manifest_requires_schema_version() -> None:
    with pytest.raises(ConfigError, match="schema_version"):
        parse_manifest({}, FAKE)


def test_manifest_refuses_newer_schema_version() -> None:
    with pytest.raises(ConfigError, match="newer than this tool"):
        parse_manifest({"schema_version": 99}, FAKE)


def test_manifest_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError, match="unknown field"):
        parse_manifest({"schema_version": 1, "hostz": {}}, FAKE)


def test_manifest_rejects_unknown_host_field() -> None:
    with pytest.raises(ConfigError, match="unknown field"):
        parse_manifest({"schema_version": 1, "hosts": {"a": {"task": []}}}, FAKE)


def test_ntfy_transport_requires_url_env() -> None:
    with pytest.raises(ConfigError, match="requires url_env"):
        parse_manifest({"schema_version": 1, "notify": {"n": {"title": "x"}}}, FAKE)


def test_command_transport_is_inferred_from_command() -> None:
    manifest = parse_manifest(
        {"schema_version": 1, "notify": {"desktop": {"command": ["true"]}}}, FAKE
    )
    assert manifest.notify["desktop"].kind == "command"


def test_runners_require_argv() -> None:
    with pytest.raises(ConfigError, match="argv is required"):
        parse_runners({"schema_version": 1, "runners": {"r": {"stdin": "prompt"}}}, FAKE)


def test_runner_stdin_must_be_prompt() -> None:
    with pytest.raises(ConfigError, match="stdin"):
        parse_runners(
            {"schema_version": 1, "runners": {"r": {"argv": ["x"], "stdin": "file"}}}, FAKE
        )


def test_task_requires_description() -> None:
    with pytest.raises(ConfigError, match="description"):
        parse_task_text('command = ["true"]')


def test_task_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError, match="unknown field"):
        parse_task_text('description = "d"\nhosts = ["a"]')


def test_task_rejects_wrong_type() -> None:
    with pytest.raises(ConfigError, match="command must be a list"):
        parse_task_text('description = "d"\ncommand = "true"')


def test_task_parses_schedule_and_durations() -> None:
    task = parse_task_text(
        'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\ntimeout = "45m"'
    )
    assert task.timeout_seconds == 2700
    assert task.schedule is not None
    assert task.schedule.text == "daily 03:00"


def test_task_reports_invalid_schedule_with_path() -> None:
    with pytest.raises(ConfigError, match="unknown schedule form"):
        parse_task_text('description = "d"\ncommand = ["true"]\nschedule = "hourly"')


def test_effective_values_fall_back_to_defaults(tree: Tree) -> None:
    tree.write_manifest(
        "schema_version = 1\n\n"
        '[defaults]\ntimeout = "30m"\nrandomized_delay = "5m"\n\n'
        '[hosts.testhost]\ntasks = ["hello"]\n'
    )
    tree.write_task("hello", 'description = "d"\ncommand = ["true"]\nschedule = "daily 03:00"\n')
    tree.write_task(
        "override", 'description = "d"\ncommand = ["true"]\ntimeout = "1m"\npersistent = false\n'
    )
    automations = tree.load()
    hello = automations.tasks["hello"]
    override = automations.tasks["override"]
    assert effective_timeout(automations.manifest, hello) == 1800
    assert effective_timeout(automations.manifest, override) == 60
    assert effective_randomized_delay(automations.manifest, hello) == 300
    assert effective_persistent(automations.manifest, hello) is True
    assert effective_persistent(automations.manifest, override) is False


def test_load_collects_task_errors_without_raising(tree: Tree) -> None:
    tree.write_task("good", 'description = "d"\ncommand = ["true"]\n')
    tree.write_task("bad", "description = 3\n")
    automations = tree.load()
    assert "good" in automations.tasks
    assert [error.path.stem for error in automations.errors] == ["bad"]
