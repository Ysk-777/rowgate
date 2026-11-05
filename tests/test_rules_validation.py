"""Validation of the rules format: every bad-rules case gets a precise message."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from rowgate.errors import RulesError
from rowgate.loader import _unknown_field, load_rules
from rowgate.rules import (
    ColumnsExist,
    InSet,
    NotNull,
    Range,
    Regex,
    RowCount,
    RulesFile,
    Source,
    Unique,
    _greater,
)

KNOWN_RULES = "columns_exist, row_count, not_null, unique, in_set, range, regex"


def rules_text(*checks: str, source: str = "{path: data.csv}") -> str:
    """A rules file whose checks are given as one-line YAML flow mappings."""
    lines = ["version: 1", f"source: {source}", "checks:", *(f"  - {check}" for check in checks)]
    return "\n".join(lines) + "\n"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def problems(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(RulesError) as excinfo:
        load_rules(write(tmp_path, text))
    return excinfo.value.problems


def load(tmp_path: Path, text: str) -> RulesFile:
    return load_rules(write(tmp_path, text))


# --- top level ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "- version: 1\n",
            ["the rules file must be a mapping with 'version', 'source' and 'checks', got a list"],
        ),
        (
            "hello\n",
            [
                "the rules file must be a mapping with 'version', 'source' and 'checks', "
                "got string 'hello'"
            ],
        ),
        (
            "source: {path: data.csv}\nchecks: [{rule: unique, column: id}]\n",
            ["field 'version': required field is missing"],
        ),
        (
            "version: 1\nchecks: [{rule: unique, column: id}]\n",
            ["field 'source': required field is missing"],
        ),
        (
            "version: 1\nsource: {path: data.csv}\n",
            ["field 'checks': required field is missing"],
        ),
        (
            "version: 1\nsource: {path: data.csv}\nchecks: []\n",
            ["field 'checks': must contain at least one item"],
        ),
        (
            "version: 1\nsource: {path: data.csv}\nchecks: {rule: unique, column: id}\n",
            ["field 'checks': expected a list, got a mapping"],
        ),
        (
            "version: 1\nsource: {path: data.csv}\nchekcs: [{rule: unique, column: id}]\n",
            [
                "field 'checks': required field is missing",
                "field 'chekcs': unknown field (did you mean 'checks'?)",
            ],
        ),
        (
            "version: 1\nsource: {path: data.csv}\nchecks: [{rule: unique, column: id}]\n"
            "owner: data-team\n",
            ["field 'owner': unknown field (allowed fields: checks, source, version)"],
        ),
    ],
)
def test_top_level_problems(tmp_path: Path, text: str, expected: list[str]) -> None:
    assert problems(tmp_path, text) == expected


@pytest.mark.parametrize(
    ("version", "got"),
    [
        ("2", "integer 2"),
        ("0", "integer 0"),
        ("'1'", "string '1'"),
        ("1.0", "number 1.0"),
        ("true", "boolean true"),
        ("null", "null"),
        ("[1]", "a list"),
    ],
)
def test_version_must_be_the_integer_1(tmp_path: Path, version: str, got: str) -> None:
    text = rules_text("{rule: unique, column: id}").replace("version: 1", f"version: {version}")
    assert problems(tmp_path, text) == [
        f"field 'version': unsupported version; expected 1, got {got}"
    ]


# --- source --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("data.csv", "field 'source': expected a mapping of fields, got string 'data.csv'"),
        ("{}", "field 'source.path': required field is missing"),
        ("{path: 5}", "field 'source.path': expected a string, got integer 5"),
        ("{path: ''}", "field 'source.path': must not be empty"),
        ("{path: '   '}", "field 'source.path': must not be blank"),
        ('{path: "a\\0b.csv"}', "field 'source.path': must not contain a NUL character"),
        (
            "{path: 'https://example.com/data.csv'}",
            "field 'source.path': URLs are not supported; RowGate reads local files only",
        ),
        (
            "{path: 'http://example.com/data.csv'}",
            "field 'source.path': URLs are not supported; RowGate reads local files only",
        ),
        (
            "{path: 's3://bucket/data.parquet'}",
            "field 'source.path': URLs are not supported; RowGate reads local files only",
        ),
        (
            "{path: 'file:///tmp/data.csv'}",
            "field 'source.path': URLs are not supported; RowGate reads local files only",
        ),
        (
            "{path: 'HTTPS://EXAMPLE.COM/DATA.CSV'}",
            "field 'source.path': URLs are not supported; RowGate reads local files only",
        ),
        (
            "{path: data.txt}",
            "field 'source.format': cannot tell the file format from the extension '.txt'; "
            "set format: csv or format: parquet",
        ),
        (
            "{path: data}",
            "field 'source.format': cannot tell the file format from a path without an "
            "extension; set format: csv or format: parquet",
        ),
        (
            "{path: data.csv, format: excel}",
            "field 'source.format': expected 'csv' or 'parquet', got string 'excel'",
        ),
        (
            "{path: data.csv, format: CSV}",
            "field 'source.format': expected 'csv' or 'parquet', got string 'CSV'",
        ),
        (
            "{path: data.csv, delimiter: ';;'}",
            "field 'source.delimiter': must be exactly one character, got string ';;'",
        ),
        (
            "{path: data.csv, delimiter: ''}",
            "field 'source.delimiter': must be exactly one character, got string ''",
        ),
        (
            "{path: data.csv, delimiter: '\"'}",
            "field 'source.delimiter': cannot be a double quote or a line break",
        ),
        (
            '{path: data.csv, delimiter: "\\n"}',
            "field 'source.delimiter': cannot be a double quote or a line break",
        ),
        (
            "{path: data.csv, delimiter: 5}",
            "field 'source.delimiter': expected a string, got integer 5",
        ),
        (
            "{path: data.parquet, delimiter: ';'}",
            "field 'source.delimiter': only applies to csv files",
        ),
        (
            "{path: data.csv, format: parquet, delimiter: ';'}",
            "field 'source.delimiter': only applies to csv files",
        ),
        (
            "{path: data.csv, delim: ';'}",
            "field 'source.delim': unknown field (did you mean 'delimiter'?)",
        ),
    ],
)
def test_source_problems(tmp_path: Path, source: str, expected: str) -> None:
    text = rules_text("{rule: unique, column: id}", source=source)
    assert problems(tmp_path, text) == [expected]


def test_long_values_are_shortened_in_messages(tmp_path: Path) -> None:
    text = rules_text(
        "{rule: unique, column: id}", source="{path: data.csv, format: " + "x" * 60 + "}"
    )
    assert problems(tmp_path, text) == [
        "field 'source.format': expected 'csv' or 'parquet', got string "
        "'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx...'"
    ]


# --- checks in general ------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        ("5", "check 1: expected a mapping of fields, got integer 5"),
        ("not_null", "check 1: expected a mapping of fields, got string 'not_null'"),
        ("{column: id}", f"check 1: missing required field 'rule' (one of: {KNOWN_RULES})"),
        (
            "{rule: not_nul, column: id}",
            "check 1: unknown rule 'not_nul' (did you mean 'not_null'?); "
            f"known rules: {KNOWN_RULES}",
        ),
        ("{rule: freshness}", f"check 1: unknown rule 'freshness'; known rules: {KNOWN_RULES}"),
        ("{rule: 5}", "check 1: field 'rule' must be the name of a rule, got integer 5"),
        ("{rule: null}", "check 1: field 'rule' must be the name of a rule, got null"),
        ("{rule: [unique]}", "check 1: field 'rule' must be the name of a rule, got a list"),
        (
            "{rule: unique, column: id, severity: fatal}",
            "check 1 (unique), field 'severity': expected 'error' or 'warn', got string 'fatal'",
        ),
        (
            "{rule: unique, column: id, severity: ERROR}",
            "check 1 (unique), field 'severity': expected 'error' or 'warn', got string 'ERROR'",
        ),
        (
            "{rule: unique, column: id, severity: 1}",
            "check 1 (unique), field 'severity': expected 'error' or 'warn', got integer 1",
        ),
    ],
)
def test_check_level_problems(tmp_path: Path, check: str, expected: str) -> None:
    assert problems(tmp_path, rules_text(check)) == [expected]


def test_check_numbers_count_from_one_in_file_order(tmp_path: Path) -> None:
    text = rules_text(
        "{rule: unique, column: id}",
        "{rule: not_null}",
        "{rule: unique, column: id}",
        "{rule: rang, column: amount, min: 0}",
    )
    assert problems(tmp_path, text) == [
        "check 2 (not_null), field 'column': required field is missing",
        f"check 4: unknown rule 'rang' (did you mean 'range'?); known rules: {KNOWN_RULES}",
    ]


def test_every_problem_in_the_file_is_reported_together(tmp_path: Path) -> None:
    text = (
        "version: 2\n"
        "source: {path: 'https://example.com/x.csv'}\n"
        "checks:\n"
        "  - {rule: not_null, colum: id}\n"
        "  - {rule: row_count, min: 5, max: 1}\n"
    )
    assert problems(tmp_path, text) == [
        "field 'version': unsupported version; expected 1, got integer 2",
        "field 'source.path': URLs are not supported; RowGate reads local files only",
        "check 1 (not_null), field 'column': required field is missing",
        "check 1 (not_null), field 'colum': unknown field (did you mean 'column'?)",
        "check 2 (row_count): min (5) is greater than max (1)",
    ]


# --- one rule at a time --------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        # columns_exist
        (
            "{rule: columns_exist}",
            ["check 1 (columns_exist), field 'columns': required field is missing"],
        ),
        (
            "{rule: columns_exist, columns: id}",
            ["check 1 (columns_exist), field 'columns': expected a list, got string 'id'"],
        ),
        (
            "{rule: columns_exist, columns: []}",
            ["check 1 (columns_exist), field 'columns': must contain at least one item"],
        ),
        (
            "{rule: columns_exist, columns: [id, 5]}",
            ["check 1 (columns_exist), field 'columns', item 2: expected a string, got integer 5"],
        ),
        (
            "{rule: columns_exist, columns: ['']}",
            ["check 1 (columns_exist), field 'columns', item 1: must not be empty"],
        ),
        (
            "{rule: columns_exist, column: id}",
            [
                "check 1 (columns_exist), field 'columns': required field is missing",
                "check 1 (columns_exist), field 'column': unknown field (did you mean 'columns'?)",
            ],
        ),
        # row_count
        ("{rule: row_count}", ["check 1 (row_count): set at least one of min or max"]),
        (
            "{rule: row_count, min: 10, max: 5}",
            ["check 1 (row_count): min (10) is greater than max (5)"],
        ),
        (
            "{rule: row_count, min: -1}",
            ["check 1 (row_count), field 'min': must be 0 or more, got integer -1"],
        ),
        (
            "{rule: row_count, min: 1.5}",
            ["check 1 (row_count), field 'min': expected a whole number, got number 1.5"],
        ),
        (
            "{rule: row_count, max: true}",
            ["check 1 (row_count), field 'max': expected a whole number, got boolean true"],
        ),
        (
            "{rule: row_count, min: '1'}",
            ["check 1 (row_count), field 'min': expected a whole number, got string '1'"],
        ),
        (
            "{rule: row_count, min: 1, maximum: 5}",
            ["check 1 (row_count), field 'maximum': unknown field (did you mean 'max'?)"],
        ),
        (
            "{rule: row_count, min: 1, column: id}",
            [
                "check 1 (row_count), field 'column': unknown field "
                "(allowed fields: rule, max, min, severity)"
            ],
        ),
        # not_null and unique
        ("{rule: not_null}", ["check 1 (not_null), field 'column': required field is missing"]),
        (
            "{rule: not_null, column: 5}",
            ["check 1 (not_null), field 'column': expected a string, got integer 5"],
        ),
        ("{rule: not_null, column: ''}", ["check 1 (not_null), field 'column': must not be empty"]),
        (
            "{rule: unique, column: [a, b]}",
            ["check 1 (unique), field 'column': expected a string, got a list"],
        ),
        (
            "{rule: unique, colum: id}",
            [
                "check 1 (unique), field 'column': required field is missing",
                "check 1 (unique), field 'colum': unknown field (did you mean 'column'?)",
            ],
        ),
        (
            "{rule: unique, columns: [id]}",
            [
                "check 1 (unique), field 'column': required field is missing",
                "check 1 (unique), field 'columns': unknown field (did you mean 'column'?)",
            ],
        ),
        # in_set
        (
            "{rule: in_set, column: status}",
            ["check 1 (in_set), field 'values': required field is missing"],
        ),
        (
            "{rule: in_set, column: status, values: []}",
            ["check 1 (in_set), field 'values': must contain at least one item"],
        ),
        (
            "{rule: in_set, column: status, values: new}",
            ["check 1 (in_set), field 'values': expected a list, got string 'new'"],
        ),
        (
            "{rule: in_set, column: status, values: [new, null]}",
            [
                "check 1 (in_set), field 'values', item 2: null is not allowed; "
                "in_set always ignores NULL values"
            ],
        ),
        (
            "{rule: in_set, column: status, values: [new, [paid]]}",
            [
                "check 1 (in_set), field 'values', item 2: expected a string, number or "
                "boolean, got a list"
            ],
        ),
        (
            "{rule: in_set, column: status, values: [new, {a: 1}]}",
            [
                "check 1 (in_set), field 'values', item 2: expected a string, number or "
                "boolean, got a mapping"
            ],
        ),
        (
            "{rule: in_set, column: answer, values: [yes, maybe]}",
            [
                "check 1 (in_set), field 'values': all values must be the same kind, but this "
                "list mixes boolean and text values; YAML reads unquoted yes, no, true, false, "
                "on and off as booleans, so quote them to use text"
            ],
        ),
        (
            "{rule: in_set, column: code, values: [1, a]}",
            [
                "check 1 (in_set), field 'values': all values must be the same kind, but this "
                "list mixes number and text values; quote numbers, like '1', to compare them "
                "as text"
            ],
        ),
        (
            "{rule: in_set, column: code, values: [1, true]}",
            [
                "check 1 (in_set), field 'values': all values must be the same kind, but this "
                "list mixes boolean and number values; booleans and numbers are different kinds"
            ],
        ),
        (
            "{rule: in_set, column: code, values: [1, a, true]}",
            [
                "check 1 (in_set), field 'values': all values must be the same kind, but this "
                "list mixes boolean and number and text values; quote numbers, like '1', to "
                "compare them as text"
            ],
        ),
        (
            "{rule: in_set, column: ratio, values: [.nan]}",
            ["check 1 (in_set), field 'values', item 1: must be a finite number, got number nan"],
        ),
        (
            "{rule: in_set, column: ratio, values: [-.inf]}",
            ["check 1 (in_set), field 'values', item 1: must be a finite number, got number -inf"],
        ),
        (
            "{rule: in_set, column: day, values: [2024-01-31]}",
            [
                "check 1 (in_set), field 'values', item 1: dates are not supported here; write "
                "it in quotes, like '2024-01-31', to compare it as text"
            ],
        ),
        # range
        ("{rule: range, column: amount}", ["check 1 (range): set at least one of min or max"]),
        ("{rule: range, min: 0}", ["check 1 (range), field 'column': required field is missing"]),
        (
            "{rule: range, column: amount, min: 10, max: 5}",
            ["check 1 (range): min (10) is greater than max (5)"],
        ),
        (
            "{rule: range, column: amount, min: 0.5, max: 0.25}",
            ["check 1 (range): min (0.5) is greater than max (0.25)"],
        ),
        (
            "{rule: range, column: day, min: 2024-02-01, max: 2024-01-01}",
            ["check 1 (range): min (2024-02-01) is greater than max (2024-01-01)"],
        ),
        (
            "{rule: range, column: day, min: 0, max: 2024-01-01}",
            ["check 1 (range): min and max must both be numbers or both be dates"],
        ),
        (
            "{rule: range, column: amount, min: true}",
            [
                "check 1 (range), field 'min': expected a number or a date, got boolean true",
            ],
        ),
        (
            "{rule: range, column: amount, min: abc}",
            [
                "check 1 (range), field 'min': expected a number or a date (YYYY-MM-DD), "
                "got string 'abc'"
            ],
        ),
        (
            "{rule: range, column: amount, max: '10'}",
            [
                "check 1 (range), field 'max': expected a number or a date (YYYY-MM-DD), "
                "got string '10'"
            ],
        ),
        (
            "{rule: range, column: day, min: '2024-02-30'}",
            ["check 1 (range), field 'min': '2024-02-30' is not a valid calendar date"],
        ),
        (
            "{rule: range, column: day, min: 2024-01-31 10:00:00}",
            [
                "check 1 (range), field 'min': timestamps are not supported; use a date such "
                "as 2024-01-31"
            ],
        ),
        (
            "{rule: range, column: amount, max: .inf}",
            ["check 1 (range), field 'max': must be a finite number, got number inf"],
        ),
        (
            "{rule: range, column: amount, min: [1]}",
            ["check 1 (range), field 'min': expected a number or a date (YYYY-MM-DD), got a list"],
        ),
        (
            "{rule: range, column: amount, minimum: 0}",
            ["check 1 (range), field 'minimum': unknown field (did you mean 'min'?)"],
        ),
        # regex
        (
            "{rule: regex, column: sku}",
            ["check 1 (regex), field 'pattern': required field is missing"],
        ),
        (
            "{rule: regex, column: sku, pattern: ''}",
            ["check 1 (regex), field 'pattern': must not be empty"],
        ),
        (
            "{rule: regex, column: sku, pattern: 5}",
            ["check 1 (regex), field 'pattern': expected a string, got integer 5"],
        ),
        (
            "{rule: regex, column: sku, regex: 'a+'}",
            [
                "check 1 (regex), field 'pattern': required field is missing",
                "check 1 (regex), field 'regex': unknown field "
                "(allowed fields: rule, column, pattern, severity)",
            ],
        ),
    ],
)
def test_rule_problems(tmp_path: Path, check: str, expected: list[str]) -> None:
    assert problems(tmp_path, rules_text(check)) == expected


# --- valid files -----------------------------------------------------------------------

SPEC_EXAMPLE = """\
version: 1
source:
  path: examples/orders.csv      # relative to the rules file
  format: csv                    # optional: csv or parquet, otherwise from extension
  delimiter: ","                 # optional, csv only
