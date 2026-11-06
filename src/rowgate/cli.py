"""Command-line interface for RowGate."""

from __future__ import annotations

import io
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from rowgate import __version__, report_text
from rowgate.checks import run_checks
from rowgate.errors import RowGateError
from rowgate.loader import load_rules

EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_ERROR = 2

app = typer.Typer(
    name="rowgate",
    help="Check local CSV and Parquet files against rules written in YAML.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

RulesArgument = Annotated[
    Path,
    typer.Argument(metavar="RULES.yaml", help="Path to the rules YAML file.", show_default=False),
]
DebugOption = Annotated[
    bool,
    typer.Option("--debug", help="Show the full traceback if something goes wrong."),
]


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"rowgate {__version__}")
        raise typer.Exit(EXIT_OK)


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


@contextmanager
def report_errors(debug: bool) -> Iterator[None]:
    """Turn any error into a short message on stderr and exit code 2.

    A traceback is printed only with ``--debug``. The exit is raised after the
    except block, so the original exception (and the open DuckDB connection its
    traceback refers to) is released instead of travelling on as context.
    """
    try:
        yield
        return
    except typer.Exit:
        raise
    except RowGateError as exc:
        if debug:
            traceback.print_exc()
            lines = [*exc.debug_lines(), *exc.lines(debug=True)]
        else:
            lines = exc.lines()
    except Exception as exc:  # noqa: BLE001 - any bug must still exit 2 without a traceback
        if debug:
            traceback.print_exc()
        hint = "" if debug else " (run again with --debug to see the traceback)"
        lines = [f"error: internal error: {type(exc).__name__}: {exc}{hint}"]
    for line in lines:
        typer.echo(line, err=True)
    raise typer.Exit(EXIT_ERROR)


@app.command()
def validate(rules: RulesArgument, debug: DebugOption = False) -> None:
    """Check a rules file for mistakes, without reading any data."""
    with report_errors(debug):
        rules_file = load_rules(rules)
        count = len(rules_file.checks)
        noun = "check" if count == 1 else "checks"
        source = rules_file.source
        typer.echo(f"{rules} is valid: {count} {noun} on {source.path} ({source.file_format})")


@app.command()
def check(rules: RulesArgument, debug: DebugOption = False) -> None:
    """Check the data file against the rules and report what fails.

    Exit code 0 if no check with severity error fails, 1 if one does, and 2
    if the rules file or the data file cannot be used.
    """
    exit_code = EXIT_ERROR
    with report_errors(debug):
        report = run_checks(load_rules(rules), rules)
        console = Console(highlight=False, soft_wrap=True, emoji=False)
        for line in report_text.render(report):
            console.print(line)
        exit_code = report.exit_code
    raise typer.Exit(exit_code)


def main() -> None:
    """Console-script entry point."""
    # A console that cannot show a character (common on Windows) must not
    # crash RowGate halfway through its output.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    app(prog_name="rowgate")
