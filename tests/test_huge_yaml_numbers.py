"""Huge hex, octal, binary and zero-led numbers in the rules never become decimal text.

Python refuses to turn an int of more than 4,300 decimal digits into text,
so such a value used to crash RowGate with an internal error. Refusals now
keep the text as written and say how many digits the number has, and an
explicit !!int value of any size is read as an exact Decimal.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rowgate import display
from rowgate.cli import app
from rowgate.rules import describe_value

HEX = 16**5000 - 1  # 0x followed by 5000 f
OCTAL = 8**5000 - 1  # 0o followed by 5000 sevens
BINARY = 2**20000 - 1  # 0b followed by 20000 ones

# Unquoted forms that YAML 1.1 reads in a surprising way, so RowGate refuses them.
REFUSED = [
    ("0" + "9" * 5000, "text, not as a number with 5000 digits"),
    ("0x" + "f" * 5000, f"a number with {display.digits(HEX)} digits"),
    ("0o" + "7" * 5000, f"text, not as a number with {display.digits(OCTAL)} digits"),
    ("0b" + "1" * 20000, f"a number with {display.digits(BINARY)} digits"),
]
IDS = ["zero-led", "hex", "octal", "binary"]
FIELDS = {
    "range": ("{{rule: range, column: v, min: {}}}", "check 1 (range), field 'min'", 40),
    "in_set": (
        "{{rule: in_set, column: v, values: [{}]}}",
        "check 1 (in_set), field 'values', item 1",
        45,
    ),
}


def project(tmp_path: Path, check: str, data: str = "v\n1\n2\n") -> Path:
    (tmp_path / "data.csv").write_text(data, encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        f"version: 1\nsource: {{path: data.csv}}\nchecks: [{check}]\n", encoding="utf-8"
    )
    return rules


def run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.parametrize("field", list(FIELDS))
@pytest.mark.parametrize(("text", "meaning"), REFUSED, ids=IDS)
def test_a_huge_surprising_number_is_refused_with_its_digit_count(
    tmp_path: Path, text: str, meaning: str, field: str
) -> None:
    template, where, column = FIELDS[field]
    rules = str(project(tmp_path, template.format(text)))
    problem = (
        f"{where}: unquoted {text} is read by YAML as {meaning}; write it as a plain decimal "
        f"number if you mean that number, or put it in quotes ('{text}') for text "
        f"(line 3, column {column})"
    )
    for command in ("validate", "check"):
        code, out, err = run(command, rules)
        assert (code, out) == (2, "")
        assert err == f"error: {rules}: {problem}\n"
    code, out, err = run("check", rules, "--format", "json")
    assert (code, out) == (2, "")
    assert json.loads(err) == {
        "error": f"invalid rules file '{rules}' (1 problem)",
        "details": [problem],
    }


@pytest.mark.parametrize(
    ("written", "value"),
    [
        ("!!int 0x" + "f" * 5000, HEX),
        ("!!int 0b" + "1" * 20000, BINARY),
        ("!!int 0" + "7" * 5000, OCTAL),  # YAML 1.1: a leading zero means octal
        ("!!int " + "9" * 2000 + "_" + "9" * 3000, 10**5000 - 1),  # decimal, with an underscore
    ],
    ids=["hex", "binary", "octal", "decimal"],
)
def test_an_explicit_int_of_any_size_is_an_exact_number(
    tmp_path: Path, written: str, value: int
) -> None:
    exact = str(Decimal(value))  # Decimal(int) needs no decimal text of the int
    data = f"v\n1\n{exact}\n2\n"
    shown = f"<{display.digits(value)}-digit number>"
    rules = str(project(tmp_path, f"{{rule: range, column: v, min: {written}}}", data))
    assert run("validate", rules) == (0, f"{rules} is valid: 1 check on data.csv (csv)\n", "")
    # 1 and 2 are below the bound; the exact value itself is past the 64-bit
    # float range, so as documented for range (like 1e400) it is a failing row.
    code, out, _ = run("check", rules)
    assert code == 1
    assert f"FAIL  1. range 'v': 3 of 3 rows are not a number of at least {shown}" in out
    code, out, _ = run("check", rules, "--format", "json")
    assert code == 1
    [result] = json.loads(out)["checks"]
    assert (result["failing_rows"], result["message"]) == (
        3,
        f"3 of 3 rows are not a number of at least {shown}",
    )

    rules = str(project(tmp_path, f"{{rule: in_set, column: v, values: [{written}]}}", data))
    assert run("validate", rules)[0] == 0
    for extra in ([], ["--format", "json"]):
        code, out, _ = run("check", rules, *extra)
        assert code == 1
    [result] = json.loads(out)["checks"]
    assert [sample["row"] for sample in result["samples"]] == [1, 3]  # the exact value is in


def test_an_invalid_explicit_octal_is_a_rules_error(tmp_path: Path) -> None:
    rules = str(project(tmp_path, "{rule: range, column: v, min: !!int 0" + "9" * 5000 + "}"))
    code, _, err = run("validate", rules)
    assert code == 2
    assert err.startswith(
        f"error: {rules}: invalid YAML at line 3, column 40: cannot read the value"
    )
    assert "internal error" not in err


def test_min_greater_than_max_names_a_huge_bound_by_its_digits(tmp_path: Path) -> None:
    rules = str(
        project(tmp_path, "{rule: range, column: v, min: !!int 0x" + "f" * 5000 + ", max: 1}")
    )
    code, _, err = run("validate", rules)
    assert code == 2
    assert err.endswith(f"min (<{display.digits(HEX)}-digit number>) is greater than max (1)\n")


def test_numbers_are_shown_without_passing_the_text_limit() -> None:
    assert display.digits(10**5000) == 5001
    assert display.fits_text(10**4299)
    assert not display.fits_text(10**4300)
    assert display.number(10**4300) == "<4301-digit number>"
    assert display.number(Decimal(10**5000)) == "<5001-digit number>"
    assert display.number(12) == "12"
    assert display.bound(date(2024, 1, 31)) == "2024-01-31"
    assert display.bound(True) == "True"
    assert describe_value(-(10**5000)) == "integer <5001-digit number>"
    assert describe_value(Decimal(10**5000)) == "number <5001-digit number>"
