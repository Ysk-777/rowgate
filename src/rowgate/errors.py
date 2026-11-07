"""Exceptions that RowGate reports to the user with exit code 2."""

from __future__ import annotations

from rowgate.display import escaped, quoted

DEBUG_HINT = (
    "run again with --debug to see DuckDB's own message, which can include data values "
    "from the file"
)


class RowGateError(Exception):
    """A problem the user can fix: bad usage, a bad rules file, or unreadable data.

    The CLI prints the message without a traceback and exits with code 2.

    ``duckdb_message`` is DuckDB's original error text, shown only with
    ``--debug`` because it can quote rows of the data file. ``duckdb_summary``
    is a safe line from it (no row content, no absolute paths), shown after
    the message unless the full text is printed instead, so that DuckDB's
    words appear once.
    """

    def __init__(
        self,
        message: str,
        *,
        duckdb_message: str | None = None,
        duckdb_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.duckdb_message = duckdb_message
        self.duckdb_summary = duckdb_summary

    def lines(self, debug: bool = False) -> list[str]:
        """The message as printable lines."""
        return [f"error: {self._text(debug)}"]

    def debug_lines(self) -> list[str]:
        """Extra lines printed with --debug: DuckDB's own message, if there is one."""
        if self.duckdb_message is None:
            return []
        return [
            "DuckDB's original message (it can include data values from the file):",
            *(f"  {line}" for line in self.duckdb_message.splitlines()),
        ]

    def _text(self, debug: bool) -> str:
        text = self.message
        full_text_shown = debug and self.duckdb_message is not None
        if self.duckdb_summary is not None and not full_text_shown:
            text += f": {self.duckdb_summary}"
        if self.duckdb_message is not None and not debug:
            text += f"; {DEBUG_HINT}"
        return text


class RulesError(RowGateError):
    """The rules file cannot be read or does not follow the rules format."""

    def __init__(self, path: str, problems: list[str]) -> None:
        if not problems:
            raise ValueError("RulesError needs at least one problem")
        self.path = path
        self.problems = problems
        count = len(problems)
        noun = "problem" if count == 1 else "problems"
        super().__init__(f"invalid rules file {quoted(path)} ({count} {noun})")

    def lines(self, debug: bool = False) -> list[str]:
        if len(self.problems) == 1:
            return [f"error: {escaped(self.path)}: {self.problems[0]}"]
        return [f"error: {self.message}:", *(f"  - {problem}" for problem in self.problems)]


class DataError(RowGateError):
    """The data file is missing, unreadable, or does not fit the rules."""

    def __init__(
        self,
        message: str,
        details: list[str] | None = None,
        *,
        duckdb_message: str | None = None,
        duckdb_summary: str | None = None,
    ) -> None:
        super().__init__(message, duckdb_message=duckdb_message, duckdb_summary=duckdb_summary)
        self.details = details or []

    def lines(self, debug: bool = False) -> list[str]:
        return [f"error: {self._text(debug)}", *(f"  {detail}" for detail in self.details)]


class UsageError(RowGateError):
    """A command-line option has a value RowGate cannot use."""
