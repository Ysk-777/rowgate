"""``rowgate check --format json``: the report, its golden files, and JSON errors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from rowgate import cli
from rowgate import source as source_module
from rowgate.cli import app, main
from rowgate.source import CsvLayout

GOLDEN = Path(__file__).parent / "golden"

CSV_DATA = (
    "id,code,amount,note\n"
    "1,ok,5,café\n"
    '2,"x\x1b[31m [bold]y[/bold]",50,\n'
    '3,ok,,"line\nbreak"\n'
    f"3,{'long-' * 20},9007199254740993,plain\n"
    "4,ok,1e400,\n"
).encode()

CSV_RULES = """\
version: 1
source: {path: data.csv}
checks:
  - {rule: columns_exist, columns: [id, code, amount, note]}
  - {rule: row_count, max: 3}
  - {rule: not_null, column: note, severity: warn}
  - {rule: unique, column: id}
  - {rule: in_set, column: code, values: [ok]}
  - {rule: range, column: amount, min: 0, max: 10}
  - {rule: regex, column: note, pattern: '[a-z]+'}
"""

PARQUET_RULES = """\
version: 1
source: {path: data.parquet}
checks:
  - {rule: range, column: d, min: 0, max: 10}
  - {rule: in_set, column: b, values: [1]}
  - {rule: range, column: day, max: 2024-12-31}
  - {rule: in_set, column: flag, values: [true]}
  - {rule: unique, column: price}
  - {rule: regex, column: d, pattern: '[0-9]+[.][0-9]+'}
