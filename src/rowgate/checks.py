"""Run the checks of a rules file against its data file.

SQL safety: every query below is assembled from fixed text written here,
``Dataset.relation`` (fixed text with bound parameters, see rowgate.source)
and column names passed through ``quote_identifier``. No other user text is
placed in SQL; values are bound as parameters. Lines that build SQL with
f-strings carry ``noqa: S608`` for that reason.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rowgate.errors import DataError, RowGateError
from rowgate.rules import (
    Check,
    ColumnsExist,
    InSet,
    NotNull,
    Range,
    Regex,
    RowCount,
    RulesFile,
    Severity,
    Unique,
)
from rowgate.source import Dataset, open_dataset
from rowgate.sql import quote_identifier

Status = Literal["passed", "failed", "warned"]

NOT_AVAILABLE_YET = (InSet, Range, Regex)


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check, in the order the rules file lists them."""

    index: int  # 1-based, as in error messages
    rule: str
    column: str | None
    severity: Severity
    ok: bool
    failing_rows: int | None  # None for checks about the whole file
    summary: str

    @property
    def status(self) -> Status:
        if self.ok:
            return "passed"
        return "failed" if self.severity == "error" else "warned"


@dataclass(frozen=True)
class Report:
    """Everything a run found, ready to be printed."""

    source_path: str
    file_format: str
    row_count: int
    column_count: int
    results: list[CheckResult]

    def count(self, status: Status) -> int:
        return sum(1 for result in self.results if result.status == status)

    @property
    def exit_code(self) -> int:
        """1 if a check with severity error failed, else 0. Warnings never fail a run."""
        return 1 if self.count("failed") else 0


def run_checks(rules: RulesFile, rules_path: Path) -> Report:
    """Open the data file named in the rules and run every check, in order."""
    _refuse_rules_not_available_yet(rules)
    with open_dataset(rules_path, rules.source) as dataset:
        _require_columns(rules, dataset)
        results = [_run(index, check, dataset) for index, check in enumerate(rules.checks, 1)]
        return Report(
            source_path=dataset.shown,
            file_format=dataset.file_format,
            row_count=dataset.row_count,
            column_count=len(dataset.columns),
            results=results,
        )


def checked_column(check: Check) -> str | None:
    """The one column a check looks at, or None for checks about the whole file."""
    if isinstance(check, ColumnsExist | RowCount):
        return None
    return check.column


def _refuse_rules_not_available_yet(rules: RulesFile) -> None:
    for index, check in enumerate(rules.checks, 1):
        if isinstance(check, NOT_AVAILABLE_YET):
            raise RowGateError(
                f"check {index} ({check.rule}): this rule is not available yet in this "
                "development version of RowGate"
            )


def _require_columns(rules: RulesFile, dataset: Dataset) -> None:
    """A check on a column the file does not have is an error (exit 2), not a failure."""
    names = dataset.column_names
    problems: list[str] = []
    for index, check in enumerate(rules.checks, 1):
        column = checked_column(check)
        if column is not None and dataset.column(column) is None:
            close = difflib.get_close_matches(column, names, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            problems.append(f"check {index} ({check.rule}): no column {column!r}{hint}")
    if not problems:
        return
    count = len(problems)
    message = (
        f"a check refers to a column that {dataset.shown!r} does not have"
        if count == 1
        else f"{count} checks refer to columns that {dataset.shown!r} does not have"
    )
    listed = ", ".join(repr(name) for name in names)
    details = [*problems, f"the file has {_plural(len(names), 'column')}: {listed}"]
    if names and names[0].startswith("#"):
        details.append(
            "the first line starts with '#'; RowGate reads it as the header and does not "
            "skip comment lines"
        )
    raise DataError(message, details)


def _run(index: int, check: Check, dataset: Dataset) -> CheckResult:
    if isinstance(check, ColumnsExist):
        return _columns_exist(index, check, dataset)
    if isinstance(check, RowCount):
        return _row_count(index, check, dataset)
    if isinstance(check, NotNull):
        return _not_null(index, check, dataset)
    if isinstance(check, Unique):
        return _unique(index, check, dataset)
    raise AssertionError(f"no runner for rule {check.rule!r}")  # refused before the run


def _columns_exist(index: int, check: ColumnsExist, dataset: Dataset) -> CheckResult:
    missing = [name for name in check.columns if dataset.column(name) is None]
    if missing:
        listed = ", ".join(repr(name) for name in missing)
        summary = f"{len(missing)} of {_plural(len(check.columns), 'column')} missing: {listed}"
    else:
        summary = f"all {_plural(len(check.columns), 'column')} present"
    return CheckResult(index, check.rule, None, check.severity, not missing, None, summary)


def _row_count(index: int, check: RowCount, dataset: Dataset) -> CheckResult:
    rows = dataset.row_count
    ok = (check.min is None or rows >= check.min) and (check.max is None or rows <= check.max)
    if check.min is not None and check.max is not None:
        expected = (
            f"exactly {check.min}"
            if check.min == check.max
            else f"between {check.min} and {check.max}"
        )
    elif check.min is not None:
        expected = f"at least {check.min}"
    else:
        expected = f"at most {check.max}"
    summary = f"{_plural(rows, 'row')}, expected {expected}"
    return CheckResult(index, check.rule, None, check.severity, ok, None, summary)


def _not_null(index: int, check: NotNull, dataset: Dataset) -> CheckResult:
    column = quote_identifier(check.column)
    failing = _count(dataset, f"SELECT count(*) FROM {dataset.relation} WHERE {column} IS NULL")  # noqa: S608
    if failing:
        verb = "is" if failing == 1 else "are"
        summary = f"{failing} of {_plural(dataset.row_count, 'row')} {verb} NULL"
    else:
        summary = "no NULL values"
    return _column_result(index, check, failing, summary)


def _unique(index: int, check: Unique, dataset: Dataset) -> CheckResult:
    # Every row whose value appears more than once fails: three copies of a
    # value are three failing rows. NULLs are ignored.
    column = quote_identifier(check.column)
    sql = (
        f"SELECT coalesce(sum(copies), 0) FROM (SELECT count(*) AS copies "  # noqa: S608
        f"FROM {dataset.relation} WHERE {column} IS NOT NULL "
        f"GROUP BY {column} HAVING count(*) > 1)"
    )
    failing = _count(dataset, sql)
    if failing:
        summary = (
            f"{failing} of {_plural(dataset.row_count, 'row')} have a value that appears "
            "more than once"
        )
    else:
        summary = "no repeated values"
    return _column_result(index, check, failing, summary)


def _column_result(index: int, check: NotNull | Unique, failing: int, summary: str) -> CheckResult:
    return CheckResult(
        index, check.rule, check.column, check.severity, failing == 0, failing, summary
    )


def _count(dataset: Dataset, sql: str) -> int:
    value = dataset.scalar(sql)
    if not isinstance(value, int):
        raise DataError(f"DuckDB returned no count for {dataset.shown!r}")
    return value


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
