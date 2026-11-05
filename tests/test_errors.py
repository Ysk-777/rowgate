from __future__ import annotations

import pytest

from rowgate.errors import RowGateError, RulesError


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
