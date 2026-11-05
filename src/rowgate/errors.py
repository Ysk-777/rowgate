"""Exceptions that RowGate reports to the user with exit code 2."""

from __future__ import annotations


class RowGateError(Exception):
    """A problem the user can fix: bad usage, a bad rules file, or unreadable data.

    The CLI prints the message without a traceback and exits with code 2.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def lines(self) -> list[str]:
        """The message as printable lines."""
        return [f"error: {self.message}"]


class RulesError(RowGateError):
    """The rules file cannot be read or does not follow the rules format."""

    def __init__(self, path: str, problems: list[str]) -> None:
        if not problems:
            raise ValueError("RulesError needs at least one problem")
        self.path = path
        self.problems = problems
        count = len(problems)
        noun = "problem" if count == 1 else "problems"
        super().__init__(f"invalid rules file {path!r} ({count} {noun})")

    def lines(self) -> list[str]:
        if len(self.problems) == 1:
            return [f"error: {self.path}: {self.problems[0]}"]
        return [f"error: {self.message}:", *(f"  - {problem}" for problem in self.problems)]
