"""Schema-v2 task and policy parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from automationctl.config import materialize
from automationctl.errors import ConfigError
from automationctl.spec import load_toml, parse_policy, parse_task


def test_schema_v2_requires_name_description_and_command(tmp_path: Path) -> None:
    path = tmp_path / "task.toml"
    path.write_text(
        'schema_version = 2\nname = "x"\ndescription = "x"\ncommand = ["true"]\n', encoding="utf-8"
    )
    assert parse_task(load_toml(path), path).name == "x"
    path.write_text(
        'schema_version = 1\nname = "x"\ndescription = "x"\ncommand = ["true"]\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unsupported schema_version"):
        parse_task(load_toml(path), path)


def test_stdin_file_is_materialized_relative_to_its_source_file(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello {task}", encoding="utf-8")
    source = tmp_path / "task.toml"
    source.write_text(
        'schema_version = 2\nname = "x"\ndescription = "x"\n'
        'command = ["true"]\nstdin_file = "prompt.txt"\n',
        encoding="utf-8",
    )
    task = materialize(parse_task(load_toml(source), source))
    assert task.stdin == "hello {task}" and task.stdin_file is None


def test_policy_has_no_task_defaults_or_hosts(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('schema_version = 2\n[lint]\nforbidden_argv = ["x"]\n', encoding="utf-8")
    assert parse_policy(load_toml(path), path).lint.forbidden_argv == ("x",)
    path.write_text("schema_version = 2\n[hosts.machine]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown field"):
        parse_policy(load_toml(path), path)


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (
            '[notify.hook]\ntype = "command"\ncommand = ["/bin/true"]\nurl_env = "URL"',
            "command transport cannot set url_env",
        ),
        (
            '[notify.hook]\ntype = "ntfy"\nurl_env = "URL"\ncommand = ["/bin/true"]',
            "ntfy transport cannot set command",
        ),
    ],
)
def test_notify_transports_reject_incompatible_fields(
    tmp_path: Path, transport: str, message: str
) -> None:
    path = tmp_path / "task.toml"
    path.write_text(
        'schema_version = 2\nname = "x"\ndescription = "x"\ncommand = ["true"]\n'
        + transport
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=message):
        parse_task(load_toml(path), path)


def test_summary_cmd_must_be_nonempty_when_supplied(tmp_path: Path) -> None:
    path = tmp_path / "task.toml"
    path.write_text(
        'schema_version = 2\nname = "x"\ndescription = "x"\ncommand = ["true"]\nsummary_cmd = []\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="summary_cmd must be non-empty"):
        parse_task(load_toml(path), path)
