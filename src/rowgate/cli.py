"""Command-line interface for RowGate."""

from __future__ import annotations

import io
import sys
import traceback
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import typer._click.exceptions as click_exceptions
from rich.console import Console

from rowgate import __version__, report_json, report_text
from rowgate.checks import DEFAULT_SAMPLES, MAX_SAMPLES, run_checks
from rowgate.display import escaped, quoted
from rowgate.errors import RowGateError, UsageError
from rowgate.loader import load_rules
from rowgate.rules import looks_like_url
from rowgate.starter import STARTER_RULES

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
NoSamplesOption = Annotated[
    bool,
    typer.Option(
        "--no-samples",
        help="Show no data values at all: no samples, and none in error messages.",
    ),
]


class OutputFormat(StrEnum):
    """How ``rowgate check`` prints its report."""

    text = "text"
    json = "json"


FormatOption = Annotated[
    OutputFormat,
    typer.Option(
        "--format",
        help="Print the report as text, or as JSON for other programs.",
        case_sensitive=False,
    ),
]
# Read as text and checked by RowGate, so a bad value gets RowGate's own
# "error:" line instead of the usage panel the command-line library prints.
MaxSamplesOption = Annotated[
    str,
    typer.Option(
        "--max-samples",
        metavar="N",
        help=f"Show up to N failing rows per check (1 to {MAX_SAMPLES}).",
    ),
]


def parse_max_samples(text: str) -> int:
    """The value of --max-samples: a whole number from 1 to MAX_SAMPLES."""
    if text.isascii() and text.isdigit() and len(text) <= len(str(MAX_SAMPLES)):
        number = int(text)
        if 1 <= number <= MAX_SAMPLES:
            return number
    shown = text if len(text) <= 20 else text[:17] + "..."
    raise UsageError(
        f"--max-samples must be a whole number from 1 to {MAX_SAMPLES}, got {quoted(shown)}"
    )


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


class ErrorReporter:
    """Writes errors to stderr, as RowGate's "error:" lines or as one JSON object.

    A JSON error is {"error": message, "details": [lines]}, plus "debug":
    [lines] with --debug (DuckDB's own message and the traceback), on one
    line of stderr. Nothing else is written to stderr in JSON mode.
    """

    def __init__(self, as_json: bool = False) -> None:
        self.as_json = as_json

    def write(self, message: str, details: Sequence[str] = (), debug: Sequence[str] = ()) -> None:
        if self.as_json:
            document: dict[str, object] = {"error": message, "details": list(details)}
            if debug:
                document["debug"] = list(debug)
            typer.echo(report_json.dumps(document), err=True)
            return
        for line in debug:
            typer.echo(line, err=True)
        typer.echo(f"error: {message}", err=True)
        for line in details:
            typer.echo(f"  {line}", err=True)

    def usage_error(self, message: str, details: Sequence[str] = ()) -> NoReturn:
        self.write(message, details)
        raise typer.Exit(EXIT_ERROR)


@contextmanager
def report_errors(
    debug: bool, hide_data: bool = False, reporter: ErrorReporter | None = None
) -> Iterator[None]:
    """Turn any error into a short message on stderr and exit code 2.

    A traceback is printed only with ``--debug``. The exit is raised after the
    except block, so the original exception (and the open DuckDB connection its
    traceback refers to) is released instead of travelling on as context.

    With ``hide_data`` (--no-samples), an unexpected error is reported by its
    type only, because its text could hold a data value.
    """
    reporter = reporter or ErrorReporter()
    try:
        yield
        return
    except typer.Exit:
        raise
    except RowGateError as exc:
        trace = traceback.format_exc().splitlines() if debug else []
        if reporter.as_json:
            message, details = exc.json_fields(debug)
            # DuckDB's message can quote data rows: only with --debug.
            reporter.write(message, details, [*exc.debug_lines(), *trace] if debug else [])
        else:
            for line in trace:
                typer.echo(line, err=True)
            lines = [*exc.debug_lines(), *exc.lines(debug=True)] if debug else exc.lines()
            for line in lines:
                typer.echo(line, err=True)
    except Exception as exc:  # noqa: BLE001 - any bug must still exit 2 without a traceback
        trace = traceback.format_exc().splitlines() if debug else []
        hint = "" if debug else " (run again with --debug to see the traceback)"
        detail = "" if hide_data else f": {exc}"
        reporter.write(f"internal error: {type(exc).__name__}{detail}{hint}", debug=trace)
    raise typer.Exit(EXIT_ERROR)


@app.command()
def validate(rules: RulesArgument, debug: DebugOption = False) -> None:
    """Check a rules file for mistakes, without reading any data."""
    with report_errors(debug):
        rules_file = load_rules(rules)
        count = len(rules_file.checks)
        noun = "check" if count == 1 else "checks"
        source = rules_file.source
        typer.echo(
            f"{escaped(str(rules))} is valid: {count} {noun} on {escaped(source.path)} "
            f"({source.file_format})"
        )


