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
    _row_alias,
    _samples,
    _type_of,
    run_checks,
)
from rowgate.conditions import row_test, type_family
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


def test_rules_from_a_later_milestone_are_refused_before_reading(tmp_path: Path) -> None:
    with pytest.raises(RowGateError) as excinfo:
        run(
            tmp_path,
            "  - {rule: not_null, column: id}\n  - {rule: in_set, column: id, values: [x]}\n",
        )
    assert excinfo.value.message == (
        "check 2 (in_set): this rule is not available yet in this development version of RowGate"
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


def test_a_rule_without_a_condition_yet_is_an_internal_error() -> None:
    check = InSet(rule="in_set", column="id", values=["1"])
    with pytest.raises(AssertionError):
        row_test(1, check, "VARCHAR")


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
        f"error: a check cannot run on {name!r}",
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
    assert _row_alias(["rowgate_row", "ROWGATE_ROW_"]) == "rowgate_row__"


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
