"""--no-samples keeps every data value out of the output; samples follow file order."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from rowgate import cli
from rowgate import source as source_module
from rowgate.checks import run_checks
from rowgate.cli import app
from rowgate.display import quoted
from rowgate.loader import load_rules
from rowgate.rules import Source
from rowgate.source import CsvLayout, open_dataset

MARKER = "MRK-7f3a"  # appears only in data values, never in rules or column names

ALL_RULES = """\
version: 1
source: {{path: {name}}}
checks:
  - {{rule: columns_exist, columns: [id, code, note]}}
  - {{rule: row_count, min: 1, max: 2}}
  - {{rule: not_null, column: note}}
  - {{rule: unique, column: code}}
  - {{rule: in_set, column: code, values: [ok]}}
  - {{rule: range, column: amount, min: 0, max: 10}}
  - {{rule: regex, column: code, pattern: '[a-z]+'}}
"""

CSV = (
    "id,code,note,amount\n"
    f"1,{MARKER},,{MARKER}\n"
    f"2,{MARKER},x,{MARKER}5\n"
    f'3,"{MARKER}\nsecond line",y,99\n'
).encode()


def write(tmp_path: Path, rules: str, data: bytes, name: str = "data.csv") -> Path:
    (tmp_path / name).write_bytes(data)
    path = tmp_path / "rules.yaml"
    path.write_text(rules, encoding="utf-8")
    return path


def cli_output(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout + result.stderr


def process_output(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "rowgate", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "FORCE_COLOR": "1"},  # colour codes must not change anything
    )
    return completed.returncode, completed.stdout + completed.stderr


# --- the marker is visible without --no-samples, so the tests below can see a leak ----------


def test_without_no_samples_the_marker_is_shown(tmp_path: Path) -> None:
    rules = write(tmp_path, ALL_RULES.format(name="data.csv"), CSV)
    code, output = process_output("check", str(rules))
    assert code == 1
    assert MARKER in output


# --- with --no-samples it never appears -------------------------------------------------------


def test_all_rules_failing_on_csv_show_no_data_value(tmp_path: Path) -> None:
    rules = write(tmp_path, ALL_RULES.format(name="data.csv"), CSV)
    code, output = process_output("check", str(rules), "--no-samples")
    assert code == 1
    assert MARKER not in output
    assert "6 failed" in output  # every rule except columns_exist failed


def test_all_rules_failing_on_parquet_show_no_data_value(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT * FROM (VALUES "  # noqa: S608 - test data
        f"(1, '{MARKER}', NULL, '{MARKER}'), (2, '{MARKER}', 'x', '50'), (3, 'z', 'y', '1')"
        f") t(id, code, note, amount)) TO '{target}' (FORMAT parquet)"
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text(ALL_RULES.format(name="data.parquet"), encoding="utf-8")
    with_samples = process_output("check", str(rules))
    code, output = process_output("check", str(rules), "--no-samples")
    assert MARKER in with_samples[1]
    assert code == 1
    assert MARKER not in output


@pytest.mark.parametrize(
    "data",
    [
        f"id,code\n1,ok\n2,{MARKER},extra\n".encode(),  # ragged row
        f'id,code\n1,ok\n2,"{MARKER}"x\n'.encode(),  # stray quote
        f'id,code\n1,ok\n2,"{MARKER}\n'.encode(),  # unclosed quote
        f"id,code\n1,{MARKER}\r\n2,ok\n".encode(),  # mixed line endings
        f"id,code\n1,{MARKER}\n\n2,ok\n".encode(),  # blank line in the middle
        f"id,code\n1,{MARKER}caf\xe9\n".encode("latin-1"),  # not UTF-8
        b"\x1f\x8b" + MARKER.encode(),  # compressed
    ],
    ids=["ragged", "stray-quote", "unclosed-quote", "mixed-endings", "blank-line", "latin1",
         "gzip"],
)  # fmt: skip
def test_unreadable_files_show_no_data_value(tmp_path: Path, data: bytes) -> None:
    rules = write(
        tmp_path,
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: row_count, min: 1}]\n",
        data,
    )
    for extra in ([], ["--no-samples"]):
        code, output = cli_output("check", str(rules), *extra)
        assert code == 2
        assert MARKER not in output


def test_a_record_over_the_limit_shows_no_data_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_module, "MAX_RECORD_BYTES", 50)
    data = f"id,code\n1,{MARKER * 10}\n".encode()
    rules = write(
        tmp_path,
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: row_count, min: 1}]\n",
        data,
    )
    code, output = cli_output("check", str(rules), "--no-samples")
    assert code == 2
    assert MARKER not in output


def test_a_duckdb_failure_shows_no_data_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Skip RowGate's own checks so that DuckDB itself reads the bad line.
    monkeypatch.setattr(source_module, "scan_csv", lambda *args: CsvLayout(["id", "code"], 2, 0))
    data = f'id,code\n1,ok\n2,"{MARKER}"x\n'.encode()
    rules = write(
        tmp_path,
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: row_count, min: 1}]\n",
        data,
    )
    code, output = cli_output("check", str(rules), "--no-samples")
    assert code == 2
    assert MARKER not in output
    # Without --no-samples, only --debug would show DuckDB's message (and the marker).
    _, debug_output = cli_output("check", str(rules), "--debug")
    assert MARKER in debug_output


def test_a_type_mismatch_shows_no_data_value(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        f"COPY (SELECT 1 AS id, '{MARKER}' AS note) TO '{target}' (FORMAT parquet)"
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "version: 1\nsource: {path: data.parquet}\n"
        "checks: [{rule: in_set, column: id, values: [x]}]\n",
        encoding="utf-8",
    )
    code, output = cli_output("check", str(rules), "--no-samples")
    assert code == 2
    assert MARKER not in output


def test_an_internal_error_holding_a_data_value_shows_no_data_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError(f"could not convert {MARKER}")

    monkeypatch.setattr(cli, "run_checks", broken)
    rules = write(tmp_path, ALL_RULES.format(name="data.csv"), CSV)
    code, output = cli_output("check", str(rules), "--no-samples")
    assert code == 2
    assert MARKER not in output
    assert cli_output("check", str(rules))[1].count(MARKER) == 1  # shown without the flag


def test_debug_cannot_be_used_to_get_data_values_with_no_samples(tmp_path: Path) -> None:
    rules = write(tmp_path, ALL_RULES.format(name="data.csv"), CSV)
    code, output = cli_output("check", str(rules), "--no-samples", "--debug")
    assert code == 2
    assert MARKER not in output


def test_no_samples_collects_nothing_from_duckdb(tmp_path: Path) -> None:
    rules = write(tmp_path, ALL_RULES.format(name="data.csv"), CSV)
    report = run_checks(load_rules(rules), rules, max_samples=None)
    assert all(result.samples is None for result in report.results)
    assert MARKER not in repr(report)


# --- samples follow file order ------------------------------------------------------------------


def test_csv_samples_follow_record_order_across_quoted_line_breaks(tmp_path: Path) -> None:
    # 30,000 records; every third holds a line break inside quotes, and every
    # seventh has an empty note (NULL). Row numbers count records, not lines.
    lines = ["id,note"]
    for number in range(1, 30_001):
        if number % 7 == 0:
            lines.append(f"{number},")
        elif number % 3 == 0:
            lines.append(f'{number},"two\nlines {number}"')
        else:
            lines.append(f"{number},one line")
    rules = write(
        tmp_path,
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: not_null, column: note}]\n",
        ("\n".join(lines) + "\n").encode(),
    )
    [result] = run_checks(load_rules(rules), rules, max_samples=1000).results
    expected = [number for number in range(1, 30_001) if number % 7 == 0]
    assert [sample.row for sample in result.samples or ()] == expected[:1000]
    assert result.failing_rows == len(expected)


def test_parquet_samples_follow_row_order_across_row_groups(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT i AS id, CASE WHEN i % 997 = 0 THEN NULL ELSE i END AS v, "  # noqa: S608 - test data
        f"i % 1000 AS code FROM range(1, 200001) t(i)) TO '{target}' "
        "(FORMAT parquet, ROW_GROUP_SIZE 2048)"
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "version: 1\nsource: {path: data.parquet}\nchecks:\n"
        "  - {rule: not_null, column: v}\n  - {rule: unique, column: code}\n",
        encoding="utf-8",
    )
    not_null, unique = run_checks(load_rules(rules), rules, max_samples=1000).results
    expected = [number for number in range(1, 200_001) if number % 997 == 0]
    assert [sample.row for sample in not_null.samples or ()] == expected
    # unique: each code first appears at row code (or 1000 for code 0), in that order.
    firsts = [(sample.row, sample.value, sample.count) for sample in unique.samples or ()]
    assert firsts[:3] == [(1, "1", 200), (2, "2", 200), (3, "3", 200)]
    assert firsts[-1] == (1000, "0", 200)
    assert [row for row, _, _ in firsts] == sorted(row for row, _, _ in firsts)


def test_parquet_rows_are_numbered_by_their_position_in_the_file(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT i AS id FROM range(1, 50001) t(i)) "  # noqa: S608 - test data
        f"TO '{target}' (FORMAT parquet, ROW_GROUP_SIZE 2048)"
    )
    with open_dataset(tmp_path / "rules.yaml", Source(path="data.parquet")) as dataset:
        numbered = dataset.numbered("r", ['"id"'])
        assert "file_row_number" in numbered
        assert "row_number() OVER ()" not in numbered
        found = dataset.row(
            f"SELECT min(r), max(r), count(*) FILTER (WHERE r <> id) FROM {numbered}"  # noqa: S608
        )
    assert found == (1, 50_000, 0)


def test_csv_rows_are_numbered_in_reading_order(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_text("id\n" + "".join(f"{i}\n" for i in range(1, 1001)))
    with open_dataset(tmp_path / "rules.yaml", Source(path="data.csv")) as dataset:
        numbered = dataset.numbered("r", ['"id"'])
        assert "row_number() OVER ()" in numbered
        found = dataset.row(
            f"SELECT count(*) FILTER (WHERE r <> CAST(id AS BIGINT)) FROM {numbered}"  # noqa: S608
        )
    assert found == (0,)


@pytest.mark.parametrize("name", ["file_row_number", "FILE_ROW_NUMBER"])
def test_a_parquet_column_named_file_row_number_cannot_have_samples(
    tmp_path: Path, name: str
) -> None:
    # DuckDB's row numbers would be hidden behind the file's own column.
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        f"COPY (SELECT 7 AS {name}, NULL::INTEGER AS v) TO '{target}' (FORMAT parquet)"
    )
    rules = write(
        tmp_path,
        "version: 1\nsource: {path: data.parquet}\n"
        "checks: [{rule: not_null, column: v}, {rule: unique, column: v}]\n",
        b"",
        name="unused.csv",
    )
    code, output = cli_output("check", str(rules))
    assert code == 2
    assert output == (
        f"error: cannot show samples for 'data.parquet': it has a column named {quoted(name)}, the "
        "name DuckDB gives the row numbers that samples show; run with --no-samples, or rename "
        "the column\n"
    )
    code, output = cli_output("check", str(rules), "--no-samples")
    assert code == 1
    assert "1 of 1 row is NULL" in output


def test_a_csv_column_named_file_row_number_is_an_ordinary_column(tmp_path: Path) -> None:
    rules = write(
        tmp_path,
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: not_null, column: v}]\n",
        b"file_row_number,v\n10,\n20,x\n",
    )
    [result] = run_checks(load_rules(rules), rules).results
    assert [sample.row for sample in result.samples or ()] == [1]
