"""Run the checks of a rules file against its data file.

All row-level checks (not_null, in_set, range, regex) are counted and
sampled together in one scan of the data; each unique check needs one more,
grouped scan. columns_exist and row_count need no scan at all.

SQL safety: every query below is assembled from fixed text written here,
``Dataset.relation`` (fixed text with bound parameters, see rowgate.source),
condition fragments from rowgate.conditions, and column names passed through
``quote_identifier``. Values are bound as parameters. Lines that build SQL
with f-strings carry ``noqa: S608`` for that reason.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rowgate.conditions import RowRule, RowTest, row_test, type_family, type_problem
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

DEFAULT_SAMPLES = 5
MAX_SAMPLES = 1000
SAMPLE_CHARS = 80  # longer values are cut to this many characters
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


@dataclass(frozen=True)
class Sample:
    """One failing row, or for unique one repeated value, as shown to the user."""

    row: int  # 1-based data row; for unique, the first row with the value
    value: str | None  # DuckDB's text form, at most SAMPLE_CHARS + 1 characters
    length: int | None  # characters in the whole value
    count: int | None = None  # unique: how many rows have the value

    @property
    def cut(self) -> bool:
        return self.length is not None and self.length > SAMPLE_CHARS


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
    samples: tuple[Sample, ...] | None = None  # None: not collected, or not row-based
    more: int = 0  # failing rows (or repeated values) beyond the samples
    text_values: bool = True  # samples are text, shown in quotes

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


NOT_AVAILABLE_YET = (Range,)


def run_checks(
    rules: RulesFile, rules_path: Path, max_samples: int | None = DEFAULT_SAMPLES
) -> Report:
    """Open the data file named in the rules and run every check, in order.

    ``max_samples`` is how many failing rows to sample per check; None
    collects none, so no data value leaves DuckDB.
    """
    _refuse_rules_not_available_yet(rules)
    with open_dataset(rules_path, rules.source) as dataset:
        _require_runnable(rules, dataset)
        tests = {
            index: row_test(index, check, _type_of(dataset, check.column))
            for index, check in enumerate(rules.checks, 1)
            if isinstance(check, NotNull | InSet | Range | Regex)
        }
        found = _scan_row_checks(dataset, tests, max_samples)
        results: list[CheckResult] = []
        for index, check in enumerate(rules.checks, 1):
            if isinstance(check, ColumnsExist):
                results.append(_columns_exist(index, check, dataset))
            elif isinstance(check, RowCount):
                results.append(_row_count(index, check, dataset))
            elif isinstance(check, Unique):
                results.append(_unique(index, check, dataset, max_samples))
            else:
                failing, samples = found[index]
                results.append(_row_result(index, check, dataset, failing, samples))
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


def _type_of(dataset: Dataset, name: str) -> str:
    column = dataset.column(name)
    if column is None:  # only possible if _require_runnable was skipped
        raise AssertionError(f"no column {name!r}")
    return column.type


def _require_runnable(rules: RulesFile, dataset: Dataset) -> None:
    """Checks on a missing column, or on a column type they cannot handle, are exit 2."""
    names = dataset.column_names
    problems: list[str] = []
    missing = False
    for index, check in enumerate(rules.checks, 1):
        column = checked_column(check)
        if column is None:
            continue
        found = dataset.column(column)
        if found is None:
            missing = True
            close = difflib.get_close_matches(column, names, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            problems.append(f"check {index} ({check.rule}): no column {column!r}{hint}")
        elif isinstance(check, NotNull | InSet | Range | Regex):
            problem = type_problem(check, found.type)
            if problem is not None:
                problems.append(f"check {index} ({check.rule}): {problem}")
    if not problems:
        return
    count = len(problems)
    message = (
        f"a check cannot run on {dataset.shown!r}"
        if count == 1
        else f"{count} checks cannot run on {dataset.shown!r}"
    )
    details = list(problems)
    if missing:
        listed = ", ".join(repr(name) for name in names)
        details.append(f"the file has {_plural(len(names), 'column')}: {listed}")
        if names and names[0].startswith("#"):
            details.append(
                "the first line starts with '#'; RowGate reads it as the header and does not "
                "skip comment lines"
            )
    raise DataError(message, details)


def _row_alias(names: list[str]) -> str:
    """A name for the row-number column that no data column has (ignoring ASCII case)."""
    taken = {name.translate(_ASCII_LOWER) for name in names}
    alias = "rowgate_row"
    while alias in taken:
        alias += "_"
    return alias


def _sample_struct(row: str, shown: str, extra: str = "") -> str:
    return (
        f"struct_pack(r := {row}, v := left({shown}, {SAMPLE_CHARS + 1}), "
        f"n := length({shown}){extra})"
    )


def _scan_row_checks(
    dataset: Dataset, tests: dict[int, RowTest], max_samples: int | None
) -> dict[int, tuple[int, tuple[Sample, ...] | None]]:
    """Count (and sample) the failing rows of every row-level check in one scan."""
    if not tests:
        return {}
    row = _row_alias(dataset.column_names)
    columns: list[str] = []
    for test in tests.values():
        for name in dataset.column_names:
            quoted = quote_identifier(name)
            if quoted in test.fails and quoted not in columns:
                columns.append(quoted)
    selects: list[str] = []
    params: dict[str, object] = {}
    for test in tests.values():
        selects.append(f"count(*) FILTER (WHERE {test.fails})")
        if max_samples is not None:
            sample = _sample_struct(row, test.shown)
            selects.append(f"min_by({sample}, {row}, $rowgate_samples) FILTER (WHERE {test.fails})")
        params.update(test.params)
    if max_samples is not None:
        params["rowgate_samples"] = max_samples
        inner = (
            f"SELECT row_number() OVER () AS {row}, {', '.join(columns)} FROM {dataset.relation}"  # noqa: S608
        )
    else:
        inner = f"SELECT {', '.join(columns)} FROM {dataset.relation}"  # noqa: S608
    sql = f"SELECT {', '.join(selects)} FROM ({inner}) AS rowgate_data"  # noqa: S608
    values = dataset.row(sql, params)
    step = 1 if max_samples is None else 2
    found: dict[int, tuple[int, tuple[Sample, ...] | None]] = {}
    for position, index in enumerate(tests):
        failing = _as_count(values[position * step], dataset)
        samples = None if max_samples is None else _samples(values[position * step + 1])
        found[index] = (failing, samples)
    return found


def _samples(packed: object) -> tuple[Sample, ...]:
    if packed is None:  # no failing row
        return ()
    if not isinstance(packed, list):
        raise AssertionError(f"unexpected samples {packed!r}")
    return tuple(
        Sample(row=item["r"], value=item["v"], length=item["n"], count=item.get("c"))
        for item in packed
    )


def _as_count(value: object, dataset: Dataset) -> int:
    if not isinstance(value, int):
        raise DataError(f"DuckDB returned no count for {dataset.shown!r}")
    return value


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


def _row_result(
    index: int,
    check: RowRule,
    dataset: Dataset,
    failing: int,
    samples: tuple[Sample, ...] | None,
) -> CheckResult:
    of_rows = f"{failing} of {_plural(dataset.row_count, 'row')}"
    one = failing == 1
    if isinstance(check, InSet):
        summary = (
            f"{of_rows} {'has' if one else 'have'} a value outside the set"
            if failing
            else "every value is in the set"
        )
    elif isinstance(check, Regex):
        summary = (
            f"{of_rows} {'does' if one else 'do'} not match the pattern"
            if failing
            else "every value matches the pattern"
        )
    else:
        summary = f"{of_rows} {'is' if one else 'are'} NULL" if failing else "no NULL values"
    more = failing - len(samples) if samples is not None else 0
    text = type_family(_type_of(dataset, check.column)) == "text"
    return CheckResult(
        index, check.rule, check.column, check.severity, failing == 0, failing, summary,
        samples, more, text,
    )  # fmt: skip


def _unique(index: int, check: Unique, dataset: Dataset, max_samples: int | None) -> CheckResult:
    # Every row whose value appears more than once fails: three copies of a
    # value are three failing rows. NULLs are ignored. One grouped scan.
    column = quote_identifier(check.column)
    text = type_family(_type_of(dataset, check.column)) == "text"
    if max_samples is None:
        sql = (
            "SELECT count(*), coalesce(sum(copies), 0) FROM (SELECT count(*) AS copies "  # noqa: S608
            f"FROM {dataset.relation} WHERE {column} IS NOT NULL "
            f"GROUP BY {column} HAVING count(*) > 1)"
        )
        groups, failing = dataset.row(sql, {})
        samples = None
    else:
        row = _row_alias(dataset.column_names)
        shown = "value" if text else "CAST(value AS VARCHAR)"
        sample = _sample_struct("first_row", shown, ", c := copies")
        sql = (
            f"SELECT count(*), coalesce(sum(copies), 0), "  # noqa: S608
            f"min_by({sample}, first_row, $rowgate_samples) FROM ("
            f"SELECT {column} AS value, count(*) AS copies, min({row}) AS first_row "
            f"FROM (SELECT row_number() OVER () AS {row}, {column} FROM {dataset.relation}) "
            f"AS rowgate_data WHERE {column} IS NOT NULL GROUP BY {column} HAVING count(*) > 1)"
        )
        groups, failing, packed = dataset.row(sql, {"rowgate_samples": max_samples})
        samples = _samples(packed)
    groups = _as_count(groups, dataset)
    failing = _as_count(failing, dataset)
    if failing:
        summary = (
            f"{failing} of {_plural(dataset.row_count, 'row')} have a value that appears "
            "more than once"
        )
    else:
        summary = "no repeated values"
    more = groups - len(samples) if samples is not None else 0
    return CheckResult(
        index, check.rule, check.column, check.severity, failing == 0, failing, summary,
        samples, more, text,
    )  # fmt: skip


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
