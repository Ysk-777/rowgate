"""The ``rowgate check`` command: output, exit codes and error handling."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rowgate import cli
from rowgate import source as source_module
from rowgate.cli import app
from rowgate.display import quoted
from rowgate.source import CsvLayout

ORDERS = b"order_id,status,amount\n1001,paid,10\n1002,new,\n1003,paid,30\n1003,shipped,40\n"

RULES = """\
version: 1
source:
  path: orders.csv
checks:
  - rule: columns_exist
    columns: [order_id, status]
  - rule: row_count
    min: 1
  - rule: not_null
    column: amount
    severity: warn
  - rule: unique
    column: order_id
"""


def project(tmp_path: Path, rules: str = RULES, data: bytes = ORDERS) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "orders.csv").write_bytes(data)
    path = tmp_path / "rules.yaml"
    path.write_text(rules, encoding="utf-8")
    return path


def run(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    result = CliRunner(env=env).invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


def test_a_failing_check_exits_with_code_1(tmp_path: Path) -> None:
    code, out, err = run("check", str(project(tmp_path)))
    assert (code, err) == (1, "")
    assert out == (
        "Source: orders.csv (csv, 4 rows, 3 columns)\n"
        "\n"
        "PASS  1. columns_exist: all 2 columns present\n"
        "PASS  2. row_count: 4 rows, expected at least 1\n"
        "WARN  3. not_null 'amount': 1 of 4 rows is NULL\n"
        "        row 2: NULL\n"
        "FAIL  4. unique 'order_id': 2 of 4 rows have a value that appears more than once\n"
        "        row 3: '1003', appears 2 times\n"
        "\n"
        "4 checks: 2 passed, 1 failed, 1 warned\n"
    )


def test_warnings_alone_exit_with_code_0(tmp_path: Path) -> None:
    rules = RULES.replace("column: order_id", "column: order_id\n    severity: warn")
    code, out, _ = run("check", str(project(tmp_path, rules)))
    assert code == 0
    assert out.endswith("4 checks: 2 passed, 0 failed, 2 warned\n")


def test_all_passing_exits_with_code_0(tmp_path: Path) -> None:
    data = b"order_id,status,amount\n1,paid,10\n2,new,20\n"
    code, out, _ = run("check", str(project(tmp_path, data=data)))
    assert code == 0
    assert out.endswith("4 checks: 4 passed, 0 failed, 0 warned\n")


def test_output_is_the_same_on_every_run(tmp_path: Path) -> None:
    rules = project(tmp_path)
    assert run("check", str(rules)) == run("check", str(rules))


def test_check_numbers_line_up_past_nine_checks(tmp_path: Path) -> None:
    checks = "".join("  - {rule: row_count, min: 1}\n" for _ in range(10))
    rules = f"version: 1\nsource: {{path: orders.csv}}\nchecks:\n{checks}"
    _, out, _ = run("check", str(project(tmp_path, rules)))
    lines = out.splitlines()
    assert lines[2] == "PASS   1. row_count: 4 rows, expected at least 1"
    assert lines[11] == "PASS  10. row_count: 4 rows, expected at least 1"


def test_a_missing_column_exits_with_code_2(tmp_path: Path) -> None:
    rules = RULES.replace("column: order_id", "column: orderid")
    code, out, err = run("check", str(project(tmp_path, rules)))
    assert (code, out) == (2, "")
    assert err == (
        "error: a check cannot run on 'orders.csv'\n"
        "  check 4 (unique): no column 'orderid' (did you mean 'order_id'?)\n"
        "  the file has 3 columns: 'order_id', 'status', 'amount'\n"
    )


def test_a_missing_data_file_exits_with_code_2(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES, encoding="utf-8")
    code, out, err = run("check", str(rules))
    assert (code, out) == (2, "")
    assert err == (
        "error: data file 'orders.csv' not found, relative to the rules file "
        f"{quoted(str(rules))}\n"
    )


def test_a_parquet_error_with_debug_prints_duckdb_words_once(tmp_path: Path) -> None:
    (tmp_path / "orders.parquet").write_bytes(b"order_id\n1\n")
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES.replace("orders.csv", "orders.parquet"), encoding="utf-8")
    _, _, plain = run("check", str(rules))
    code, _, err = run("check", str(rules), "--debug")
    assert code == 2
    summary = plain.removeprefix("error: cannot read 'orders.parquet' as Parquet: ").strip()
    assert summary
    assert err.count(summary) == 1
    assert err.endswith(f"error: cannot read 'orders.parquet' as Parquet: {summary}\n")
    assert str(tmp_path) not in plain


def test_unreadable_data_exits_with_code_2(tmp_path: Path) -> None:
    code, out, err = run("check", str(project(tmp_path, data=b"order_id,status\n1,a,extra\n")))
    assert (code, out) == (2, "")
    assert err == "error: line 2 of 'orders.csv' has 3 fields, but the header has 2 fields\n"


def test_a_bad_rules_file_exits_with_code_2(tmp_path: Path) -> None:
    code, out, err = run("check", str(project(tmp_path, RULES.replace("min: 1", "min: -1"))))
    assert (code, out) == (2, "")
    assert err.endswith("check 2 (row_count), field 'min': must be 0 or more, got integer -1\n")


def test_internal_errors_exit_with_code_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_checks", broken)
    code, out, err = run("check", str(project(tmp_path)))
    assert (code, out) == (2, "")
    assert err == (
        "error: internal error: RuntimeError: boom (run again with --debug to see the traceback)\n"
    )


def test_debug_shows_the_traceback_of_a_data_error(tmp_path: Path) -> None:
    code, _, err = run("check", str(project(tmp_path, data=b"")), "--debug")
    assert code == 2
    assert "Traceback (most recent call last)" in err
    assert "rowgate.errors.DataError" in err
    assert err.endswith("error: 'orders.csv' is empty (0 bytes)\n")


def test_status_words_are_coloured_only_on_a_colour_terminal(tmp_path: Path) -> None:
    rules = str(project(tmp_path))
    _, plain, _ = run("check", rules, env={"NO_COLOR": "1", "FORCE_COLOR": ""})
    _, coloured, _ = run("check", rules, env={"FORCE_COLOR": "1", "NO_COLOR": ""})
    assert "\x1b[" not in plain
    assert "\x1b[1;31mFAIL\x1b[0m  4. unique" in coloured
    assert "\x1b[32mPASS\x1b[0m  1. columns_exist" in coloured
    assert "\x1b[33mWARN\x1b[0m  3. not_null" in coloured
    assert (
        coloured.replace("\x1b[1;31m", "")
        .replace("\x1b[32m", "")
        .replace("\x1b[33m", "")
        .replace("\x1b[0m", "")
        == plain
    )  # only the status words are styled


def test_names_with_control_characters_are_printed_escaped(tmp_path: Path) -> None:
    data = b'"line\nbreak","\x1b[31mred"\n1,2\n'
    rules = (
        "version: 1\nsource: {path: orders.csv}\nchecks:\n"
        '  - {rule: not_null, column: "line\\nbreak"}\n'
        '  - {rule: not_null, column: "\\e[31mred"}\n'
    )
    _, out, _ = run("check", str(project(tmp_path, rules, data)))
    assert "\x1b" not in out
    assert "PASS  1. not_null 'line\\nbreak': no NULL values\n" in out
    assert "PASS  2. not_null '\\x1b[31mred': no NULL values\n" in out


def test_rich_markup_in_names_is_not_interpreted(tmp_path: Path) -> None:
    data = b"[bold]x[/bold]\n1\n"
    rules = (
        "version: 1\nsource: {path: orders.csv}\nchecks:\n"
        "  - {rule: unique, column: '[bold]x[/bold]'}\n"
    )
    _, out, _ = run("check", str(project(tmp_path, rules, data)))
    assert "PASS  1. unique '[bold]x[/bold]': no repeated values\n" in out


# --- as a real process ------------------------------------------------------------------------


def run_process(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rowgate", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=cwd,
        env=env,
    )


@pytest.mark.parametrize(
    ("data", "rules", "code"),
    [
        (ORDERS, RULES, 1),
        (b"order_id,status,amount\n1,paid,10\n", RULES, 0),
        (ORDERS, RULES.replace("column: amount", "column: amountt"), 2),  # missing column
        (b"order_id,status\n1,a,b\n", RULES, 2),  # unreadable data
        (b"\xff\xfe", RULES, 2),  # not UTF-8
        (ORDERS, RULES.replace("min: 1", "min: yes"), 2),  # bad rules
    ],
    ids=["failing", "passing", "missing-column", "ragged", "not-utf8", "bad-rules"],
)
def test_real_process_exit_codes_and_clean_stderr(
    tmp_path: Path, data: bytes, rules: str, code: int
) -> None:
    completed = run_process("check", str(project(tmp_path, rules, data)))
    assert completed.returncode == code
    assert "Traceback" not in completed.stderr
    assert "nanobind" not in completed.stderr  # DuckDB 2.0 leak warnings at exit
    if code == 2:
        assert completed.stdout == ""
        assert completed.stderr.startswith("error: ")
    else:
        assert completed.stderr == ""


def test_relative_paths_resolve_from_the_rules_file(tmp_path: Path) -> None:
    project(tmp_path / "project")
    completed = run_process("check", "project/rules.yaml", cwd=tmp_path)
    assert completed.returncode == 1
    assert completed.stdout.startswith("Source: orders.csv (csv, 4 rows, 3 columns)\n")


def test_unicode_column_names_on_a_cp1252_console(tmp_path: Path) -> None:
    data = "注文,état\n1,a\n".encode()
    rules = "version: 1\nsource: {path: orders.csv}\nchecks:\n  - {rule: not_null, column: 注文}\n"
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    completed = run_process("check", str(project(tmp_path, rules, data)), env=env)
    assert (completed.returncode, completed.stderr) == (0, "")
    assert "PASS  1. not_null '\\u6ce8\\u6587': no NULL values" in completed.stdout


def test_a_path_with_control_characters_is_printed_escaped(tmp_path: Path) -> None:
    # A colour code would contain '[', which source paths may not; a tab and a
    # bare escape character are enough to prove the path is not printed raw.
    if sys.platform == "win32":
        pytest.skip("Windows does not allow control characters in file names")
    (tmp_path / "x\ty\x1b.csv").write_bytes(b"id\n1\n")
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        'version: 1\nsource: {path: "x\\ty\\e.csv"}\nchecks: [{rule: row_count, min: 1}]\n',
        encoding="utf-8",
    )
    code, out, _ = run("check", str(rules))
    assert code == 0
    assert "\x1b" not in out
    assert out.startswith("Source: 'x\\ty\\x1b.csv' (csv, 1 row, 1 column)\n")


@pytest.mark.parametrize(
    "bad_line",
    [b'2,"private-value"x,1', b"2,private-value" + b"x" * 17_000_000 + b",1"],
    ids=["stray-quote", "record-over-16-mib"],
)
def test_unreadable_lines_are_reported_without_their_content(
    tmp_path: Path, bad_line: bytes
) -> None:
    data = b"order_id,status,amount\n1,paid,10\n" + bad_line + b"\n"
    completed = run_process("check", str(project(tmp_path, data=data)))
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("error: line 3 of 'orders.csv' ")
    assert "private-value" not in completed.stderr
    assert "Possible fixes" not in completed.stderr
    assert len(completed.stderr) < 300


# --- DuckDB's own message, only with --debug ------------------------------------------------------


@pytest.fixture
def duckdb_meets_a_bad_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Skip RowGate's own CSV checks so that DuckDB itself fails on line 3.
    monkeypatch.setattr(
        source_module, "scan_csv", lambda *args: CsvLayout(["order_id", "status", "amount"], 2, 0)
    )
    return project(tmp_path, data=b'order_id,status,amount\n1,paid,10\n2,"private-value"x,3\n')


def test_without_debug_duckdb_message_is_hidden(duckdb_meets_a_bad_line: Path) -> None:
    code, out, err = run("check", str(duckdb_meets_a_bad_line))
    assert (code, out) == (2, "")
    assert err == (
        "error: DuckDB could not read line 3 of 'orders.csv' as CSV; run again with --debug to "
        "see DuckDB's own message, which can include data values from the file\n"
    )


def test_with_debug_duckdb_message_is_printed(duckdb_meets_a_bad_line: Path) -> None:
    code, out, err = run("check", str(duckdb_meets_a_bad_line), "--debug")
    assert (code, out) == (2, "")
    assert "Traceback (most recent call last)" in err
    assert "DuckDB's original message (it can include data values from the file):\n" in err
    assert "private-value" in err  # DuckDB quotes the row
    assert err.endswith("error: DuckDB could not read line 3 of 'orders.csv' as CSV\n")


# --- samples on the command line ------------------------------------------------------------------

MANY = b"order_id,status,amount\n" + b"".join(b"%d,s,\n" % i for i in range(1, 13))
NOT_NULL_AMOUNT = (
    "version: 1\nsource: {path: orders.csv}\nchecks:\n  - {rule: not_null, column: amount}\n"
)


def test_five_samples_are_shown_by_default(tmp_path: Path) -> None:
    code, out, _ = run("check", str(project(tmp_path, NOT_NULL_AMOUNT, MANY)))
    assert code == 1
    assert out.splitlines()[2:9] == [
        "FAIL  1. not_null 'amount': 12 of 12 rows are NULL",
        "        row 1: NULL",
        "        row 2: NULL",
        "        row 3: NULL",
        "        row 4: NULL",
        "        row 5: NULL",
        "        ... and 7 more rows",
    ]


@pytest.mark.parametrize(("value", "shown"), [("1", 1), ("12", 12), ("1000", 12)])
def test_max_samples(tmp_path: Path, value: str, shown: int) -> None:
    code, out, _ = run(
        "check", str(project(tmp_path, NOT_NULL_AMOUNT, MANY)), "--max-samples", value
    )
    assert code == 1
    assert sum(1 for line in out.splitlines() if line.startswith("        row ")) == shown


@pytest.mark.parametrize(
    "value", ["0", "-1", "1001", "abc", "2.5", "", " 5", "+5", "\uff15", "1" * 5000]
)
def test_max_samples_outside_1_to_1000_is_a_usage_error(tmp_path: Path, value: str) -> None:
    code, out, err = run("check", str(project(tmp_path)), "--max-samples", value)
    assert (code, out) == (2, "")
    shown = value if len(value) <= 20 else value[:17] + "..."
    assert (
        err == f"error: --max-samples must be a whole number from 1 to 1000, got {quoted(shown)}\n"
    )


def test_a_bad_max_samples_is_reported_even_with_no_samples(tmp_path: Path) -> None:
    code, out, err = run("check", str(project(tmp_path)), "--no-samples", "--max-samples", "0")
    assert (code, out) == (2, "")
    assert err.startswith("error: --max-samples must be")


def test_max_samples_errors_are_one_line_without_a_usage_panel(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "rowgate", "check", str(project(tmp_path)), "--max-samples", "0"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FORCE_COLOR": "1", "COLUMNS": "60"},
    )
    assert (completed.returncode, completed.stdout) == (2, "")
    assert completed.stderr == (
        "error: --max-samples must be a whole number from 1 to 1000, got '0'\n"
    )


def test_no_samples_shows_none(tmp_path: Path) -> None:
    code, out, _ = run("check", str(project(tmp_path, NOT_NULL_AMOUNT, MANY)), "--no-samples")
    assert code == 1
    assert out.splitlines()[2:] == [
        "FAIL  1. not_null 'amount': 12 of 12 rows are NULL",
        "",
        "1 check: 0 passed, 1 failed, 0 warned",
    ]


def test_no_samples_wins_over_max_samples(tmp_path: Path) -> None:
    rules = str(project(tmp_path, NOT_NULL_AMOUNT, MANY))
    assert run("check", rules, "--no-samples", "--max-samples", "9") == run(
        "check", rules, "--no-samples"
    )


def test_debug_and_no_samples_together_are_a_usage_error(tmp_path: Path) -> None:
    code, out, err = run("check", str(project(tmp_path)), "--no-samples", "--debug")
    assert (code, out) == (2, "")
    assert err == ("error: --debug can print data values, so it cannot be used with --no-samples\n")


def test_internal_errors_with_no_samples_show_only_the_error_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("value private-value broke something")

    monkeypatch.setattr(cli, "run_checks", broken)
    code, _, err = run("check", str(project(tmp_path)), "--no-samples")
    assert code == 2
    assert err == (
        "error: internal error: RuntimeError (run again with --debug to see the traceback)\n"
    )


UNIQUE_V = "version: 1\nsource: {path: orders.csv}\nchecks:\n  - {rule: unique, column: v}\n"


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (b"plain", "'plain'"),
        (b"line\nbreak", "'line\\nbreak'"),
        (b"\x1b[31mred\x1b[0m", "'\\x1b[31mred\\x1b[0m'"),
        (b"[bold]markup[/bold]", "'[bold]markup[/bold]'"),
        ("\u202eevil".encode(), "'\\u202eevil'"),  # right-to-left override
        (b"tab\there", "'tab\\there'"),
        (b"it's", '"it\'s"'),
        (b"", "''"),
    ],
    ids=["plain", "newline", "ansi", "rich-markup", "bidi", "tab", "quote", "empty"],
)
def test_sample_values_are_escaped(tmp_path: Path, value: bytes, shown: str) -> None:
    field = b'"' + value.replace(b'"', b'""') + b'"'
    data = b"v\n" + field + b"\n" + field + b"\n"
    code, out, _ = run("check", str(project(tmp_path, UNIQUE_V, data)))
    assert code == 1
    assert f"        row 1: {shown}, appears 2 times\n" in out
    assert "\x1b" not in out
    assert "\u202e" not in out


def test_long_values_are_cut_at_80_characters(tmp_path: Path) -> None:
    long = "x" * 79 + "yz" + "w" * 919  # 1000 characters; the 81st is "z"
    data = f"v\n{long}\n{long}\n".encode()
    code, out, _ = run("check", str(project(tmp_path, UNIQUE_V, data)))
    assert code == 1
    shown = "x" * 79 + "y"
    assert f"        row 1: '{shown}' [cut: 1000 characters in all], appears 2 times\n" in out


def test_values_of_exactly_80_characters_are_not_cut(tmp_path: Path) -> None:
    value = "v" * 80
    data = f"v\n{value}\n{value}\n".encode()
    _, out, _ = run("check", str(project(tmp_path, UNIQUE_V, data)))
    assert f"        row 1: '{value}', appears 2 times\n" in out


def test_unique_lists_how_many_repeated_values_are_not_shown(tmp_path: Path) -> None:
    data = b"v\n" + b"".join(b"%d\n%d\n" % (i, i) for i in range(7))
    _, out, _ = run("check", str(project(tmp_path, UNIQUE_V, data)), "--max-samples", "2")
    assert out.splitlines()[2:6] == [
        "FAIL  1. unique 'v': 14 of 14 rows have a value that appears more than once",
        "        row 1: '0', appears 2 times",
        "        row 3: '1', appears 2 times",
        "        ... and 5 more repeated values",
    ]


def test_one_more_row_is_singular(tmp_path: Path) -> None:
    data = b"order_id,status,amount\n1,a,\n2,b,\n"
    _, out, _ = run("check", str(project(tmp_path, NOT_NULL_AMOUNT, data)), "--max-samples", "1")
    assert "        ... and 1 more row\n" in out


def test_an_empty_rules_path_has_its_own_message() -> None:
    assert run("check", "") == (2, "", "error: the rules file path is empty\n")
    code, out, err = run("check", "", "--format", "json")
    assert (code, out) == (2, "")
    assert err == '{"error": "the rules file path is empty", "details": []}\n'
