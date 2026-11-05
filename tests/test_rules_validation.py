"""Validation of the rules format: every bad-rules case gets a precise message."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel

from rowgate.errors import RulesError
from rowgate.loader import _choices, _unknown_field, load_rules
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
from support import split_problem

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
            "{rule: in_set, column: answer, values: [true, maybe]}",
            [
                "check 1 (in_set), field 'values': all values must be the same kind, but this "
                "list mixes boolean and text values; true and false are booleans; put them in "
                "quotes, like 'true', to use them as text"
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
            "{rule: in_set, column: d, values: ['yes', 'no']}",
        ),
    )
    in_sets = [check for check in rules.checks if isinstance(check, InSet)]
    assert [check.kind for check in in_sets] == ["text", "number", "boolean", "text"]
    assert in_sets[0].values == ["new", "1", "true"]
    assert in_sets[3].values == ["yes", "no"]  # unquoted yes and no are refused


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


REGEX_PREFIX = "invalid regular expression: "


@pytest.mark.parametrize("pattern", ["'a(b'", "'(?=[A-Z])'", "'(a)\\1'"])
def test_invalid_regex_is_a_rules_error(tmp_path: Path, pattern: str) -> None:
    text = rules_text(f"{{rule: regex, column: sku, pattern: {pattern}}}")
    [problem] = problems(tmp_path, text)
    where, message = split_problem(problem)
    assert where == "check 1 (regex), field 'pattern'"
    assert message.startswith(REGEX_PREFIX)
    assert len(message) > len(REGEX_PREFIX)  # followed by RE2's reason


def test_every_invalid_regex_is_reported(tmp_path: Path) -> None:
    text = rules_text(
        "{rule: regex, column: a, pattern: 'ok+'}",
        "{rule: regex, column: b, pattern: '[z-a]'}",
        "{rule: regex, column: c, pattern: 'x**'}",
    )
    found = [split_problem(problem) for problem in problems(tmp_path, text)]
    assert [where for where, _ in found] == [
        "check 2 (regex), field 'pattern'",
        "check 3 (regex), field 'pattern'",
    ]
    assert all(message.startswith(REGEX_PREFIX) for _, message in found)


@pytest.mark.parametrize(
    ("model", "field", "expected"),
    [
        (NotNull, "severity", "'error' or 'warn'"),
        (Source, "format", "'csv' or 'parquet'"),
        (RulesFile, "version", "1"),
        (NotNull, "column", "an allowed value"),
        (None, "severity", "an allowed value"),
    ],
)
def test_allowed_values_are_written_by_rowgate(
    model: type[BaseModel] | None, field: str, expected: str
) -> None:
    assert _choices(model, field) == expected


# --- aliases ------------------------------------------------------------------------

SELF_REFERENCE = "an alias refers to the list or mapping that contains it"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "version: 1\nsource: {path: a.csv}\nchecks: &a [*a]\n",
            f"check 1: {SELF_REFERENCE}",
        ),
        (
            "version: 1\nsource: {path: a.csv}\nchecks:\n"
            "  - &c {rule: in_set, column: s, values: [ok, *c]}\n",
            f"check 1 (in_set), field 'values', item 2: {SELF_REFERENCE}",
        ),
        (
            "version: 1\nsource: &s {path: a.csv, again: *s}\n"
            "checks: [{rule: unique, column: id}]\n",
            f"field 'source.again': {SELF_REFERENCE}",
        ),
        ("&r {version: 1, again: *r}\n", f"field 'again': {SELF_REFERENCE}"),
        ("&r [*r]\n", f"field '0': {SELF_REFERENCE}"),
    ],
)
def test_recursive_aliases_are_rules_errors(tmp_path: Path, text: str, expected: str) -> None:
    assert problems(tmp_path, text) == [expected]


def test_shared_aliases_are_fine(tmp_path: Path) -> None:
    text = rules_text(
        "{rule: in_set, column: s, values: &v [new, paid]}",
        "{rule: in_set, column: t, values: *v}",
    )
    in_sets = [check for check in load(tmp_path, text).checks if isinstance(check, InSet)]
    assert [check.values for check in in_sets] == [["new", "paid"], ["new", "paid"]]


def test_alias_bomb_is_checked_without_expanding_it(tmp_path: Path) -> None:
    # 10 levels of 10 aliases each would be 10**10 leaves if expanded. The
    # structure walk visits each list once, so this finishes immediately.
    lines = ["version: 1", "source: {path: a.csv}", "checks:"]
    lines.append("  - {rule: in_set, column: s, values: &a0 [x, x, x, x, x, x, x, x, x, x]}")
    for level in range(1, 10):
        refs = ", ".join([f"*a{level - 1}"] * 10)
        lines.append(f"  - {{rule: in_set, column: s{level}, values: &a{level} [{refs}]}}")
    found = problems(tmp_path, "\n".join(lines) + "\n")
    assert len(found) == 9 * 10  # every item of checks 2-10 is a list, not a value
    assert split_problem(found[0])[0] == "check 2 (in_set), field 'values', item 1"


def test_long_alias_chains_do_not_exhaust_the_stack(tmp_path: Path) -> None:
    lines = ["version: 1", "source: {path: a.csv}", "checks:", "  - &c0 [x]"]
    lines += [f"  - &c{n} [*c{n - 1}]" for n in range(1, 2000)]
    found = problems(tmp_path, "\n".join(lines) + "\n")
    assert len(found) == 2000
    assert found[0] == "check 1: expected a mapping of fields, got a list"


# --- unquoted values that YAML 1.1 reads surprisingly --------------------------------------

# Every case below sits at the first item of this check, line 4, column 40.
SURPRISE_CHECK = "{rule: in_set, column: s, values: [%s]}"
WHERE = "check 1 (in_set), field 'values', item 1"
QUOTE = "put it in quotes ('{text}') for text (line 4, column 40)"


def as_number(text: str, number: str) -> str:
    return (
        f"unquoted {text} is read by YAML as the number {number}; write {number} if you mean "
        f"that number, or {QUOTE.format(text=text)}"
    )


def as_boolean(text: str, word: str) -> str:
    return (
        f"unquoted {text} is read by YAML as the boolean {word}; write true or false for a "
        f"boolean, or {QUOTE.format(text=text)}"
    )


def as_text(text: str, number: str) -> str:
    return (
        f"unquoted {text} is read by YAML as text, not as the number {number}; write {number} "
        f"if you mean that number, or {QUOTE.format(text=text)}"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        # integers YAML 1.1 reads in a non-decimal way
        ("0123", as_number("0123", "83")),
        ("010", as_number("010", "8")),
        ("00", as_number("00", "0")),
        ("1:30", as_number("1:30", "90")),
        ("1_000", as_number("1_000", "1000")),
        ("0x1F", as_number("0x1F", "31")),
        ("-0x1F", as_number("-0x1F", "-31")),
        ("0b101", as_number("0b101", "5")),
        # floats with underscores or sexagesimal parts
        ("1_000.5", as_number("1_000.5", "1000.5")),
        ("1:30.5", as_number("1:30.5", "90.5")),
        # booleans other than true and false
        ("yes", as_boolean("yes", "true")),
        ("NO", as_boolean("NO", "false")),
        ("on", as_boolean("on", "true")),
        ("Off", as_boolean("Off", "false")),
        ("True", as_boolean("True", "true")),
        ("FALSE", as_boolean("FALSE", "false")),
        # text in YAML 1.1 that YAML 1.2 and most languages read as numbers
        ("1e3", as_text("1e3", "1000.0")),
        ("1.0e3", as_text("1.0e3", "1000.0")),
        ("+1E-2", as_text("+1E-2", "0.01")),
        ("08", as_text("08", "8")),
        ("-09", as_text("-09", "-9")),
        ("0o17", as_text("0o17", "15")),
    ],
)
def test_surprising_unquoted_values_are_refused(tmp_path: Path, text: str, message: str) -> None:
    assert problems(tmp_path, rules_text(SURPRISE_CHECK % text)) == [f"{WHERE}: {message}"]


@pytest.mark.parametrize(
    ("check", "where"),
    [
        ("{rule: not_null, column: 0123}", "check 1 (not_null), field 'column'"),
        ("{rule: unique, column: id, severity: yes}", "check 1 (unique), field 'severity'"),
        ("{rule: row_count, min: 010}", "check 1 (row_count), field 'min'"),
        ("{rule: range, column: a, max: 1_000}", "check 1 (range), field 'max'"),
        ("{rule: regex, column: a, pattern: 08}", "check 1 (regex), field 'pattern'"),
        (
            "{rule: columns_exist, columns: [id, 1:30]}",
            "check 1 (columns_exist), field 'columns', item 2",
        ),
        ("{rule: on, column: a}", "check 1, field 'rule'"),
    ],
)
def test_surprising_values_are_refused_in_every_field(
    tmp_path: Path, check: str, where: str
) -> None:
    [problem] = problems(tmp_path, rules_text(check))
    assert split_problem(problem)[0] == where
    assert "put it in quotes" in problem


def test_surprising_values_in_version_and_source(tmp_path: Path) -> None:
    text = (
        "version: 0x1\nsource: {path: 1e3, delimiter: on}\nchecks: [{rule: unique, column: id}]\n"
    )
    assert [split_problem(problem)[0] for problem in problems(tmp_path, text)] == [
        "field 'version'",
        "field 'source.path'",
        "field 'source.delimiter'",
    ]


def test_surprising_mapping_keys_are_refused(tmp_path: Path) -> None:
    text = "version: 1\nyes: 1\nsource: {path: a.csv}\nchecks: [{rule: unique, column: id}]\n"
    assert problems(tmp_path, text) == [
        "field 'yes': unquoted yes is read by YAML as the boolean true; write true or false for "
        "a boolean, or put it in quotes ('yes') for text (line 2, column 1)"
    ]


def test_every_surprising_value_is_reported_in_file_order(tmp_path: Path) -> None:
    text = rules_text(
        "{rule: in_set, column: a, values: [ok, yes]}",
        "{rule: not_null, column: 0123}",
        "{rule: in_set, column: b, values: [1_000, 2]}",
    )
    assert [split_problem(problem)[0] for problem in problems(tmp_path, text)] == [
        "check 1 (in_set), field 'values', item 2",
        "check 2 (not_null), field 'column'",
        "check 3 (in_set), field 'values', item 1",
    ]


def test_quoted_forms_keep_their_text(tmp_path: Path) -> None:
    quoted = ["0123", "010", "1:30", "1_000", "0x1F", "yes", "NO", "on", "True", "1e3", "08"]
    values = ", ".join(f"'{text}'" for text in quoted)
    rules = load(tmp_path, rules_text(f"{{rule: in_set, column: s, values: [{values}]}}"))
    [check] = rules.checks
    assert isinstance(check, InSet)
    assert check.values == quoted


def test_plain_numbers_and_lowercase_booleans_are_accepted(tmp_path: Path) -> None:
    rules = load(
        tmp_path,
        rules_text(
            "{rule: in_set, column: n, values: [123, -5, 0, +7, -0, 1.5, .5, 1., 2.5e+3]}",
            "{rule: in_set, column: b, values: [true, false]}",
            "{rule: in_set, column: t, values: [y, n, '=', =, 1.2.3, 12abc]}",
        ),
    )
    in_sets = [check for check in rules.checks if isinstance(check, InSet)]
    assert [check.values for check in in_sets] == [
        [123, -5, 0, 7, 0, 1.5, 0.5, 1.0, 2500.0],
        [True, False],
        ["y", "n", "=", "=", "1.2.3", "12abc"],
    ]


def test_explicit_tags_are_taken_at_their_word(tmp_path: Path) -> None:
    rules = load(
        tmp_path,
        rules_text(
            "{rule: row_count, min: !!int 0x1F}",
            "{rule: in_set, column: s, values: [!!str 0123, !!str yes]}",
        ),
    )
    row_count, in_set = rules.checks
    assert isinstance(row_count, RowCount)
    assert row_count.min == 31
    assert isinstance(in_set, InSet)
    assert in_set.values == ["0123", "yes"]


GLOB_HINT = "DuckDB treats *, ? and [ as file patterns, and reading several files is not supported"


@pytest.mark.parametrize(
    ("path", "found"),
    [
        ("s*.csv", "'*'"),
        ("data/**/orders.csv", "'*'"),
        ("q?.csv", "'?'"),
        ("a[1].csv", "'['"),
        ("[x]/*.parquet", "'*' and '['"),
        ("w?ld[*].csv", "'*' and '?' and '['"),
    ],
)
def test_glob_characters_in_the_source_path_are_refused(
    tmp_path: Path, path: str, found: str
) -> None:
    text = rules_text("{rule: unique, column: id}", source=f"{{path: '{path}'}}")
    assert problems(tmp_path, text) == [
        f"field 'source.path': must not contain {found}; {GLOB_HINT}"
    ]


@pytest.mark.parametrize("path", ["a]1.csv", "a{1,2}.csv", "a(1).csv", "a!.csv", "a#1.csv"])
def test_other_punctuation_in_the_source_path_is_fine(tmp_path: Path, path: str) -> None:
    text = rules_text("{rule: unique, column: id}", source=f"{{path: '{path}'}}")
    assert load(tmp_path, text).source.path == path
