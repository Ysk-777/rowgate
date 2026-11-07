"""Which column types each rule accepts, and the SQL condition its rows fail on.

SQL safety: the fragments below are fixed text, column names passed through
``quote_identifier``, and ``$name`` placeholders whose values are bound as
parameters. No value from the rules file is written into SQL text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rowgate.rules import InSet, NotNull, Range, Regex
from rowgate.sql import quote_identifier

Family = Literal["text", "integer", "decimal", "float", "boolean", "date", "timestamp", "other"]
RowRule = NotNull | InSet | Range | Regex

INTEGER_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }
)


def type_family(column_type: str) -> Family:
    """Group a DuckDB column type: CSV columns are always VARCHAR, so always text."""
    if column_type == "VARCHAR":
        return "text"
    if column_type in INTEGER_TYPES:
        return "integer"
    if column_type.startswith("DECIMAL"):
        return "decimal"
    if column_type in ("FLOAT", "DOUBLE"):
        return "float"
    if column_type == "BOOLEAN":
        return "boolean"
    if column_type == "DATE":
        return "date"
    if column_type.startswith("TIMESTAMP"):
        return "timestamp"
    return "other"


def type_problem(check: RowRule, column_type: str) -> str | None:
    """Why ``check`` cannot run on a column of this type, or None if it can."""
    return None  # not_null and regex run on any column


@dataclass(frozen=True)
class RowTest:
    """How a row-level check finds its failing rows in one pass over the data."""

    fails: str  # SQL condition, true for a failing row
    shown: str  # SQL text form of the column, for samples
    params: dict[str, object]


def row_test(index: int, check: RowRule, column_type: str) -> RowTest:
    """The failing-row condition for check number ``index`` on a column of this type.

    Only not_null looks at NULLs; every other rule ignores them.
    """
    column = quote_identifier(check.column)
    family = type_family(column_type)
    shown = column if family == "text" else f"CAST({column} AS VARCHAR)"
    if isinstance(check, NotNull):
        return RowTest(f"{column} IS NULL", shown, {})
    if isinstance(check, Regex):
        # Full match with RE2 and no flags, on the DuckDB text form of the value.
        name = f"c{index}_pattern"
        return RowTest(
            f"{column} IS NOT NULL AND NOT regexp_full_match({shown}, ${name})",
            shown,
            {name: check.pattern},
        )
    raise AssertionError(f"{check.rule} is not available yet")  # refused before the run
