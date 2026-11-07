"""The plain-text report printed by ``rowgate check``.

The text is the same for the same input on every run and machine: no
timestamps, no terminal-width wrapping, results in rules-file order. Only the
status words are coloured, and only on a terminal that supports colour.
Column names and sample values are shown with Python's repr quoting, so a
line break, a terminal escape code or rich markup in them is printed escaped,
never interpreted. Sample values are cut to 80 characters.
"""

from __future__ import annotations

from rich.text import Text

from rowgate.checks import SAMPLE_CHARS, CheckResult, Report, Sample, Status

STATUS_WORDS: dict[Status, str] = {"passed": "PASS", "failed": "FAIL", "warned": "WARN"}
STATUS_STYLES: dict[Status, str] = {"passed": "green", "failed": "bold red", "warned": "yellow"}
SAMPLE_INDENT = " " * 8


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
        lines.extend(Text(sample) for sample in sample_lines(result))
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


def sample_lines(result: CheckResult) -> list[str]:
    """The sample rows shown under a failing check, in file order."""
    if not result.samples:
        return []
    lines = []
    for sample in result.samples:
        value = show_value(sample, quoted=result.text_values)
        if sample.count is not None:
            lines.append(f"{SAMPLE_INDENT}row {sample.row}: {value}, appears {sample.count} times")
        else:
            lines.append(f"{SAMPLE_INDENT}row {sample.row}: {value}")
    if result.more:
        noun = "repeated value" if result.rule == "unique" else "row"
        nouns = noun if result.more == 1 else f"{noun}s"
        lines.append(f"{SAMPLE_INDENT}... and {result.more} more {nouns}")
    return lines


def show_value(sample: Sample, quoted: bool) -> str:
    """A sample value, escaped and cut to SAMPLE_CHARS characters.

    Text values are shown in quotes; other values (numbers, dates) in
    DuckDB's text form without quotes, escaped the same way.
    """
    if sample.value is None:
        return "NULL"
    text = sample.value[:SAMPLE_CHARS] if sample.cut else sample.value
    shown = repr(text) if quoted else repr(text)[1:-1]
    if sample.cut:
        shown += f" [cut: {sample.length} characters in all]"
    return shown
