"""Command-line interface for automationctl."""

from importlib.metadata import version
from typing import Annotated

import typer

app = typer.Typer(
    name="automationctl",
    help="Agent-neutral automation runner: compile task specs to the platform scheduler.",
    no_args_is_help=True,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(version("automationctl"))
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Agent-neutral automation runner."""
