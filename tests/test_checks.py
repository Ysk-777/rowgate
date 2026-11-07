"""What the M2 rules count, and the errors that stop a run before any check."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from rowgate.checks import (
    CheckResult,
    Report,
    Sample,
    _as_count,
    _samples,
    _type_of,
    run_checks,
)
from rowgate.conditions import Scan, row_test, type_family
from rowgate.display import quoted
from rowgate.errors import DataError
from rowgate.loader import load_rules, parse_rules_yaml
from rowgate.rules import InSet, NotNull, Range, Regex
from rowgate.source import open_dataset
from rowgate.sql import text_as_number

ORDERS = b"""\
order_id,status,amount,note
1001,paid,10,
1002,new,20,gift
1003,paid,,
1003,shipped,30,""
1004,,40,gift
1003,new,50,gift
"""


def run(
    tmp_path: Path,
    checks: str,
    data: bytes = ORDERS,
    name: str = "data.csv",
    max_samples: int | None = 5,
) -> Report:
    data_path = tmp_path / name
    if not data_path.exists():
        data_path.write_bytes(data)
    rules = tmp_path / "rules.yaml"
    rules.write_text(f"version: 1\nsource: {{path: {name}}}\nchecks:\n{checks}", encoding="utf-8")
    return run_checks(load_rules(rules), rules, max_samples=max_samples)


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
        "error: a check cannot run on 'data.csv'",
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
        "error: 2 checks cannot run on 'data.csv'",
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


def test_a_missing_count_is_a_data_error(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n")
    rules = load_rules(_write_rules(tmp_path))
    with open_dataset(tmp_path / "rules.yaml", rules.source) as dataset:
        with pytest.raises(DataError):
            _as_count(None, dataset)
        with pytest.raises(AssertionError):
            _type_of(dataset, "no such column")
        # The queries must read the relation, or DuckDB rejects the unused parameters.
        with pytest.raises(DataError, match="returned no result"):
            dataset.row(f"SELECT 1 FROM {dataset.relation} WHERE false")  # noqa: S608
        with pytest.raises(DataError, match="RowGate could not read"):
            dataset.row(f"SELECT no_such_column FROM {dataset.relation}")  # noqa: S608


def test_unexpected_samples_are_an_internal_error() -> None:
    with pytest.raises(AssertionError):
        _samples("not a list")


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


def test_checks_ignore_trailing_blank_lines_in_a_one_column_file(tmp_path: Path) -> None:
    checks = "  - {rule: not_null, column: v}\n  - {rule: row_count, min: 3, max: 3}\n"
    report = run(tmp_path, checks, b"v\na\nb\nc\n\n\n")
    assert [(r.ok, r.failing_rows) for r in report.results] == [(True, 0), (True, None)]
    assert report.row_count == 3


# --- NaN and signed zero in Parquet ---------------------------------------------------------


@pytest.fixture
def floats_parquet(tmp_path: Path) -> Path:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT * FROM (VALUES "  # noqa: S608 - test data
        "(1.0::DOUBLE, 1.0::FLOAT, 1), ('NaN'::DOUBLE, 'NaN'::FLOAT, 2), "
        "('NaN'::DOUBLE, 'NaN'::FLOAT, 3), (NULL, NULL, 4)) t(d, f, n)) "
        f"TO '{target}' (FORMAT parquet)"
    )
    return target


@pytest.mark.parametrize("column", ["d", "f"], ids=["double", "float"])
def test_nan_is_not_null(tmp_path: Path, floats_parquet: Path, column: str) -> None:
    result = only(run(tmp_path, f"  - {{rule: not_null, column: {column}}}\n", name="data.parquet"))
    assert result.failing_rows == 1  # only the real NULL


@pytest.mark.parametrize("column", ["d", "f"], ids=["double", "float"])
def test_two_nans_are_duplicates(tmp_path: Path, floats_parquet: Path, column: str) -> None:
    result = only(run(tmp_path, f"  - {{rule: unique, column: {column}}}\n", name="data.parquet"))
    assert result.failing_rows == 2


def test_negative_and_positive_zero_are_duplicates(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT * FROM (VALUES (-0.0::DOUBLE), (0.0::DOUBLE)) t(z)) "  # noqa: S608 - test data
        f"TO '{target}' (FORMAT parquet)"
    )
    result = only(run(tmp_path, "  - {rule: unique, column: z}\n", name="data.parquet"))
    assert result.failing_rows == 2


def test_nan_in_csv_is_just_text(tmp_path: Path) -> None:
    result = only(run(tmp_path, "  - {rule: unique, column: v}\n", b"v\nNaN\nnan\nNaN\n"))
    assert result.failing_rows == 2  # the two "NaN" texts; "nan" differs in case


@pytest.mark.parametrize("name", ["data.parquet", "data.csv"])
@pytest.mark.parametrize("folders", ["year=2024", "year=2024/month=01/day"])
def test_a_key_value_folder_is_not_a_column_to_check(
    tmp_path: Path, name: str, folders: str
) -> None:
    # DuckDB's Hive partitioning would add a "year" column from the folder name.
    folder = tmp_path / folders
    folder.mkdir(parents=True)
    if name.endswith(".parquet"):
        duckdb.connect(":memory:").execute(
            f"COPY (SELECT 1 AS id, 'a' AS b, 2 AS c) TO '{folder / name}' (FORMAT parquet)"
        )
    else:
        (folder / name).write_bytes(b"id,b,c\n1,a,2\n")
    with pytest.raises(DataError) as excinfo:
        run(folder, "  - {rule: not_null, column: year}\n", name=name)
    assert excinfo.value.lines() == [
        f"error: a check cannot run on {quoted(name)}",
        "  check 1 (not_null): no column 'year'",
        "  the file has 3 columns: 'id', 'b', 'c'",
    ]


# --- samples ---------------------------------------------------------------------------------


def test_not_null_samples_are_row_numbers_in_file_order(tmp_path: Path) -> None:
    result = only(run(tmp_path, "  - {rule: not_null, column: note}\n"))
    assert result.samples == (
        Sample(row=1, value=None, length=None),
        Sample(row=3, value=None, length=None),
    )
    assert result.more == 0


def test_unique_samples_show_each_repeated_value_once_with_its_count(tmp_path: Path) -> None:
    result = only(run(tmp_path, "  - {rule: unique, column: status}\n"))
    # paid first appears on row 1, new on row 2: ordered by first appearance.
    assert result.samples == (
        Sample(row=1, value="paid", length=4, count=2),
        Sample(row=2, value="new", length=3, count=2),
    )


def test_samples_are_limited_and_the_rest_counted(tmp_path: Path) -> None:
    data = b"v\n" + b"\n".join(b"" if i % 2 else b"x" for i in range(1, 22)) + b"\nend\n"
    report = run(tmp_path, "  - {rule: not_null, column: v}\n", data, max_samples=3)
    result = only(report)
    assert result.failing_rows == 11
    assert [sample.row for sample in result.samples or ()] == [1, 3, 5]
    assert result.more == 8


def test_unique_counts_the_repeated_values_not_shown(tmp_path: Path) -> None:
    data = b"v\n" + b"".join(b"%d\n%d\n" % (i, i) for i in range(10))
    result = only(run(tmp_path, "  - {rule: unique, column: v}\n", data, max_samples=4))
    assert [sample.value for sample in result.samples or ()] == ["0", "1", "2", "3"]
    assert result.more == 6


def test_no_samples_collects_none(tmp_path: Path) -> None:
    checks = "  - {rule: not_null, column: note}\n  - {rule: unique, column: status}\n"
    report = run(tmp_path, checks, max_samples=None)
    assert [(r.failing_rows, r.samples, r.more) for r in report.results] == [
        (2, None, 0),
        (4, None, 0),
    ]


def test_passing_checks_have_no_samples(tmp_path: Path) -> None:
    report = run(tmp_path, "  - {rule: not_null, column: order_id}\n")
    assert only(report).samples == ()


def test_long_values_are_fetched_cut_with_their_length(tmp_path: Path) -> None:
    long = "é" * 500
    data = f"v\n{long}\n{long}\n".encode()
    sample = (only(run(tmp_path, "  - {rule: unique, column: v}\n", data)).samples or ())[0]
    assert sample.length == 500
    assert sample.cut
    assert sample.value == "é" * 81  # only what is needed to show 80 characters


def test_row_numbers_count_records_not_lines(tmp_path: Path) -> None:
    # Quoted line breaks do not start a new row.
    data = b'id,note\n1,"a\nb"\n2,\n3,"c\n\nd"\n4,\n'
    result = only(run(tmp_path, "  - {rule: not_null, column: note}\n", data))
    assert [sample.row for sample in result.samples or ()] == [2, 4]


def test_a_data_column_named_like_the_row_number_is_fine(tmp_path: Path) -> None:
    data = b"rowgate_row,ROWGATE_ROW_\n1,\n1,2\n"
    checks = "  - {rule: not_null, column: ROWGATE_ROW_}\n  - {rule: unique, column: rowgate_row}\n"
    report = run(tmp_path, checks, data)
    assert [(r.failing_rows, [x.row for x in r.samples or ()]) for r in report.results] == [
        (1, [1]),
        (2, [1]),
    ]
    assert Scan(["rowgate_row", "ROWGATE_ROW_"]).new_name("rowgate_row") == '"rowgate_row__"'


def test_parquet_samples_use_duckdb_text_form(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT * FROM (VALUES (1.5::DOUBLE), (1.5::DOUBLE), (1e20::DOUBLE), "  # noqa: S608 - test data
        f"(1e20::DOUBLE)) t(x)) TO '{target}' (FORMAT parquet)"
    )
    result = only(run(tmp_path, "  - {rule: unique, column: x}\n", name="data.parquet"))
    assert [(s.value, s.count) for s in result.samples or ()] == [("1.5", 2), ("1e+20", 2)]
    assert not result.text_values


@pytest.mark.parametrize(
    ("column_type", "family"),
    [
        ("VARCHAR", "text"),
        ("BIGINT", "integer"),
        ("UHUGEINT", "integer"),
        ("DECIMAL(10,2)", "decimal"),
        ("DOUBLE", "float"),
        ("FLOAT", "float"),
        ("BOOLEAN", "boolean"),
        ("DATE", "date"),
        ("TIMESTAMP", "timestamp"),
        ("TIMESTAMP WITH TIME ZONE", "timestamp"),
        ("TIME", "other"),
        ("INTEGER[]", "other"),
        ("STRUCT(a INTEGER)", "other"),
    ],
)
def test_type_families(column_type: str, family: str) -> None:
    assert type_family(column_type) == family


# --- regex ----------------------------------------------------------------------------------


def regex(pattern: str, column: str = "v") -> str:
    return f"  - {{rule: regex, column: {column}, pattern: '{pattern}'}}\n"


def test_regex_must_match_the_whole_value(tmp_path: Path) -> None:
    data = b"v\nABC-1234\nABC-12345\nxABC-1234\nABC-1234\n"
    result = only(run(tmp_path, regex("[A-Z]{3}-[0-9]{4}"), data))
    assert result.failing_rows == 2
    assert [(s.row, s.value) for s in result.samples or ()] == [(2, "ABC-12345"), (3, "xABC-1234")]
    assert result.summary == "2 of 4 rows do not match the pattern"


def test_regex_anchors_change_nothing(tmp_path: Path) -> None:
    data = b"v\nABC-1234\nABC-12345\n"
    plain = only(run(tmp_path, regex("[A-Z]{3}-[0-9]{4}"), data)).failing_rows
    anchored = only(run(tmp_path / ".." / tmp_path.name, regex("^[A-Z]{3}-[0-9]{4}$"), data))
    assert plain == anchored.failing_rows == 1


def test_regex_is_case_sensitive_with_no_flags(tmp_path: Path) -> None:
    data = b"v\nabc\nABC\n"
    assert only(run(tmp_path, regex("abc"), data)).failing_rows == 1
    # RE2's inline flag in the pattern itself is the way to ignore case.
    insensitive = only(run(tmp_path / ".." / tmp_path.name, regex("(?i)abc"), data))
    assert insensitive.failing_rows == 0


def test_regex_ignores_nulls_but_not_empty_text(tmp_path: Path) -> None:
    data = b'v\nok\n\n""\nok\n'
    result = only(run(tmp_path, regex("ok"), data))
    assert result.failing_rows == 1  # the empty text; the NULL is ignored
    assert [(s.row, s.value) for s in result.samples or ()] == [(3, "")]


def test_regex_one_failure_is_singular_and_passes_are_worded(tmp_path: Path) -> None:
    assert only(run(tmp_path, regex("a"), b"v\na\nb\n")).summary == (
        "1 of 2 rows does not match the pattern"
    )
    passed = only(run(tmp_path / ".." / tmp_path.name, regex("[ab]"), b"v\na\nb\n"))
    assert (passed.ok, passed.summary) == (True, "every value matches the pattern")


def test_regex_matches_unicode_characters(tmp_path: Path) -> None:
    data = "v\nCafé\nCafe\n".encode()
    assert only(run(tmp_path, regex("Caf."), data)).failing_rows == 0


@pytest.mark.parametrize(
    ("select", "pattern", "failing_values"),
    [
        # A DOUBLE is matched on DuckDB's text form: 100.0, 1.5 and 1e+20.
        ("100.0::DOUBLE, 1.5::DOUBLE, 1e20::DOUBLE", "[0-9]+[.][0-9]+", ["1e+20"]),
        ("7::INTEGER, 42::INTEGER, -3::INTEGER", "[0-9]+", ["-3"]),
        ("DATE '2024-01-31', DATE '2024-02-01', DATE '2023-12-31'", "2024-.*", ["2023-12-31"]),
        ("true, false, true", "true", ["false"]),
    ],
    ids=["double", "integer", "date", "boolean"],
)
def test_regex_on_non_text_parquet_columns_uses_duckdb_text_form(
    tmp_path: Path, select: str, pattern: str, failing_values: list[str]
) -> None:
    target = tmp_path / "data.parquet"
    rows = ", ".join(f"({value})" for value in select.split(", "))
    duckdb.connect(":memory:").execute(
        f"COPY (SELECT * FROM (VALUES {rows}) t(x)) TO '{target}' (FORMAT parquet)"  # noqa: S608 - test data
    )
    result = only(run(tmp_path, regex(pattern, "x"), name="data.parquet"))
    assert [s.value for s in result.samples or ()] == failing_values
    assert not result.text_values


# --- in_set ---------------------------------------------------------------------------------


def in_set(values: str, column: str = "v") -> str:
    return f"  - {{rule: in_set, column: {column}, values: {values}}}\n"


def failing_values(result: CheckResult) -> list[str | None]:
    return [sample.value for sample in result.samples or ()]


NUMBER_TEXTS = b'v\n1\n1.0\n01\n" 1"\n1e0\n+1\n1.\n-0\n.5\n""\n\nabc\n2\n'


def test_a_number_set_on_text_compares_plain_numbers(tmp_path: Path) -> None:
    # 1, 1.0, 01, 1e0, +1 and 1. are all the number 1; " 1" is not a plain
    # number. The empty text is a value (not a number), the NULL is ignored.
    result = only(run(tmp_path, in_set("[1]"), NUMBER_TEXTS, max_samples=50))
    assert failing_values(result) == [" 1", "-0", ".5", "", "abc", "2"]
    assert result.summary == "6 of 13 rows have a value outside the set"


def test_decimal_set_values_match_any_spelling(tmp_path: Path) -> None:
    result = only(run(tmp_path, in_set("[1.0, 0.5]"), NUMBER_TEXTS, max_samples=50))
    assert failing_values(result) == [" 1", "-0", "", "abc", "2"]


def test_a_text_set_compares_text_exactly(tmp_path: Path) -> None:
    result = only(run(tmp_path, in_set("['1']"), NUMBER_TEXTS, max_samples=50))
    assert failing_values(result) == [
        "1.0", "01", " 1", "1e0", "+1", "1.", "-0", ".5", "", "abc", "2"
    ]  # fmt: skip


def test_text_sets_are_case_sensitive_and_can_hold_the_empty_text(tmp_path: Path) -> None:
    data = b'v\nnew\nNew\n""\n\npaid\n'
    result = only(run(tmp_path, in_set("[new, paid, '']"), data))
    assert failing_values(result) == ["New"]


def test_whole_numbers_are_compared_exactly(tmp_path: Path) -> None:
    data = b"v\n9007199254740992\n9007199254740993\n170141183460469231731687303715884105727\n"
    result = only(
        run(tmp_path, in_set("[9007199254740992, 170141183460469231731687303715884105727]"), data)
    )
    assert failing_values(result) == ["9007199254740993"]  # as a DOUBLE it would match


BOOLEAN_TEXTS = b'v\ntrue\nfalse\nTRUE\nTrue\n1\nyes\nt\n""\n\n'


def test_a_boolean_set_on_text_matches_lowercase_true_and_false_only(tmp_path: Path) -> None:
    result = only(run(tmp_path, in_set("[true]"), BOOLEAN_TEXTS, max_samples=50))
    assert failing_values(result) == ["false", "TRUE", "True", "1", "yes", "t", ""]
    both = only(run(tmp_path / ".." / tmp_path.name, in_set("[true, false]"), BOOLEAN_TEXTS))
    assert both.failing_rows == 6


def test_other_spellings_of_booleans_need_a_text_set(tmp_path: Path) -> None:
    result = only(run(tmp_path, in_set("['TRUE', 'FALSE']"), BOOLEAN_TEXTS, max_samples=50))
    assert failing_values(result) == ["true", "false", "True", "1", "yes", "t", ""]


def test_in_set_one_failure_and_a_pass(tmp_path: Path) -> None:
    assert only(run(tmp_path, in_set("[a]"), b"v\na\nb\n")).summary == (
        "1 of 2 rows has a value outside the set"
    )
    passed = only(run(tmp_path / ".." / tmp_path.name, in_set("[a, b]"), b"v\na\nb\n"))
    assert (passed.ok, passed.summary) == (True, "every value is in the set")


def parquet(tmp_path: Path, values: str, cast: str = "") -> None:
    rows = ", ".join(f"({value}{cast})" for value in values.split(", "))
    duckdb.connect(":memory:").execute(
        f"COPY (SELECT * FROM (VALUES {rows}) t(v)) TO '{tmp_path / 'data.parquet'}' "  # noqa: S608 - test data
        "(FORMAT parquet)"
    )


@pytest.mark.parametrize(
    ("values", "cast", "members", "failing"),
    [
        (
            "1, 2, NULL, 9007199254740993", "::BIGINT", "[1, 9007199254740992]",
            ["2", "9007199254740993"],
        ),
        ("1, 2, 3", "::INTEGER", "[1.0, 2]", ["3"]),
        ("18446744073709551615, 1", "::UBIGINT", "[18446744073709551615]", ["1"]),
        ("1.0, 'nan', 0.1, 'inf'", "::DOUBLE", "[1, 0.1]", ["nan", "inf"]),
        ("1.0, 'nan'", "::FLOAT", "[1]", ["nan"]),
        ("1.50, 2.25", "::DECIMAL(10,2)", "[1.5]", ["2.25"]),
        ("true, false, NULL", "", "[true]", ["false"]),
        ("'a', 'A', '', NULL", "", "[a, '']", ["A"]),
        ("'1', '1.0', 'x'", "", "[1]", ["x"]),
    ],
    ids=["bigint-exact", "int-with-decimal-set", "ubigint", "double-nan-inf", "float-nan",
         "decimal", "boolean", "varchar-text", "varchar-numbers"],
)  # fmt: skip
def test_in_set_on_parquet_types(
    tmp_path: Path, values: str, cast: str, members: str, failing: list[str]
) -> None:
    parquet(tmp_path, values, cast)
    result = only(run(tmp_path, in_set(members), name="data.parquet"))
    assert failing_values(result) == failing


@pytest.mark.parametrize(
    ("values", "cast", "members", "problem"),
    [
        (
            "1, 2", "::INTEGER", "['1']",
            "column 'v' holds numbers (INTEGER), but the values are text; write them as "
            "numbers, without quotes",
        ),
        (
            "1.5", "::DOUBLE", "[true]",
            "column 'v' holds numbers (DOUBLE), but the values are booleans; write them as "
            "numbers, without quotes",
        ),
        (
            "true", "", "['true']",
            "column 'v' holds booleans (BOOLEAN), but the values are text; write true or false",
        ),
        ("true", "", "[1]", "column 'v' holds booleans (BOOLEAN), but the values are numbers; "
         "write true or false"),
        (
            "DATE '2024-01-01'", "", "['2024-01-01']",
            "in_set does not support DATE columns (column 'v')",
        ),
        ("[7]", "", "['[7]']", "in_set does not support INTEGER[] columns (column 'v')"),
    ],
    ids=["text-on-integer", "boolean-on-double", "text-on-boolean", "number-on-boolean",
         "date", "list"],
)  # fmt: skip
def test_a_set_that_does_not_fit_the_column_type_is_exit_2(
    tmp_path: Path, values: str, cast: str, members: str, problem: str
) -> None:
    parquet(tmp_path, values, cast)
    with pytest.raises(DataError) as excinfo:
        run(tmp_path, in_set(members), name="data.parquet")
    assert excinfo.value.lines() == [
        "error: a check cannot run on 'data.parquet'",
        f"  check 1 ({'in_set'}): {problem}",
    ]


def test_type_problems_and_missing_columns_are_listed_together(tmp_path: Path) -> None:
    parquet(tmp_path, "1, 2", "::INTEGER")
    checks = in_set("['1']") + "  - {rule: not_null, column: w}\n"
    with pytest.raises(DataError) as excinfo:
        run(tmp_path, checks, name="data.parquet")
    assert excinfo.value.lines() == [
        "error: 2 checks cannot run on 'data.parquet'",
        "  check 1 (in_set): column 'v' holds numbers (INTEGER), but the values are text; "
        "write them as numbers, without quotes",
        "  check 2 (not_null): no column 'w'",
        "  the file has 1 column: 'v'",
    ]


# --- range ----------------------------------------------------------------------------------


def range_check(bounds: str, column: str = "v") -> str:
    return f"  - {{rule: range, column: {column}, {bounds}}}\n"


def test_whole_numbers_are_compared_exactly_with_whole_bounds(tmp_path: Path) -> None:
    # As a DOUBLE, 9007199254740993 rounds to 9007199254740992 and would pass.
    data = b"v\n9007199254740992\n9007199254740993\n"
    result = only(run(tmp_path, range_check("max: 9007199254740992"), data))
    assert failing_values(result) == ["9007199254740993"]
    assert result.summary == "1 of 2 rows is not a number of at most 9007199254740992"


def test_exact_up_to_39_digits(tmp_path: Path) -> None:
    top = 2**127 - 1
    data = f"v\n{top}\n{top - 1}\n".encode()
    result = only(run(tmp_path, range_check(f"max: {top - 1}"), data))
    assert failing_values(result) == [str(top)]


TEXT_NUMBERS = (
    b'v\n5\n007\n1e2\n-0\n100\n0\n0.5\nnan\ninf\nInfinity\n1e400\n" 5"\n1_000\nabc\n""\n\n'
    + b"1"
    + b"0" * 40
    + b"\n"
    + b"9" * 400
    + b"\n"
)


def test_text_must_be_a_plain_finite_number_in_range(tmp_path: Path) -> None:
    result = only(run(tmp_path, range_check("min: 0, max: 100"), TEXT_NUMBERS, max_samples=50))
    assert failing_values(result) == [
        "nan", "inf", "Infinity", "1e400", " 5", "1_000", "abc", "",
        "1" + "0" * 40,  # a plain number, but over 100 (compared as DOUBLE past 38 digits)
        "9" * 81,  # 400 digits: past the largest DOUBLE, so not finite (81 characters
        # are fetched so that cutting can be detected; 80 are shown)
    ]  # fmt: skip
    assert result.failing_rows == 10  # the NULL is ignored


def test_bounds_are_inclusive(tmp_path: Path) -> None:
    data = b"v\n0\n100\n0.0\n1e2\n-0.0001\n100.0001\n"
    result = only(run(tmp_path, range_check("min: 0, max: 100"), data))
    assert failing_values(result) == ["-0.0001", "100.0001"]


def test_decimal_bounds_compare_as_floating_point(tmp_path: Path) -> None:
    data = b"v\n0.5\n0.49\n1\n"
    result = only(run(tmp_path, range_check("min: 0.5"), data))
    assert failing_values(result) == ["0.49"]


def test_a_range_with_only_one_bound(tmp_path: Path) -> None:
    data = b"v\n-5\n5\n50\n"
    low = only(run(tmp_path, range_check("min: 0"), data))
    high = only(run(tmp_path / ".." / tmp_path.name, range_check("max: 10"), data))
    assert (failing_values(low), low.summary) == (
        ["-5"],
        "1 of 3 rows is not a number of at least 0",
    )
    assert (failing_values(high), high.summary) == (
        ["50"],
        "1 of 3 rows is not a number of at most 10",
    )


def test_a_passing_range(tmp_path: Path) -> None:
    result = only(run(tmp_path, range_check("min: 1, max: 9"), b"v\n1\n9\n"))
    assert (result.ok, result.summary) == (True, "every value is a number from 1 to 9")


def test_dates_in_text_must_be_real_iso_dates(tmp_path: Path) -> None:
    data = b"v\n2024-01-31\n01/02/2024\n2024-02-30\n\n2024-1-31\n2025-01-01\n2024-12-31\n"
    result = only(run(tmp_path, range_check("min: 2024-01-01, max: 2024-12-31"), data))
    assert failing_values(result) == ["01/02/2024", "2024-02-30", "2024-1-31", "2025-01-01"]
    assert result.summary == "4 of 7 rows are not a date from 2024-01-01 to 2024-12-31"


@pytest.mark.parametrize(
    ("values", "cast", "bounds", "failing"),
    [
        ("9007199254740992, 9007199254740993, NULL", "::BIGINT", "max: 9007199254740992",
         ["9007199254740993"]),
        ("1.50, 10.01", "::DECIMAL(10,2)", "max: 10", ["10.01"]),
        ("DATE '2024-01-31', DATE '2025-01-01'", "", "max: 2024-12-31", ["2025-01-01"]),
        ("'5', 'x', '50'", "", "max: 10", ["x", "50"]),
    ],
    ids=["bigint-exact", "decimal", "date", "varchar"],
)  # fmt: skip
def test_range_on_parquet_types(
    tmp_path: Path, values: str, cast: str, bounds: str, failing: list[str]
) -> None:
    parquet(tmp_path, values, cast)
    assert failing_values(only(run(tmp_path, range_check(bounds), name="data.parquet"))) == failing


@pytest.mark.parametrize("cast", ["::DOUBLE", "::FLOAT"])
@pytest.mark.parametrize("bounds", ["min: 0", "max: 10", "min: 0, max: 10"])
def test_nan_and_infinity_fail_any_range(tmp_path: Path, cast: str, bounds: str) -> None:
    # DuckDB orders NaN above every number, so a min-only range would pass it.
    parquet(tmp_path, "1.0, 'nan', 'inf', '-inf', 5.0, NULL", cast)
    result = only(run(tmp_path, range_check(bounds), name="data.parquet"))
    assert failing_values(result) == ["nan", "inf", "-inf"]


@pytest.mark.parametrize(
    ("values", "bounds", "problem"),
    [
        ("DATE '2024-01-31'", "max: 5",
         "column 'v' holds dates (DATE), but the bounds are numbers; write them as dates, "
         "like 2024-01-31"),
        ("5::BIGINT", "max: 2024-12-31",
         "column 'v' holds numbers (BIGINT), but the bounds are dates; write them as numbers"),
        ("TIMESTAMP '2024-01-31 10:00:00'", "max: 2024-12-31",
         "column 'v' is TIMESTAMP; range on timestamps is not supported yet"),
        ("TIMESTAMPTZ '2024-01-31 10:00:00+00'", "max: 5",
         "column 'v' is TIMESTAMP WITH TIME ZONE; range on timestamps is not supported yet"),
        ("true", "max: 1", "range does not support BOOLEAN columns (column 'v')"),
        ("true", "max: 2024-12-31", "range does not support BOOLEAN columns (column 'v')"),
        ("TIME '10:00:00'", "max: 1", "range does not support TIME columns (column 'v')"),
    ],
    ids=[
        "number-on-date", "date-on-bigint", "timestamp", "timestamptz", "boolean",
        "date-on-boolean", "time",
    ],
)  # fmt: skip
def test_bounds_that_do_not_fit_the_column_type_are_exit_2(
    tmp_path: Path, values: str, bounds: str, problem: str
) -> None:
    parquet(tmp_path, values)
    with pytest.raises(DataError) as excinfo:
        run(tmp_path, range_check(bounds), name="data.parquet")
    assert excinfo.value.lines() == [
        "error: a check cannot run on 'data.parquet'",
        f"  check 1 (range): {problem}",
    ]


def test_text_past_the_largest_double_fails_a_min_only_range(tmp_path: Path) -> None:
    # 1e400 is a plain number, but as a DOUBLE it is infinity, which is not a
    # finite number in range even though it is above any minimum.
    data = b"v\n1e400\n" + b"9" * 400 + b"\n5\n"
    result = only(run(tmp_path, range_check("min: 0"), data))
    assert result.failing_rows == 2
    assert [sample.row for sample in result.samples or ()] == [1, 2]


def test_a_large_set_is_matched_with_a_hash_join(tmp_path: Path) -> None:
    # list_contains compares every row with every member: with 1,000 values on
    # 3 million rows it took 12.4 s against 0.4 s for the IN-subquery form.
    members = ", ".join(f"v{number}" for number in range(1000))
    data = b"v\n" + b"".join(b"v%d\n" % number for number in range(0, 2000, 3))
    result = only(run(tmp_path, in_set(f"[{members}]"), data, max_samples=1))
    assert result.failing_rows == sum(1 for number in range(0, 2000, 3) if number >= 1000)
    assert failing_values(result) == ["v1002"]
    for column_type in ("VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN"):
        kind = {"VARCHAR": "[a]", "BIGINT": "[1, 2.5]", "DOUBLE": "[1]", "BOOLEAN": "[true]"}
        check = InSet.model_validate(
            {"rule": "in_set", "column": "v", "values": load_values(kind[column_type])}
        )
        assert "list_contains" not in row_test(1, check, column_type, Scan(["v"])).fails


def load_values(flow: str) -> list[object]:
    loaded = parse_rules_yaml(f"values: {flow}\n", "test")  # numbers as RowGate reads them
    assert isinstance(loaded, dict)
    values = loaded["values"]
    assert isinstance(values, list)
    return values


# --- what the combined scan selects ------------------------------------------------------


def test_the_scan_selects_only_the_columns_the_checks_name() -> None:
    # The quoted name of x (\"x\") is part of the quoted name of x" (\"x\"\"\"),
    # so a search of the SQL text would also select x.
    scan = Scan(["x", 'x"', "unused", "id"])
    row_test(1, NotNull(rule="not_null", column='x"'), "VARCHAR", scan)
    row_test(2, Regex(rule="regex", column="id", pattern="[0-9]+"), "VARCHAR", scan)
    assert scan.select_list() == ['"x"""', '"id"']


def test_text_is_converted_to_a_number_once_per_row() -> None:
    scan = Scan(["v", "w"])
    first = row_test(1, Range(rule="range", column="v", min=0, max=10), "VARCHAR", scan)
    second = row_test(2, Range(rule="range", column="v", min=5), "VARCHAR", scan)
    conversion = text_as_number('"v"')
    assert conversion in scan.computed  # computed once in the scan, for both checks
    for expression, name in scan.computed.items():
        assert expression not in first.fails
        assert expression not in second.fails
        assert name in first.fails


def test_names_for_computed_values_avoid_the_data_columns(tmp_path: Path) -> None:
    data = b"rowgate_row,ROWGATE_VALUE_1,rowgate_value_1_,v\na,b,c,5\nd,e,f,50\ng,h,i,x\n"
    checks = range_check("min: 0, max: 10") + in_set("[1, 5]")
    first, second = run(tmp_path, checks, data).results
    assert [(s.row, s.value) for s in first.samples or ()] == [(2, "50"), (3, "x")]
    assert [(s.row, s.value) for s in second.samples or ()] == [(2, "50"), (3, "x")]
