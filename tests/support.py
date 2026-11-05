"""Helpers shared by the tests.

Tests assert RowGate's own wording exactly. Text that comes from a
dependency (PyYAML, pydantic, RE2, click, DuckDB) can change between
versions, so for those messages tests assert where the problem is (check
number, field, line and column) and the exit code, not the wording.
"""

from __future__ import annotations

import re

_LOCATION = re.compile(
    r"(?P<where>"
    r"(?:check [0-9]+(?: \([a-z_]+\))?(?:, field '[^']*')?|field '[^']*')"
    r"(?:, item [0-9]+)?"
    r"): (?P<message>.*)",
    re.DOTALL,
)
_YAML_POSITION = re.compile(r"invalid YAML at line (?P<line>[0-9]+), column (?P<column>[0-9]+): ")


def split_problem(problem: str) -> tuple[str, str]:
    """Split ``check 3 (not_null), field 'column': message`` into location and message."""
    match = _LOCATION.fullmatch(problem)
    if match is None:
        return "", problem
    return match["where"], match["message"]


def yaml_position(problem: str) -> tuple[int, int]:
    """The (line, column) of an ``invalid YAML at line L, column C: ...`` problem."""
    match = _YAML_POSITION.match(problem)
    assert match is not None, f"not a positioned YAML problem: {problem!r}"
    return int(match["line"]), int(match["column"])
