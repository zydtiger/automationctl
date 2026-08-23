"""Smoke tests for the CLI entry point."""

from importlib.metadata import version

from typer.testing import CliRunner

from automationctl.cli import app

runner = CliRunner()


def test_version_flag_reports_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == version("automationctl")


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "automationctl" in result.output
