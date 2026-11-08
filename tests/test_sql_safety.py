"""Hostile column names, values, patterns and file names cannot change RowGate's SQL.

Two kinds of proof. Behaviour: checks on hostile names and values give the
right answers, and the output shows them escaped. Structure: every SQL
statement RowGate sends to DuckDB is recorded, and once the quoted column
names are taken out, no text from the rules file or the data path is left
in it; every such text carries the marker HOSTILE, so a leak cannot hide.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from rowgate import database
from rowgate.checks import CheckResult, run_checks
from rowgate.cli import app
from rowgate.errors import DataError
from rowgate.loader import load_rules
from rowgate.sql import quote_identifier

NFC = unicodedata.normalize("NFC", "caf\u00e9")
NFD = unicodedata.normalize("NFD", "caf\u00e9")

# Distinct even ignoring ASCII case, as RowGate requires of column names.
HOSTILE_NAMES = [
    'HOSTILE1" ; drop table y; --',
    "HOSTILE2'; drop table y; --",
    "$rowgate_path",
    "HOSTILE3 $1 ? :name",
    "[bold red]HOSTILE4[/]",
    "\x1b[31mHOSTILE5\x1b[0m",
    'HOSTILE6 "a""b"',
    "select",
    "rowgate_row",
    "rowgate_value_1",
    "rowgate_pieces",
    NFC,
    NFD,
]
HOSTILE_VALUES = [
    "HOSTILE-v1'; drop table y; --",
    'HOSTILE-v2" OR "1"="1',
    "HOSTILE-v3 $rowgate_path",
    "[blink]HOSTILE-v4[/blink]",
    "HOSTILE-v5\x1b]0;title\x07",
]


class Recorder:
    """Wraps a DuckDB connection and keeps every SQL text sent through it."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, statements: list[str]) -> None:
        self._connection = connection
        self._statements = statements

    def execute(self, sql: str, params: object = None) -> duckdb.DuckDBPyConnection:
        self._statements.append(sql)
        return self._connection.execute(sql, params)

    def close(self) -> None:
        self._connection.close()


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    real_connect = database.connect

    def recording_connect(path: Path | None = None) -> object:
        return Recorder(real_connect(path), recorded)

    monkeypatch.setattr(database, "connect", recording_connect)
    return recorded


def without_identifiers(sql: str, names: list[str]) -> str:
    """The SQL with each quoted column name taken out, longest first."""
    for name in sorted(names, key=len, reverse=True):
        sql = sql.replace(quote_identifier(name), "<column>")
    return sql


def assert_no_user_text(statements: list[str], names: list[str]) -> None:
    assert statements
    for sql in statements:
        rest = without_identifiers(sql, names)
        assert "HOSTILE" not in rest, rest
        assert "drop table" not in rest.lower(), rest
        assert NFC not in rest
        assert NFD not in rest


def csv_text(rows: Sequence[Sequence[str | None]]) -> bytes:
    """Write rows as CSV: every text quoted (so "" is empty text), None as an empty field."""

    def field(value: str | None) -> str:
        return "" if value is None else '"' + value.replace('"', '""') + '"'

    return "".join(",".join(field(value) for value in row) + "\n" for row in rows).encode()


def write_rules(folder: Path, data_name: str, checks: list[str]) -> Path:
    rules = folder / "rules.yaml"
    body = "".join(f"  - {check}\n" for check in checks)
    rules.write_text(
        f"version: 1\nsource:\n  path: {yaml_text(data_name)}\n  format: csv\nchecks:\n{body}",
        encoding="utf-8",
    )
    return rules


