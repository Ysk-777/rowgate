"""Paths, column names and values are shown as written: backslashes are never doubled.

These tests run on every operating system. A path such as a\\rules.yaml is
a file with a backslash in its name on Linux and macOS, and a file in the
folder a on Windows; either way RowGate must show it with one backslash.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rowgate.cli import app
from rowgate.display import escaped, quoted, shown


def run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C:\\data", "'C:\\data'"),  # one backslash, as written
        ("a\\nb", "'a\\nb'"),  # a backslash and an n, kept as written
        ("café 中文 ✓", "'café 中文 ✓'"),  # printable Unicode is kept
        ("it's", '"it\'s"'),
        ('it\'s "x"', "'it's \"x\"'"),
        ("line\nbreak\ttab\r", "'line\\nbreak\\ttab\\r'"),
        ("\x1b[31mred\x1b[0m", "'\\x1b[31mred\\x1b[0m'"),
        ("nul\x00 del\x7f", "'nul\\x00 del\\x7f'"),
        ("zero\u200bwidth \u202eright-to-left", "'zero\\u200bwidth \\u202eright-to-left'"),
        ("no\xa0break \u2028line", "'no\\xa0break \\u2028line'"),
        ("[bold]markup[/bold]", "'[bold]markup[/bold]'"),
        ("", "''"),
    ],
)
def test_quoted_escapes_only_what_cannot_be_printed(text: str, expected: str) -> None:
    assert quoted(text) == expected
    assert escaped(text) == expected[1:-1]


def test_shown_quotes_text_and_reprs_anything_else() -> None:
    assert shown("C:\\x") == "'C:\\x'"
    assert shown(5) == "5"
    assert shown(None) == "None"


def windows_style_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules: str) -> str:
    """Write rules at a\\rules.yaml and return that path as typed on the command line."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a\\rules.yaml").write_text(rules, encoding="utf-8")
    return "a\\rules.yaml"


def beside(tmp_path: Path, rules_path: str, name: str) -> Path:
    """Where a data path ``name`` written in those rules points to, on this system."""
    return tmp_path / Path(rules_path).parent / name


def test_a_windows_style_rules_path_is_shown_with_single_backslashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = windows_style_rules(
        tmp_path, monkeypatch, "version: 2\nsource: {path: d.csv}\nchecks: []\n"
    )
    code, out, err = run("validate", path)
    assert (code, out) == (2, "")
    assert err.startswith("error: invalid rules file 'a\\rules.yaml' (2 problems):\n")


def test_a_windows_style_rules_path_with_one_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = windows_style_rules(
        tmp_path,
        monkeypatch,
        "version: 2\nsource: {path: d.csv}\nchecks: [{rule: row_count, min: 1}]\n",
    )
    code, _, err = run("validate", path)
    assert code == 2
    assert err.startswith("error: a\\rules.yaml: field 'version': ")


def test_validate_shows_windows_style_paths_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = windows_style_rules(
        tmp_path,
        monkeypatch,
        'version: 1\nsource: {path: "data\\\\d.csv"}\nchecks: [{rule: row_count, min: 1}]\n',
    )
    assert run("validate", path) == (
        0,
        "a\\rules.yaml is valid: 1 check on data\\d.csv (csv)\n",
        "",
    )


def test_a_missing_windows_style_data_path_is_shown_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = windows_style_rules(
        tmp_path,
        monkeypatch,
        'version: 1\nsource: {path: "C:\\\\Users\\\\someone\\\\missing.csv"}\n'
        "checks: [{rule: row_count, min: 1}]\n",
    )
    code, out, err = run("check", path)
    assert (code, out) == (2, "")
    # On Windows the path is absolute; elsewhere it is relative to the rules file.
    assert err.startswith("error: data file 'C:\\Users\\someone\\missing.csv' not found")
    assert "\\\\" not in err


def test_check_shows_windows_style_names_and_values_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = windows_style_rules(
        tmp_path,
        monkeypatch,
        'version: 1\nsource: {path: "x\\\\data.csv"}\n'
        "checks:\n"
        "  - {rule: not_null, column: 'C:\\col'}\n"
        "  - {rule: regex, column: path, pattern: '[a-z]+'}\n",
    )
    beside(tmp_path, path, "x").mkdir()
    beside(tmp_path, path, "x\\data.csv").write_bytes(
        b"C:\\col,path\n,C:\\data\n1,\\\\server\\share\n"
    )
    code, out, err = run("check", path)
    assert (code, err) == (1, "")
    assert out.splitlines() == [
        "Source: x\\data.csv (csv, 2 rows, 2 columns)",
        "",
        "FAIL  1. not_null 'C:\\col': 1 of 2 rows is NULL",
        "        row 1: NULL",
        "FAIL  2. regex 'path': 2 of 2 rows do not match the pattern",
        "        row 1: 'C:\\data'",
        "        row 2: '\\\\server\\share'",
        "",
        "2 checks: 0 passed, 2 failed, 0 warned",
    ]


def test_missing_column_errors_show_backslashes_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = windows_style_rules(
        tmp_path,
        monkeypatch,
        "version: 1\nsource: {path: d.csv}\nchecks: [{rule: not_null, column: 'C:\\coll'}]\n",
    )
    beside(tmp_path, path, "d.csv").write_bytes(b"C:\\col\n1\n")
    code, out, err = run("check", path)
    assert (code, out) == (2, "")
    assert err == (
        "error: a check cannot run on 'd.csv'\n"
        "  check 1 (not_null): no column 'C:\\coll' (did you mean 'C:\\col'?)\n"
        "  the file has 1 column: 'C:\\col'\n"
    )


def test_the_rules_path_is_shown_exactly_as_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Path would print ./rules.yaml as rules.yaml everywhere, and a/b.yaml as
    # a\b.yaml on Windows; RowGate keeps the text the user typed.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules.yaml").write_text(
        "version: 1\nsource: {path: missing.csv}\nchecks: [{rule: row_count, min: 1}]\n",
        encoding="utf-8",
    )
    assert run("validate", "./rules.yaml") == (
        0,
        "./rules.yaml is valid: 1 check on missing.csv (csv)\n",
        "",
    )
    code, _, err = run("check", ".//rules.yaml")
    assert code == 2
    assert err == (
        "error: data file 'missing.csv' not found, relative to the rules file './/rules.yaml'\n"
    )
    code, _, err = run("validate", "./nope.yaml")
    assert (code, err) == (2, "error: ./nope.yaml: file not found\n")