"""


def csv_project(tmp_path: Path) -> Path:
    (tmp_path / "data.csv").write_bytes(CSV_DATA)
    rules = tmp_path / "rules.yaml"
    rules.write_text(CSV_RULES, encoding="utf-8")
    return rules


def parquet_project(tmp_path: Path) -> Path:
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT * FROM (VALUES "  # noqa: S608 - test data
        "(1.5::DOUBLE, 9007199254740993::BIGINT, DATE '2024-01-31', true, 1.10::DECIMAL(18,2)), "
        "('nan'::DOUBLE, 1, DATE '2025-01-01', false, 1.10), "
        "('inf'::DOUBLE, 9007199254740992, NULL, NULL, 2.00), "
        "('-inf'::DOUBLE, 1, DATE '2024-12-31', true, 2.00), "
        "(1e20::DOUBLE, NULL, DATE '2023-06-30', false, 3.00)"
        f") t(d, b, day, flag, price)) TO '{target}' (FORMAT parquet)"
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text(PARQUET_RULES, encoding="utf-8")
    return rules


def run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


def run_main(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    """Run the real entry point, which also reports command-line usage errors."""
    with pytest.raises(SystemExit) as caught:
        main(list(args))
    captured = capsys.readouterr()
    assert isinstance(caught.value.code, int)
    return caught.value.code, captured.out, captured.err


def strict_json(text: str) -> object:
    """Parse JSON, refusing NaN and Infinity literals, which strict JSON does not have."""

    def refuse(constant: str) -> object:
        raise AssertionError(f"not strict JSON: {constant}")

    assert text.isascii()
    return json.loads(text, parse_constant=refuse)


# --- golden files ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("project", "golden"),
    [(csv_project, "report_csv.json"), (parquet_project, "report_parquet.json")],
    ids=["csv", "parquet"],
)
def test_report_matches_the_golden_file(tmp_path: Path, project: object, golden: str) -> None:
    # To update after a deliberate change, check the new output by eye and
    # copy it over the golden file: these files are the documented format.
    assert callable(project)
    code, out, err = run("check", str(project(tmp_path)), "--format", "json", "--max-samples", "2")
    assert (code, err) == (1, "")
    assert out == (GOLDEN / golden).read_text(encoding="utf-8")


def test_the_report_is_strict_ascii_json_with_exact_values(tmp_path: Path) -> None:
    code, out, _ = run("check", str(parquet_project(tmp_path)), "--format", "json")
    assert code == 1
    report = strict_json(out)
    assert isinstance(report, dict)
    range_check, in_set_check = report["checks"][0], report["checks"][1]
    assert [s["value"] for s in range_check["samples"]] == ["nan", "inf", "-inf", "1e+20"]
    assert [s["value"] for s in in_set_check["samples"]] == [
        "9007199254740993",
        "9007199254740992",
    ]


def test_the_same_input_gives_the_same_bytes(tmp_path: Path) -> None:
    rules = str(csv_project(tmp_path))
    assert run("check", rules, "--format", "json") == run("check", rules, "--format", "json")


def test_stdout_holds_only_the_report(tmp_path: Path) -> None:
    rules = csv_project(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "rowgate", "check", str(rules), "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, "FORCE_COLOR": "1"},  # colour codes must not reach JSON
    )
    assert (completed.returncode, completed.stderr) == (1, "")
    report = strict_json(completed.stdout)
    assert isinstance(report, dict)
    assert list(report) == ["schema_version", "rowgate_version", "source", "summary", "checks"]
    assert report["schema_version"] == 1


def test_a_passing_run_has_empty_sample_lists(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n2\n")
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: unique, column: id}]\n",
        encoding="utf-8",
    )
    code, out, _ = run("check", str(rules), "--format", "json")
    assert code == 0
    report = strict_json(out)
    assert isinstance(report, dict)
    [only] = report["checks"]
    assert (only["status"], only["failing_rows"], only["samples"], only["not_shown"]) == (
        "passed",
        0,
        [],
        0,
    )


def test_no_samples_leaves_no_value_in_the_report(tmp_path: Path) -> None:
    code, out, _ = run("check", str(csv_project(tmp_path)), "--format", "json", "--no-samples")
    assert code == 1
    report = strict_json(out)
    assert isinstance(report, dict)
    assert all(c["samples"] is None and c["not_shown"] is None for c in report["checks"])
    for value in ("caf", "bold", "long-", "9007199254740993", "line", "plain", "1e400"):
        assert value not in out


# --- errors as JSON --------------------------------------------------------------------------


def json_error(err: str) -> dict[str, object]:
    lines = err.splitlines()
    assert len(lines) == 1, err  # one JSON object on one line, nothing else
    document = strict_json(lines[0])
    assert isinstance(document, dict)
    return document


def test_a_rules_error_is_json_with_every_problem(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text("version: 2\nsource: {path: d.csv}\nchecks: []\n", encoding="utf-8")
    code, out, err = run("check", str(rules), "--format", "json")
    assert (code, out) == (2, "")
    error = json_error(err)
    assert list(error) == ["error", "details"]
    assert error["error"] == f"invalid rules file '{rules}' (2 problems)"
    assert isinstance(error["details"], list)
    assert len(error["details"]) == 2


def test_a_data_error_is_json_with_its_details(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n")
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: not_null, column: idd}]\n",
        encoding="utf-8",
    )
    code, out, err = run("check", str(rules), "--format", "json")
    assert (code, out) == (2, "")
    assert json_error(err) == {
        "error": "a check cannot run on 'data.csv'",
        "details": [
            "check 1 (not_null): no column 'idd' (did you mean 'id'?)",
            "the file has 1 column: 'id'",
        ],
    }


def test_with_debug_the_json_error_carries_duckdb_s_message_and_the_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Skip RowGate's own CSV checks so that DuckDB itself fails on the bad line.
    monkeypatch.setattr(source_module, "scan_csv", lambda *args: CsvLayout(["id", "code"], 2, 0))
    (tmp_path / "data.csv").write_bytes(b'id,code\n1,ok\n2,"x"y\n')
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "version: 1\nsource: {path: data.csv}\nchecks: [{rule: row_count, min: 1}]\n",
        encoding="utf-8",
    )
    code, out, err = run("check", str(rules), "--format", "json", "--debug")
    assert (code, out) == (2, "")
    error = json_error(err)
    assert list(error) == ["error", "details", "debug"]
    debug = error["debug"]
    assert isinstance(debug, list)
    assert debug[0] == "DuckDB's original message (it can include data values from the file):"
    assert any(line.startswith("Traceback (most recent call last):") for line in debug)
    _, _, plain = run("check", str(rules), "--format", "json")
    assert "debug" not in json_error(plain)


def test_an_internal_error_with_no_samples_is_json_with_its_type_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("MRK-data-value")

    monkeypatch.setattr(cli, "run_checks", broken)
    code, out, err = run("check", str(csv_project(tmp_path)), "--format", "json", "--no-samples")
    assert (code, out) == (2, "")
    assert json_error(err) == {
        "error": "internal error: ValueError (run again with --debug to see the traceback)",
        "details": [],
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--max-samples", "0"], "--max-samples must be a whole number from 1 to 1000, got '0'"),
        (
            ["--no-samples", "--debug"],
            "--debug can print data values, so it cannot be used with --no-samples",
        ),
    ],
)
def test_rowgate_usage_errors_are_json(tmp_path: Path, args: list[str], message: str) -> None:
    code, out, err = run("check", str(csv_project(tmp_path)), "--format", "json", *args)
    assert (code, out) == (2, "")
    assert json_error(err) == {"error": message, "details": []}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["check", "--format", "json"], "Missing argument 'RULES.yaml'."),
        (["check", "r.yaml", "--format=json", "--bogus"], "No such option: --bogus"),
        (["check", "r.yaml", "--bogus", "--format", "JSON"], "No such option: --bogus"),
    ],
)
def test_command_line_usage_errors_are_json_when_json_is_asked_for(
    capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    code, out, err = run_main(capsys, *args)
    assert (code, out) == (2, "")
    assert json_error(err) == {
        "error": message,
        "details": ["run 'rowgate check --help' to see the options"],
    }


@pytest.mark.parametrize(
    ("args", "first_line"),
    [
        (["check"], "error: Missing argument 'RULES.yaml'."),
        (["check", "r.yaml", "--bogus"], "error: No such option: --bogus"),
        (
            ["check", "r.yaml", "--format", "xml"],
            "error: Invalid value for '--format': 'xml' is not one of 'text', 'json'.",
        ),
        (["nope"], "error: No such command 'nope'."),
    ],
)
def test_command_line_usage_errors_use_rowgate_s_error_style(
    capsys: pytest.CaptureFixture[str], args: list[str], first_line: str
) -> None:
    code, out, err = run_main(capsys, *args)
    assert (code, out) == (2, "")
    where = "rowgate check" if args[0] == "check" else "rowgate"
    assert err == f"{first_line}\n  run '{where} --help' to see the options\n"


def test_the_entry_point_keeps_help_version_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run_main(capsys)
    assert code == 2
    assert "validate" in out  # no arguments: the help, and the usage exit code
    assert run_main(capsys, "--version")[:2] == (0, "rowgate 0.1.0\n")
    code, out, _ = run_main(capsys, "check", "--help")
    assert code == 0
    assert "--format" in out
    rules = str(csv_project(tmp_path))
    assert run_main(capsys, "check", rules, "--format", "json")[0] == 1