def yaml_text(text: str) -> str:
    """A YAML double-quoted scalar for any text, with escapes for control characters."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\x{ord(m[0]):02x}", escaped)
    return f'"{escaped}"'


def by_index(results: list[CheckResult]) -> dict[int, CheckResult]:
    return {result.index: result for result in results}


# --- hostile column names -------------------------------------------------------------------


def test_hostile_column_names_are_only_ever_quoted_identifiers(
    tmp_path: Path, statements: list[str]
) -> None:
    # Each column holds its own name, a NULL and a repeat, so every rule has
    # a known answer on every column.
    nulls: list[str | None] = [None] * len(HOSTILE_NAMES)
    rows: list[Sequence[str | None]] = [HOSTILE_NAMES, HOSTILE_NAMES, HOSTILE_NAMES, nulls]
    (tmp_path / "data.csv").write_bytes(csv_text(rows))
    checks = [f"{{rule: columns_exist, columns: [{', '.join(map(yaml_text, HOSTILE_NAMES))}]}}"]
    for name in HOSTILE_NAMES:
        column = yaml_text(name)
        checks += [
            f"{{rule: not_null, column: {column}}}",
            f"{{rule: unique, column: {column}}}",
            f"{{rule: in_set, column: {column}, values: [{yaml_text(name)}]}}",
            f"{{rule: regex, column: {column}, pattern: '.+'}}",
            f"{{rule: range, column: {column}, min: 0}}",
        ]
    rules = write_rules(tmp_path, "data.csv", checks)
    report = run_checks(load_rules(rules), rules, max_samples=5)
    results = by_index(report.results)
    assert results[1].ok  # every hostile name exists
    for position, name in enumerate(HOSTILE_NAMES):
        not_null, unique, in_set, regex, in_range = (
            results[2 + 5 * position + k] for k in range(5)
        )
        assert (not_null.column, not_null.failing_rows) == (name, 1)
        assert unique.failing_rows == 2  # the name twice
        assert in_set.failing_rows == 0
        assert regex.failing_rows == 0
        assert in_range.failing_rows == 2  # a name is not a number
    assert_no_user_text(statements, HOSTILE_NAMES)


def test_a_hostile_name_in_a_missing_column_error_is_escaped(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n")
    name = '\x1b[31m[bold]HOSTILE" ; drop table y; --[/bold]'
    rules = write_rules(tmp_path, "data.csv", [f"{{rule: not_null, column: {yaml_text(name)}}}"])
    with pytest.raises(DataError) as caught:
        run_checks(load_rules(rules), rules)
    assert caught.value.details[0] == (
        "check 1 (not_null): no column '\\x1b[31m[bold]HOSTILE\" ; drop table y; --[/bold]'"
    )


def test_hostile_names_and_values_are_printed_as_text(tmp_path: Path) -> None:
    name = "[bold red]HOSTILE[/] \x1b[2J"
    value = "[link=https://example.invalid]HOSTILE[/link]\x1b[31m"
    (tmp_path / "data.csv").write_bytes(csv_text([[name], [value]]))
    rules = write_rules(
        tmp_path, "data.csv", [f"{{rule: in_set, column: {yaml_text(name)}, values: [ok]}}"]
    )
    result = CliRunner().invoke(app, ["check", str(rules)], env={"FORCE_COLOR": "1"})
    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert (
        "FAIL  1. in_set '[bold red]HOSTILE[/] \\x1b[2J': 1 of 1 row has a value outside the set"
        in [line.replace("\x1b[1;31m", "").replace("\x1b[0m", "") for line in lines]
    )
    assert "        row 1: '[link=https://example.invalid]HOSTILE[/link]\\x1b[31m'" in lines
    # The only escape codes are RowGate's own colour for the status word.
    assert set(re.findall(r"\x1b\[[0-9;]*m", result.stdout)) <= {"\x1b[1;31m", "\x1b[0m"}


# --- hostile values and patterns ------------------------------------------------------------


def test_hostile_values_are_bound_as_parameters(tmp_path: Path, statements: list[str]) -> None:
    (tmp_path / "data.csv").write_bytes(
        csv_text([["v"], *([value] for value in HOSTILE_VALUES), ["other"]])
    )
    members = ", ".join(map(yaml_text, HOSTILE_VALUES))
    rules = write_rules(
        tmp_path,
        "data.csv",
        [
            f"{{rule: in_set, column: v, values: [{members}]}}",
            f"{{rule: in_set, column: v, values: [{yaml_text(HOSTILE_VALUES[0])}]}}",
        ],
    )
    first, second = run_checks(load_rules(rules), rules).results
    assert [sample.value for sample in first.samples or ()] == ["other"]
    assert second.failing_rows == len(HOSTILE_VALUES)  # every value but the first, and other
    assert_no_user_text(statements, ["v"])


@pytest.mark.parametrize(
    "pattern",
    [
        "HOSTILE'.*",
        'HOSTILE" OR 1=1 --.*',
        "HOSTILE.*$rowgate_path",
        "HOSTILE.*'\\); drop table y; --",
        "(?i)hostile.*",
    ],
)
def test_hostile_patterns_are_bound_as_parameters(
    tmp_path: Path, statements: list[str], pattern: str
) -> None:
    values = ["HOSTILE'", 'HOSTILE" OR 1=1 --x', "HOSTILE $rowgate_path", "plain"]
    (tmp_path / "data.csv").write_bytes(csv_text([["v"], *([value] for value in values)]))
    rules = write_rules(
        tmp_path, "data.csv", [f"{{rule: regex, column: v, pattern: {yaml_text(pattern)}}}"]
    )
    [result] = run_checks(load_rules(rules), rules, max_samples=10).results
    # RE2 and Python agree on these patterns: full match, case-sensitive.
    expected = [value for value in values if re.fullmatch(pattern, value) is None]
    assert [sample.value for sample in result.samples or ()] == expected
    assert_no_user_text(statements, ["v"])


def test_a_hostile_delimiter_is_a_parameter(tmp_path: Path, statements: list[str]) -> None:
    (tmp_path / "data.csv").write_bytes(b"a'b\n1'2\n")
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        'version: 1\nsource: {path: data.csv, delimiter: "\'"}\n'
        "checks: [{rule: columns_exist, columns: [a, b]}]\n",
        encoding="utf-8",
    )
    [result] = run_checks(load_rules(rules), rules).results
    assert result.ok
    for sql in statements:
        assert "'''" not in sql  # the delimiter never appears as an SQL string


# --- hostile file names ------------------------------------------------------------------------


FILE_NAMES = [
    "HOSTILE' ; drop table y; --.csv",
    "HOSTILE $rowgate_path.csv",
    "HOSTILE {x} (y) @z #w %v.csv",
    f"HOSTILE {NFC}.csv",
]
if sys.platform != "win32":  # Windows does not allow " or control characters in names
    # A bare escape character and a tab: colour codes contain "[", which RowGate
    # refuses in data paths because DuckDB reads it as a file pattern.
    FILE_NAMES += ['HOSTILE" ; drop table y; --.csv', "HOSTILE \x1b\t.csv"]


@pytest.mark.parametrize("name", FILE_NAMES)
def test_hostile_file_names_are_bound_as_parameters(
    tmp_path: Path, statements: list[str], name: str
) -> None:
    folder = tmp_path / "HOSTILE folder ' ; drop"
    folder.mkdir()
    (folder / name).write_bytes(b"id\n1\n2\n")
    rules = write_rules(folder, name, ["{rule: row_count, min: 2}"])
    report = run_checks(load_rules(rules), rules)
    assert report.results[0].ok
    assert report.source_path == name
    assert_no_user_text(statements, ["id"])


def test_a_hostile_parquet_file_name_is_a_parameter(tmp_path: Path, statements: list[str]) -> None:
    name = "HOSTILE' ; drop table y; --.parquet"
    target = tmp_path / name
    duckdb.connect(":memory:").execute(
        "COPY (SELECT 1 AS id) TO $path (FORMAT parquet)", {"path": str(target)}
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        f"version: 1\nsource: {{path: {yaml_text(name)}}}\n"
        "checks: [{rule: row_count, min: 1}, {rule: not_null, column: id}]\n",
        encoding="utf-8",
    )
    report = run_checks(load_rules(rules), rules)
    assert all(result.ok for result in report.results)
    assert_no_user_text(statements, ["id"])


# --- Unicode normalization ---------------------------------------------------------------------


def test_nfc_and_nfd_names_are_different_columns(tmp_path: Path) -> None:
    # The same word in two Unicode forms: RowGate and DuckDB keep them apart,
    # and never normalize a name from the rules file to find a column.
    assert NFC != NFD
    (tmp_path / "data.csv").write_bytes(csv_text([[NFC, NFD], ["x", None]]))
    rules = write_rules(
        tmp_path,
        "data.csv",
        [
            f"{{rule: not_null, column: {yaml_text(NFC)}}}",
            f"{{rule: not_null, column: {yaml_text(NFD)}}}",
        ],
    )
    nfc_check, nfd_check = run_checks(load_rules(rules), rules).results
    assert (nfc_check.column, nfc_check.failing_rows) == (NFC, 0)
    assert (nfd_check.column, nfd_check.failing_rows) == (NFD, 1)


def test_an_nfc_name_does_not_find_an_nfd_column(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(csv_text([[NFD], ["x"]]))
    rules = write_rules(tmp_path, "data.csv", [f"{{rule: not_null, column: {yaml_text(NFC)}}}"])
    with pytest.raises(DataError) as caught:
        run_checks(load_rules(rules), rules)
    assert caught.value.details[0].startswith(f"check 1 (not_null): no column '{NFC}'")
    assert caught.value.details[-1] == f"the file has 1 column: '{NFD}'"