checks:
  - rule: columns_exist
    columns: [order_id, status]
  - rule: row_count
    min: 1
    max: 1000000
  - rule: not_null
    column: order_id
  - rule: unique
    column: order_id
  - rule: in_set
    column: status
    values: [new, paid, shipped]
  - rule: range
    column: amount
    min: 0
    max: 10000
    severity: warn
  - rule: regex
    column: sku
    pattern: "^[A-Z]{3}-[0-9]{4}$"
"""


def test_the_documented_example_is_valid(tmp_path: Path) -> None:
    rules = load(tmp_path, SPEC_EXAMPLE)
    assert rules.version == 1
    assert rules.source == Source(path="examples/orders.csv", format="csv", delimiter=",")
    assert rules.checks == [
        ColumnsExist(rule="columns_exist", columns=["order_id", "status"]),
        RowCount(rule="row_count", min=1, max=1000000),
        NotNull(rule="not_null", column="order_id"),
        Unique(rule="unique", column="order_id"),
        InSet(rule="in_set", column="status", values=["new", "paid", "shipped"]),
        Range(rule="range", column="amount", min=0, max=10000, severity="warn"),
        Regex(rule="regex", column="sku", pattern="^[A-Z]{3}-[0-9]{4}$"),
    ]
    assert [check.severity for check in rules.checks] == ["error"] * 5 + ["warn", "error"]


@pytest.mark.parametrize(
    ("source", "file_format"),
    [
        ("{path: data.csv}", "csv"),
        ("{path: DATA.CSV}", "csv"),
        ("{path: nested/dir.v2/data.parquet}", "parquet"),
        ("{path: data.Parquet}", "parquet"),
        ("{path: data.txt, format: csv}", "csv"),
        ("{path: data, format: parquet}", "parquet"),
        ("{path: data.csv, format: parquet}", "parquet"),
        ("{path: data.csv, format: null}", "csv"),
    ],
)
def test_format_is_explicit_or_from_the_extension(
    tmp_path: Path, source: str, file_format: str
) -> None:
    rules = load(tmp_path, rules_text("{rule: unique, column: id}", source=source))
    assert rules.source.file_format == file_format


@pytest.mark.parametrize("delimiter", ["';'", '"\\t"', "'|'", "'§'"])
def test_single_character_delimiters(tmp_path: Path, delimiter: str) -> None:
    source = f"{{path: data.csv, delimiter: {delimiter}}}"
    rules = load(tmp_path, rules_text("{rule: unique, column: id}", source=source))
    assert rules.source.delimiter is not None
    assert len(rules.source.delimiter) == 1


def test_unusual_column_names_are_accepted(tmp_path: Path) -> None:
    names = ["order id", 'say "hi"', "it's", "prix_€", "日付", "select", "drop table x; --"]
    checks = [f"{{rule: not_null, column: {name!r}}}" for name in names]
    rules = load(tmp_path, rules_text(*checks))
    assert [check.column for check in rules.checks if isinstance(check, NotNull)] == names


def test_in_set_value_kinds(tmp_path: Path) -> None:
    rules = load(
        tmp_path,
        rules_text(
            "{rule: in_set, column: a, values: [new, '1', 'true']}",
            "{rule: in_set, column: b, values: [1, 2.5, -3]}",
            "{rule: in_set, column: c, values: [true, false]}",
            "{rule: in_set, column: d, values: [yes, no]}",
        ),
    )
    in_sets = [check for check in rules.checks if isinstance(check, InSet)]
    assert [check.kind for check in in_sets] == ["text", "number", "boolean", "boolean"]
    assert in_sets[0].values == ["new", "1", "true"]
    assert in_sets[3].values == [True, False]


def test_range_bounds(tmp_path: Path) -> None:
    rules = load(
        tmp_path,
        rules_text(
            "{rule: range, column: a, min: 0, max: 10.5}",
            "{rule: range, column: b, max: -1}",
            "{rule: range, column: c, min: 2024-01-01, max: '2024-12-31'}",
            "{rule: range, column: d, min: 5, max: 5}",
        ),
    )
    ranges = [check for check in rules.checks if isinstance(check, Range)]
    assert [(r.min, r.max, r.kind) for r in ranges] == [
        (0, 10.5, "number"),
        (None, -1, "number"),
        (date(2024, 1, 1), date(2024, 12, 31), "date"),
        (5, 5, "number"),
    ]


# --- guards that only matter if validation is bypassed -------------------------------------


def test_unresolved_format_is_an_internal_error() -> None:
    with pytest.raises(AssertionError):
        _ = Source.model_construct(path="data", format=None).file_format


def test_range_without_bounds_is_an_internal_error() -> None:
    with pytest.raises(AssertionError):
        _ = Range.model_construct(rule="range", column="a").kind


def test_unknown_field_message_without_a_model() -> None:
    assert _unknown_field(None, "x") == "unknown field"


def test_explicit_null_delimiter_means_not_set(tmp_path: Path) -> None:
    rules = load(
        tmp_path, rules_text("{rule: unique, column: id}", source="{path: a.csv, delimiter: null}")
    )
    assert rules.source.delimiter is None


def test_other_yaml_types_are_named_in_messages(tmp_path: Path) -> None:
    text = rules_text("{rule: columns_exist, columns: !!binary aGk=}")
    assert problems(tmp_path, text) == [
        "check 1 (columns_exist), field 'columns': expected a list, got a value of type bytes"
    ]


def test_bounds_of_different_kinds_are_never_compared() -> None:
    with pytest.raises(TypeError):
        _greater(1, date(2024, 1, 1))
