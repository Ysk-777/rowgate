"""What the M2 rules count, and the errors that stop a run before any check."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from rowgate.checks import CheckResult, Report, _count, _run, run_checks
from rowgate.errors import DataError, RowGateError
from rowgate.loader import load_rules
from rowgate.rules import InSet
from rowgate.source import open_dataset

ORDERS = b"""\
order_id,status,amount,note
1001,paid,10,
1002,new,20,gift
1003,paid,,
1003,shipped,30,""
1004,,40,gift
1003,new,50,gift
"""


def run(tmp_path: Path, checks: str, data: bytes = ORDERS, name: str = "data.csv") -> Report:
    data_path = tmp_path / name
    if not data_path.exists():
        data_path.write_bytes(data)
    rules = tmp_path / "rules.yaml"
    rules.write_text(f"version: 1\nsource: {{path: {name}}}\nchecks:\n{checks}", encoding="utf-8")
    return run_checks(load_rules(rules), rules)


def only(report: Report) -> CheckResult:
    [result] = report.results
    return result


# --- not_null --------------------------------------------------------------------------


def test_not_null_counts_empty_fields(tmp_path: Path) -> None:
    result = only(run(tmp_path, "  - {rule: not_null, column: status}\n"))
    assert (result.ok, result.failing_rows) == (False, 1)
    assert result.summary == "1 of 6 rows is NULL"


def test_a_quoted_empty_field_is_not_null(tmp_path: Path) -> None:
    # note: rows 1 and 3 are empty (NULL); row 4 is "" (empty text, not NULL)
    result = only(run(tmp_path, "  - {rule: not_null, column: note}\n"))
    assert result.failing_rows == 2
    assert result.summary == "2 of 6 rows are NULL"


def test_not_null_passes(tmp_path: Path) -> None:
    result = only(run(tmp_path, "  - {rule: not_null, column: order_id}\n"))
    assert (result.ok, result.failing_rows, result.summary) == (True, 0, "no NULL values")


# --- unique --------------------------------------------------------------------------------


def test_unique_counts_every_copy(tmp_path: Path) -> None:
    # 1003 appears three times: three failing rows, not two.
    result = only(run(tmp_path, "  - {rule: unique, column: order_id}\n"))
    assert (result.ok, result.failing_rows) == (False, 3)
    assert result.summary == "3 of 6 rows have a value that appears more than once"


def test_unique_ignores_nulls(tmp_path: Path) -> None:
    # Three NULLs are not three copies of anything.
    result = only(run(tmp_path, "  - {rule: unique, column: v}\n", b"v\nx\n\n\n\ny\n"))
    assert result.failing_rows == 0


def test_unique_counts_repeated_values_around_nulls(tmp_path: Path) -> None:
    # status: paid x2, new x2, shipped, NULL -> four failing rows.
    result = only(run(tmp_path, "  - {rule: unique, column: status}\n"))
    assert result.failing_rows == 4


def test_unique_compares_text_exactly(tmp_path: Path) -> None:
    # Case, spaces and leading zeros all make values different; "" is a value.
    data = b'v\nA\na\n a\n7\n007\n""\n""\n'
    result = only(run(tmp_path, "  - {rule: unique, column: v}\n", data))
    assert result.failing_rows == 2  # only the two empty texts


def test_unique_passes(tmp_path: Path) -> None:
    result = only(run(tmp_path, "  - {rule: unique, column: amount}\n"))
    assert (result.ok, result.summary) == (True, "no repeated values")


# --- row_count -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bounds", "ok", "summary"),
    [
        ("min: 1", True, "6 rows, expected at least 1"),
        ("min: 7", False, "6 rows, expected at least 7"),
        ("max: 6", True, "6 rows, expected at most 6"),
        ("max: 5", False, "6 rows, expected at most 5"),
        ("min: 1, max: 10", True, "6 rows, expected between 1 and 10"),
        ("min: 6, max: 6", True, "6 rows, expected exactly 6"),
        ("min: 0, max: 0", False, "6 rows, expected exactly 0"),
    ],
)
def test_row_count_bounds_are_inclusive(
    tmp_path: Path, bounds: str, ok: bool, summary: str
) -> None:
    result = only(run(tmp_path, f"  - {{rule: row_count, {bounds}}}\n"))
    assert (result.ok, result.failing_rows, result.summary) == (ok, None, summary)


# --- columns_exist ---------------------------------------------------------------------------


def test_columns_exist_passes(tmp_path: Path) -> None:
    result = only(run(tmp_path, "  - {rule: columns_exist, columns: [order_id, status]}\n"))
    assert (result.ok, result.column, result.summary) == (True, None, "all 2 columns present")


def test_columns_exist_lists_what_is_missing_in_rule_order(tmp_path: Path) -> None:
    check = "  - {rule: columns_exist, columns: [zeta, order_id, Status, alpha]}\n"
    result = only(run(tmp_path, check))
    assert not result.ok
    assert result.summary == "3 of 4 columns missing: 'zeta', 'Status', 'alpha'"


def test_columns_exist_failure_is_a_check_failure_not_an_error(tmp_path: Path) -> None:
    report = run(tmp_path, "  - {rule: columns_exist, columns: [missing]}\n")
    assert report.exit_code == 1


# --- a header-only file -------------------------------------------------------------------


def test_header_only_file(tmp_path: Path) -> None:
    checks = (
        "  - {rule: columns_exist, columns: [id]}\n"
        "  - {rule: not_null, column: id}\n"
        "  - {rule: unique, column: id}\n"
        "  - {rule: row_count, min: 1}\n"
        "  - {rule: row_count, max: 0}\n"
    )
    report = run(tmp_path, checks, b"id,name\n")
    assert report.row_count == 0
    assert [r.ok for r in report.results] == [True, True, True, False, True]
    assert report.results[3].summary == "0 rows, expected at least 1"


# --- severity, order and exit codes -----------------------------------------------------------


def test_results_follow_the_rules_file_order(tmp_path: Path) -> None:
    checks = (
        "  - {rule: unique, column: order_id}\n"
        "  - {rule: row_count, min: 1}\n"
        "  - {rule: not_null, column: status, severity: warn}\n"
        "  - {rule: columns_exist, columns: [note]}\n"
    )
    report = run(tmp_path, checks)
    assert [(r.index, r.rule) for r in report.results] == [
        (1, "unique"),
        (2, "row_count"),
        (3, "not_null"),
        (4, "columns_exist"),
    ]
    assert [r.status for r in report.results] == ["failed", "passed", "warned", "passed"]
    assert (report.count("passed"), report.count("failed"), report.count("warned")) == (2, 1, 1)


def test_warnings_never_fail_the_run(tmp_path: Path) -> None:
    report = run(tmp_path, "  - {rule: unique, column: order_id, severity: warn}\n")
    assert only(report).status == "warned"
    assert report.exit_code == 0


def test_a_passing_run_exits_zero(tmp_path: Path) -> None:
    report = run(tmp_path, "  - {rule: not_null, column: order_id}\n")
    assert report.exit_code == 0
    assert (report.source_path, report.file_format, report.row_count, report.column_count) == (
        "data.csv",
        "csv",
        6,
        4,
    )


# --- checks on columns the file does not have --------------------------------------------------


def test_a_missing_column_is_an_error_listing_the_columns(tmp_path: Path) -> None:
    with pytest.raises(DataError) as excinfo:
        run(tmp_path, "  - {rule: row_count, min: 1}\n  - {rule: not_null, column: orderid}\n")
    assert excinfo.value.lines() == [
        "error: a check refers to a column that 'data.csv' does not have",
        "  check 2 (not_null): no column 'orderid' (did you mean 'order_id'?)",
        "  the file has 4 columns: 'order_id', 'status', 'amount', 'note'",
    ]


def test_every_missing_column_is_reported(tmp_path: Path) -> None:
    checks = (
        "  - {rule: unique, column: Status}\n"
        "  - {rule: not_null, column: amount}\n"
        "  - {rule: not_null, column: zzz}\n"
    )
    with pytest.raises(DataError) as excinfo:
        run(tmp_path, checks)
    assert excinfo.value.lines() == [
        "error: 2 checks refer to columns that 'data.csv' does not have",
        "  check 1 (unique): no column 'Status' (did you mean 'status'?)",
        "  check 3 (not_null): no column 'zzz'",
        "  the file has 4 columns: 'order_id', 'status', 'amount', 'note'",
    ]


def test_a_comment_line_read_as_the_header_is_pointed_out(tmp_path: Path) -> None:
    with pytest.raises(DataError) as excinfo:
        run(tmp_path, "  - {rule: not_null, column: id}\n", b"# id\n1\n")
    assert excinfo.value.lines()[-2:] == [
        "  the file has 1 column: '# id'",
        "  the first line starts with '#'; RowGate reads it as the header and does not skip "
        "comment lines",
    ]


def test_rules_from_a_later_milestone_are_refused_before_reading(tmp_path: Path) -> None:
    with pytest.raises(RowGateError) as excinfo:
        run(
            tmp_path,
            "  - {rule: not_null, column: id}\n  - {rule: regex, column: id, pattern: x}\n",
        )
    assert excinfo.value.message == (
        "check 2 (regex): this rule is not available yet in this development version of RowGate"
    )


# --- column names that look like SQL -----------------------------------------------------------


HOSTILE = [
    'a "quoted" name',
    "it's",
    "select",
    'x"; DROP TABLE t; --',
    "日付",
    "sp ace",
    "{source}",
]


def test_unusual_and_hostile_column_names_work(tmp_path: Path) -> None:
    header = ",".join('"' + name.replace('"', '""') + '"' for name in HOSTILE)
    data = (
        header
        + "\n"
        + ",".join(["v"] * len(HOSTILE))
        + "\n"
        + ",".join(["v"] * len(HOSTILE))
        + "\n"
    ).encode()
    checks = "".join(
        f"  - {{rule: not_null, column: '{name.replace(chr(39), chr(39) * 2)}'}}\n"
        f"  - {{rule: unique, column: '{name.replace(chr(39), chr(39) * 2)}'}}\n"
        for name in HOSTILE
    )
    report = run(tmp_path, checks, data)
    assert [(r.column, r.rule, r.failing_rows) for r in report.results] == [
        item for name in HOSTILE for item in ((name, "not_null", 0), (name, "unique", 2))
    ]


# --- Parquet keeps its types -----------------------------------------------------------------


def test_checks_on_parquet(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT * FROM (VALUES (1, 'a'), (2, NULL), (2, 'c'), (NULL, 'd')) t(id, name)) "  # noqa: S608 - test data
        f"TO '{target}' (FORMAT parquet)"
    )
    checks = (
        "  - {rule: not_null, column: id}\n"
        "  - {rule: unique, column: id}\n"
        "  - {rule: not_null, column: name}\n"
        "  - {rule: row_count, min: 4, max: 4}\n"
    )
    report = run(tmp_path, checks, name="data.parquet")
    assert [r.failing_rows for r in report.results] == [1, 2, 1, None]
    assert report.file_format == "parquet"


# --- guards ----------------------------------------------------------------------------------


def test_a_check_without_a_runner_is_an_internal_error(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n")
    rules = load_rules(_write_rules(tmp_path))
    check = InSet(rule="in_set", column="id", values=["1"])
    with (
        open_dataset(tmp_path / "rules.yaml", rules.source) as dataset,
        pytest.raises(AssertionError),
    ):
        _run(1, check, dataset)


def test_a_missing_count_is_a_data_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n")
    rules = load_rules(_write_rules(tmp_path))
    with open_dataset(tmp_path / "rules.yaml", rules.source) as dataset:
        monkeypatch.setattr(dataset, "scalar", lambda sql: None)
        with pytest.raises(DataError):
            _count(dataset, "SELECT 1")


def _write_rules(tmp_path: Path) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: row_count, min: 1}]\n",
        encoding="utf-8",
    )
    return path


def test_checks_can_use_the_positional_name_of_an_unnamed_column(tmp_path: Path) -> None:
    data = b",order_id\n0,1001\n1,1002\n1,1003\n"
    result = only(run(tmp_path, "  - {rule: unique, column: column_1}\n", data))
    assert (result.column, result.failing_rows) == ("column_1", 2)
