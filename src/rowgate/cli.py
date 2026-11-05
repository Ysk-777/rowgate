"""Command-line interface for RowGate."""

from __future__ import annotations

from typing import Annotated

import typer

from rowgate import __version__

app = typer.Typer(
    name="rowgate",
    help="Check local CSV and Parquet files against rules written in YAML.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"rowgate {__version__}")
        raise typer.Exit(0)


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the RowGate version and exit.",
        ),
    ] = False,
) -> None:
    """Check local CSV and Parquet files against rules written in YAML."""


def main() -> None:
    """Console-script entry point."""
    app(prog_name="rowgate")