# Text, not Path: Path would turn "https://x" into "https:/x" before the
# URL check sees it.
InitPathArgument = Annotated[
    str,
    typer.Argument(metavar="[PATH]", help="Where to write the starter rules file."),
]


@app.command()
def init(path: InitPathArgument = "rowgate.yaml", debug: DebugOption = False) -> None:
    """Write a commented starter rules file. Never overwrites a file."""
    with report_errors(debug):
        write_starter(path)
        shown = escaped(path)
        typer.echo(
            f"created {shown}; set source.path to your data file, then run rowgate validate {shown}"
        )


def write_starter(text: str) -> None:
    """Create the file ``text`` names with the starter rules.

    Refused if anything has that name already, or if its folder is missing.
    """
    shown = quoted(text)
    if looks_like_url(text):
        raise RowGateError(f"{shown} looks like a URL; RowGate writes local files only")
    path = Path(text)
    if path.is_dir():
        example = quoted(str(path / "rowgate.yaml"))
        raise RowGateError(f"{shown} is a folder; give a file name, such as {example}")
    folder = path.parent
    if not folder.is_dir():
        problem = "is a file, not a folder" if folder.exists() else "does not exist"
        raise RowGateError(f"cannot create {shown}: {quoted(str(folder))} {problem}")
    try:
        # "x" creates the file only if nothing has that name, in one step, so
        # an existing file is never overwritten, even one created a moment ago.
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(STARTER_RULES)
    except FileExistsError:
        raise RowGateError(
            f"{shown} already exists; RowGate never overwrites a file, so choose another "
            "name or move that file away"
        ) from None
    except OSError as exc:
        raise RowGateError(f"cannot create {shown}: {exc.strerror or exc}") from None


@app.command()
def check(
    rules: RulesArgument,
    output_format: FormatOption = OutputFormat.text,
    no_samples: NoSamplesOption = False,
    max_samples: MaxSamplesOption = str(DEFAULT_SAMPLES),
    debug: DebugOption = False,
) -> None:
    """Check the data file against the rules and report what fails.

    Exit code 0 if no check with severity error fails, 1 if one does, and 2
    if the rules file or the data file cannot be used.
    """
    as_json = output_format is OutputFormat.json
    reporter = ErrorReporter(as_json)
    try:
        sample_count = parse_max_samples(max_samples)
    except UsageError as exc:
        reporter.usage_error(exc.message)
    if no_samples and debug:
        # --debug can print DuckDB's own messages, which can quote data rows.
        reporter.usage_error(
            "--debug can print data values, so it cannot be used with --no-samples"
        )
    exit_code = EXIT_ERROR
    with report_errors(debug, hide_data=no_samples, reporter=reporter):
        samples = None if no_samples else sample_count
        report = run_checks(load_rules(rules), rules, max_samples=samples)
        if as_json:
            typer.echo(report_json.render(report), nl=False)
        else:
            console = Console(highlight=False, soft_wrap=True, emoji=False)
            for line in report_text.render(report):
                console.print(line)
        exit_code = report.exit_code
    raise typer.Exit(exit_code)


def main(args: list[str] | None = None) -> None:
    """Console-script entry point.

    Usage errors found while reading the command line (a missing argument,
    an unknown option) are reported in RowGate's style too: an "error:"
    line, or a JSON object with --format json, instead of the command-line
    library's usage panel. Exit code 2 either way.
    """
    # A console that cannot show a character (common on Windows) must not
    # crash RowGate halfway through its output.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    arguments = sys.argv[1:] if args is None else args
    command = typer.main.get_command(app)
    try:
        code = command.main(args=arguments, prog_name="rowgate", standalone_mode=False)
    except click_exceptions.NoArgsIsHelpError:
        raise SystemExit(EXIT_ERROR) from None  # the help has been printed
    except click_exceptions.UsageError as exc:
        where = exc.ctx.command_path if exc.ctx is not None else "rowgate"
        reporter = ErrorReporter(_wants_json(arguments))
        reporter.write(exc.format_message(), [f"run '{where} --help' to see the options"])
        raise SystemExit(EXIT_ERROR) from None
    raise SystemExit(code if isinstance(code, int) else EXIT_OK)


def _wants_json(arguments: list[str]) -> bool:
    """Whether the command line asks for JSON output, even if it is wrong elsewhere."""
    pairs = zip(arguments, [*arguments[1:], ""], strict=True)
    return any(
        argument.lower() == "--format=json"
        or (argument == "--format" and following.lower() == "json")
        for argument, following in pairs
    )
