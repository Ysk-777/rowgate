"""Which column types each rule accepts, and the SQL condition its rows fail on.

SQL safety: the fragments below are fixed text, column names passed through
``quote_identifier``, and ``$name`` placeholders whose values are bound as
parameters. No value from the rules file is written into SQL text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rowgate.rules import InSet, NotNull, Range, Regex
from rowgate.sql import quote_identifier, text_as_date, text_as_integer, text_as_number

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
NUMERIC: tuple[Family, ...] = ("integer", "decimal", "float")
_KIND_WORDS = {"text": "text", "number": "numbers", "boolean": "booleans"}


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
    family = type_family(column_type)
    if isinstance(check, InSet):
        return _in_set_problem(check, column_type, family)
    if isinstance(check, Range):
        return _range_problem(check, column_type, family)
    return None  # not_null and regex run on any column


def _in_set_problem(check: InSet, column_type: str, family: Family) -> str | None:
    words = _KIND_WORDS[check.kind]
    if family == "text":
        return None
    if family in NUMERIC:
        if check.kind == "number":
            return None
        return (
            f"column {check.column!r} holds numbers ({column_type}), but the values are "
            f"{words}; write them as numbers, without quotes"
        )
    if family == "boolean":
        if check.kind == "boolean":
            return None
        return (
            f"column {check.column!r} holds booleans (BOOLEAN), but the values are {words}; "
            "write true or false"
        )
    return f"in_set does not support {column_type} columns (column {check.column!r})"


def _range_problem(check: Range, column_type: str, family: Family) -> str | None:
    if family == "timestamp":
        return f"column {check.column!r} is {column_type}; range on timestamps is not supported yet"
    if check.kind == "number":
        if family == "text" or family in NUMERIC:
            return None
        if family == "date":
            return (
                f"column {check.column!r} holds dates (DATE), but the bounds are numbers; "
                "write them as dates, like 2024-01-31"
            )
    else:
        if family in ("text", "date"):
            return None
        if family in NUMERIC:
            return (
                f"column {check.column!r} holds numbers ({column_type}), but the bounds are "
                "dates; write them as numbers"
            )
    return f"range does not support {column_type} columns (column {check.column!r})"


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
    prefix = f"c{index}_"
    if isinstance(check, NotNull):
        return RowTest(f"{column} IS NULL", shown, {})
    if isinstance(check, Regex):
        # Full match with RE2 and no flags, on the DuckDB text form of the value.
        name = f"{prefix}pattern"
        return RowTest(
            f"{column} IS NOT NULL AND NOT regexp_full_match({shown}, ${name})",
            shown,
            {name: check.pattern},
        )
    if isinstance(check, InSet):
        passes, params = _in_set(check, column, family, prefix)
    else:
        passes, params = _in_range(check, column, family, prefix)
    return RowTest(f"{column} IS NOT NULL AND NOT coalesce({passes}, false)", shown, params)


def _in_set(
    check: InSet, column: str, family: Family, prefix: str
) -> tuple[str, dict[str, object]]:
    # Membership is written as "value IN (SELECT unnest($set))", which DuckDB
    # runs as a hash join. list_contains compares each row with every member:
    # with 1,000 values on 3 million rows it took 12.4 s instead of 0.4 s.
    if check.kind == "text":
        name = f"{prefix}values"
        return f"{column} IN (SELECT unnest(${name}))", {name: list(check.values)}
    if check.kind == "boolean":
        name = f"{prefix}values"
        if family == "boolean":
            return f"{column} IN (SELECT unnest(${name}))", {name: [bool(v) for v in check.values]}
        # In text, only the lowercase words true and false are booleans.
        words = sorted({"true" if value else "false" for value in check.values})
        return f"{column} IN (SELECT unnest(${name}))", {name: words}
    return _in_number_set(check, column, family, prefix)


def _in_number_set(
    check: InSet, column: str, family: Family, prefix: str
) -> tuple[str, dict[str, object]]:
    ints = [value for value in check.values if isinstance(value, int)]
    floats = [float(value) for value in check.values if isinstance(value, float)]
    params: dict[str, object] = {}
    if family in ("decimal", "float"):
        params[f"{prefix}numbers"] = [float(value) for value in check.values]
        return f"CAST({column} AS DOUBLE) IN (SELECT unnest(${prefix}numbers))", params
    whole = text_as_integer(column) if family == "text" else column
    # A whole number is compared exactly with whole numbers in the set, and as
    # a DOUBLE with the decimal ones.
    exact: list[str] = []
    if ints:
        params[f"{prefix}ints"] = ints
        exact.append(f"({whole}) IN (SELECT unnest(${prefix}ints))")
    if floats:
        params[f"{prefix}floats"] = floats
        exact.append(f"CAST({whole} AS DOUBLE) IN (SELECT unnest(${prefix}floats))")
    whole_match = " OR ".join(exact)
    if family == "integer":
        return whole_match, params
    # Text: 01 is the whole number 1; 1.0 and 1e0 are compared as DOUBLE.
    params[f"{prefix}numbers"] = [float(value) for value in check.values]
    number = text_as_number(column)
    return (
        f"CASE WHEN {whole} IS NOT NULL THEN {whole_match} "
        f"ELSE ({number}) IN (SELECT unnest(${prefix}numbers)) END",
        params,
    )


def _in_range(
    check: Range, column: str, family: Family, prefix: str
) -> tuple[str, dict[str, object]]:
    params: dict[str, object] = {}
    parts: list[str] = []
    bounds = [(check.min, ">=", f"{prefix}min"), (check.max, "<=", f"{prefix}max")]
    if family == "text" and check.kind == "number":
        number = text_as_number(column)
        whole = text_as_integer(column)
        parts.append(f"isfinite({number})")  # nan, inf and 1e400 fail
        for bound, op, name in bounds:
            if bound is None:
                continue
            params[name] = bound
            if isinstance(bound, int):
                # Whole numbers exactly, as HUGEINT; anything else as DOUBLE.
                parts.append(f"coalesce({whole} {op} ${name}, {number} {op} ${name})")
            else:
                parts.append(f"{number} {op} ${name}")
        return " AND ".join(parts), params
    value = column
    if family == "text":  # dates written as text
        value = text_as_date(column)
        parts.append(f"{value} IS NOT NULL")
    elif family == "float":
        parts.append(f"isfinite({column})")  # NaN and infinity fail
    for bound, op, name in bounds:
        if bound is None:
            continue
        params[name] = bound
        parts.append(f"{value} {op} ${name}")
    return " AND ".join(parts), params
