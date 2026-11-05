"""The plain-text report printed by ``rowgate check``.

The text is the same for the same input on every run and machine: no
timestamps, no terminal-width wrapping, results in rules-file order. Only the
status words are coloured, and only on a terminal that supports colour.
Column names are shown with Python's repr quoting, so a name holding a line
break or a terminal escape code is printed escaped, never interpreted.
"""

from __future__ import annotations

from rich.text import Text

from rowgate.checks import Report, Status

STATUS_WORDS: dict[Status, str] = {"passed": "PASS", "failed": "FAIL", "warned": "WARN"}
STATUS_STYLES: dict[Status, str] = {"passed": "green", "failed": "bold red", "warned": "yellow"}


def render(report: Report) -> list[Text]:
    """The report as lines of text, each a rich Text with styles only on the status word."""
    path = report.source_path if report.source_path.isprintable() else repr(report.source_path)
    lines = [
        Text(
            f"Source: {path} ({report.file_format}, {_plural(report.row_count, 'row')}, "
            f"{_plural(report.column_count, 'column')})"
        ),
        Text(""),
    ]
    width = len(str(len(report.results)))
    for result in report.results:
        line = Text()  # Text(word, style=...) would colour the whole line
        line.append(STATUS_WORDS[result.status], style=STATUS_STYLES[result.status])
        target = "" if result.column is None else f" {result.column!r}"
        line.append(f"  {result.index:>{width}}. {result.rule}{target}: {result.summary}")
        lines.append(line)
    lines.append(Text(""))
    lines.append(
        Text(
            f"{_plural(len(report.results), 'check')}: {report.count('passed')} passed, "
            f"{report.count('failed')} failed, {report.count('warned')} warned"
        )
    )
    return lines


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
