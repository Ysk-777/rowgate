"""The ``rowgate validate`` command and the CLI's error handling."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from rowgate import __version__, cli
from rowgate.cli import app, report_errors

VALID = """\
version: 1
source:
  path: data/orders.csv
checks:
  - rule: not_null
    column: order_id
  - rule: regex
    column: sku
    pattern: "[A-Z]{3}-[0-9]{4}"
"""


def write(tmp_path: Path, text: str, name: str = "rules.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


def test_valid_rules_file(tmp_path: Path) -> None:
    rules = write(tmp_path, VALID)
    code, out, err = run("validate", str(rules))
    assert (code, err) == (0, "")
    assert out == f"{rules} is valid: 2 checks on data/orders.csv (csv)\n"


def test_validate_does_not_need_the_data_file(tmp_path: Path) -> None:
    rules = write(tmp_path, VALID)
    assert not (tmp_path / "data" / "orders.csv").exists()
    assert run("validate", str(rules))[0] == 0


def test_singular_check(tmp_path: Path) -> None:
    rules = write(
        tmp_path,
        "version: 1\nsource: {path: d.parquet}\nchecks: [{rule: row_count, min: 1}]\n",
    )
    assert run("validate", str(rules))[1] == f"{rules} is valid: 1 check on d.parquet (parquet)\n"


def test_one_problem(tmp_path: Path) -> None:
    rules = write(tmp_path, VALID.replace("column: order_id", "colum: order_id"))
    code, out, err = run("validate", str(rules))
    assert (code, out) == (2, "")
    assert err == (
        f"error: invalid rules file '{rules}' (2 problems):\n"
        "  - check 1 (not_null), field 'column': required field is missing\n"
        "  - check 1 (not_null), field 'colum': unknown field (did you mean 'column'?)\n"
    )


def test_invalid_regex_is_caught_by_validate(tmp_path: Path) -> None:
    rules = write(tmp_path, VALID.replace('"[A-Z]', '"([A-Z]'))
    code, out, err = run("validate", str(rules))
    assert (code, out) == (2, "")
    # The reason after the prefix is RE2's wording, so it is not pinned here.
    assert err.startswith(
        f"error: {rules}: check 2 (regex), field 'pattern': invalid regular expression: "
    )
    assert err.count("\n") == 1


def test_url_source_is_rejected(tmp_path: Path) -> None:
    rules = write(tmp_path, VALID.replace("data/orders.csv", "https://example.com/orders.csv"))
    code, _, err = run("validate", str(rules))
    assert code == 2
    assert err == (
        f"error: {rules}: field 'source.path': URLs are not supported; "
        "RowGate reads local files only\n"
    )


def test_missing_rules_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    assert run("validate", str(missing)) == (2, "", f"error: {missing}: file not found\n")


def test_yaml_syntax_error(tmp_path: Path) -> None:
    rules = write(tmp_path, "version: 1\nsource: {path: a.csv\n")
    code, _, err = run("validate", str(rules))
    assert code == 2
    assert err.startswith(f"error: {rules}: invalid YAML at line 3, column 1:")


def test_missing_argument_is_a_usage_error() -> None:
    code, out, err = run("validate")
    assert (code, out) == (2, "")
    assert err  # click explains the usage error in its own words


UNICODE_FIELD = (
    "version: 1\nsource: {path: a.csv}\nchecks:\n  - {rule: unique, column: 注文, 列: x}\n"
)


def test_unicode_in_messages(tmp_path: Path) -> None:
    rules = write(tmp_path, UNICODE_FIELD)
    code, _, err = run("validate", str(rules))
    assert code == 2
    assert "field '列': unknown field" in err


def test_internal_errors_are_short_without_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path) -> None:
        raise RuntimeError("something broke")

    monkeypatch.setattr(cli, "load_rules", broken)
    code, out, err = run("validate", str(write(tmp_path, VALID)))
    assert (code, out) == (2, "")
    assert err == (
        "error: internal error: RuntimeError: something broke "
        "(run again with --debug to see the traceback)\n"
    )


def test_internal_errors_show_a_traceback_with_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path) -> None:
        raise RuntimeError("something broke")

    monkeypatch.setattr(cli, "load_rules", broken)
    code, _, err = run("validate", str(write(tmp_path, VALID)), "--debug")
    assert code == 2
    assert "Traceback (most recent call last)" in err
    assert err.endswith("error: internal error: RuntimeError: something broke\n")


def test_rules_errors_show_a_traceback_with_debug(tmp_path: Path) -> None:
    code, _, err = run("validate", str(tmp_path / "nope.yaml"), "--debug")
    assert code == 2
    assert "Traceback (most recent call last)" in err
    assert "rowgate.errors.RulesError" in err
    assert err.endswith("file not found\n")


def test_exit_requests_pass_through_the_error_handler() -> None:
    with pytest.raises(typer.Exit) as excinfo, report_errors(debug=False):
        raise typer.Exit(1)
    assert excinfo.value.exit_code == 1


def run_process(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rowgate", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )


def test_real_process_exit_codes(tmp_path: Path) -> None:
    good = run_process("validate", str(write(tmp_path, VALID)))
    assert (good.returncode, good.stderr) == (0, "")
    bad = run_process("validate", str(write(tmp_path, "version: 2\n", "bad.yaml")))
    assert bad.returncode == 2
    assert "Traceback" not in bad.stderr
    assert bad.stderr.startswith("error: invalid rules file")


def test_output_survives_a_console_that_cannot_encode_it(tmp_path: Path) -> None:
    # A redirected Windows console typically uses cp1252, and Python's stdout
    # is strict by default, so printing this path would otherwise crash.
    rules = write(
        tmp_path, "version: 1\nsource: {path: 注文.csv}\nchecks: [{rule: unique, column: id}]\n"
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    completed = run_process("validate", str(rules), env=env)
    assert (completed.returncode, completed.stderr) == (0, "")
    assert completed.stdout.endswith("is valid: 1 check on \\u6ce8\\u6587.csv (csv)\n")


def test_main_leaves_unusual_streams_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "argv", ["rowgate", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    assert out.getvalue() == f"rowgate {__version__}\n"
