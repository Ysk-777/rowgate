from __future__ import annotations

import pytest

from rowgate.errors import DataError, RowGateError, RulesError


def test_plain_error_renders_one_line() -> None:
    assert RowGateError("data file not found").lines() == ["error: data file not found"]


def test_rules_error_with_one_problem_renders_one_line() -> None:
    error = RulesError("rules.yaml", ["file not found"])
    assert error.lines() == ["error: rules.yaml: file not found"]
    assert error.message == "invalid rules file 'rules.yaml' (1 problem)"


def test_rules_error_with_several_problems_renders_a_list() -> None:
    error = RulesError("rules.yaml", ["first", "second"])
    assert error.lines() == [
        "error: invalid rules file 'rules.yaml' (2 problems):",
        "  - first",
        "  - second",
    ]


def test_rules_error_needs_a_problem() -> None:
    with pytest.raises(ValueError, match="at least one problem"):
        RulesError("rules.yaml", [])


def test_data_error_renders_its_details_indented() -> None:
    error = DataError("check 3 (not_null): no column 'x'", ["the file has: 'a', 'b'"])
    assert error.lines() == ["error: check 3 (not_null): no column 'x'", "  the file has: 'a', 'b'"]
    assert DataError("plain").lines() == ["error: plain"]


def test_errors_without_a_duckdb_message_have_no_hint_or_debug_lines() -> None:
    error = DataError("plain")
    assert error.lines() == error.lines(debug=True) == ["error: plain"]
    assert error.debug_lines() == []
    assert RulesError("r.yaml", ["bad"]).lines(debug=True) == ["error: r.yaml: bad"]


def test_a_duckdb_message_adds_a_hint_and_debug_lines() -> None:
    error = RowGateError("could not read", duckdb_message="IO Error: one\ntwo")
    assert error.lines() == [
        "error: could not read; run again with --debug to see DuckDB's own message, which can "
        "include data values from the file"
    ]
    assert error.lines(debug=True) == ["error: could not read"]
    assert error.debug_lines() == [
        "DuckDB's original message (it can include data values from the file):",
        "  IO Error: one",
        "  two",
    ]


def test_details_follow_the_hint() -> None:
    error = DataError("could not read", ["detail"], duckdb_message="x")
    assert error.lines()[1:] == ["  detail"]
    assert error.lines()[0].startswith("error: could not read; run again with --debug")
