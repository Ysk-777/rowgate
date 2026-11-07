"""Which column types each rule accepts, and the SQL condition its rows fail on.

SQL safety: the fragments below are fixed text, column names passed through
``quote_identifier``, and ``$name`` placeholders whose values are bound as
parameters. No value from the rules file is written into SQL text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from rowgate import numbers
from rowgate.display import quoted
from rowgate.rules import HUGEINT_MAX, HUGEINT_MIN, InSet, NotNull, Range, Regex
from rowgate.sql import (
    NUMBER_PATTERN,
    number_compare,
    number_key,
    quote_identifier,
    text_as_date,
    text_as_integer,
    text_as_number,
)

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
_FLOAT_TYPES = {"FLOAT": "FLOAT", "DOUBLE": "DOUBLE"}  # fixed SQL text for each type
_DECIMAL_TYPE = re.compile(r"DECIMAL\(([0-9]+),([0-9]+)\)")
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
            f"column {quoted(check.column)} holds numbers ({column_type}), but the values are "
            f"{words}; write them as numbers, without quotes"
        )
    if family == "boolean":
        if check.kind == "boolean":
            return None
        return (
            f"column {quoted(check.column)} holds booleans (BOOLEAN), but the values are {words}; "
            "write true or false"
        )
    return f"in_set does not support {column_type} columns (column {quoted(check.column)})"


def _range_problem(check: Range, column_type: str, family: Family) -> str | None:
    if family == "timestamp":
        return (
            f"column {quoted(check.column)} is {column_type}; "
            "range on timestamps is not supported yet"
        )
    if check.kind == "number":
        if family == "text" or family in NUMERIC:
            return None
        if family == "date":
            return (
                f"column {quoted(check.column)} holds dates (DATE), but the bounds are numbers; "
                "write them as dates, like 2024-01-31"
            )
    else:
        if family in ("text", "date"):
            return None
        if family in NUMERIC:
            return (
                f"column {quoted(check.column)} holds numbers ({column_type}), but the bounds are "
                "dates; write them as numbers"
            )
    return f"range does not support {column_type} columns (column {quoted(check.column)})"


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


@dataclass
class Scan:
    """What one scan of the data selects: the columns and computed values the checks use.

    Row tests ask for their columns and conversions here, so the scan reads
    exactly the columns the checks name, and a conversion that several
    conditions use, such as text to number, is computed once per row.
    """

    column_names: list[str]
    columns: list[str] = field(init=False, default_factory=list)  # quoted, in order of use
    computed: dict[str, str] = field(init=False, default_factory=dict)  # SQL -> its name
    _taken: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        # DuckDB compares names ignoring ASCII case, so new names must not
        # clash with a data column that way either.
        self._taken = {name.translate(_ASCII_LOWER) for name in self.column_names}

    def column(self, name: str) -> str:
        """The quoted name of a data column, which the scan will select."""
        quoted = quote_identifier(name)
        if quoted not in self.columns:
            self.columns.append(quoted)
        return quoted

    def value(self, expression: str) -> str:
        """The name under which the scan computes ``expression`` once per row."""
        name = self.computed.get(expression)
        if name is None:
            name = self.new_name(f"rowgate_value_{len(self.computed) + 1}")
            self.computed[expression] = name
        return name

    def new_name(self, base: str) -> str:
        """A quoted name that no data column and no other computed value has."""
        name = base
        while name in self._taken:
            name += "_"
        self._taken.add(name)
        return quote_identifier(name)

    def select_list(self) -> list[str]:
        """The scan's select list: the columns, then each computed value with its name."""
        return [*self.columns, *(f"{sql} AS {name}" for sql, name in self.computed.items())]


@dataclass(frozen=True)
class RowTest:
    """How a row-level check finds its failing rows in one pass over the data."""

    fails: str  # SQL condition, true for a failing row
    shown: str  # SQL text form of the column, for samples
    params: dict[str, object]


def row_test(index: int, check: RowRule, column_type: str, scan: Scan) -> RowTest:
    """The failing-row condition for check number ``index`` on a column of this type.

    Only not_null looks at NULLs; every other rule ignores them. The
    condition refers only to names that ``scan`` selects.
    """
    column = scan.column(check.column)
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
        passes, params = _in_set(check, column, column_type, family, prefix, scan)
    else:
        passes, params = _in_range(check, column, column_type, family, prefix, scan)
    return RowTest(f"{column} IS NOT NULL AND NOT coalesce({passes}, false)", shown, params)


def _in_set(
    check: InSet, column: str, column_type: str, family: Family, prefix: str, scan: Scan
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
    return _in_number_set(check, column, column_type, family, prefix, scan)


def _in_number_set(
    check: InSet, column: str, column_type: str, family: Family, prefix: str, scan: Scan
) -> tuple[str, dict[str, object]]:
    name = f"{prefix}values"
    members = [value for value in check.values if not isinstance(value, bool | str)]
    if family == "float":
        # Each member becomes the column's own type, so a FLOAT value stored as
        # 0.1 is in [0.1]. Members too large for the type would be infinity,
        # which is never in a set.
        texts = [str(value) for value in members]
        sql_type = _FLOAT_TYPES[column_type]
        return (
            f"{column} IN (SELECT member FROM (SELECT CAST(unnest(${name}) AS {sql_type}) "  # noqa: S608
            "AS member) WHERE isfinite(member))",
            {name: texts},
        )
    if family == "decimal":
        precision, scale = _decimal_type(column_type)
        # Only members with at most `scale` decimals that fit the type can be equal.
        exact = [numbers.scaled_exact(value, scale) for value in members]
        texts = [
            numbers.at_scale(unscaled, scale)
            for unscaled in exact
            if unscaled is not None and abs(unscaled) < 10**precision
        ]
        if not texts:
            return "false", {}
        return (
            f"{column} IN (SELECT CAST(unnest(${name}) AS DECIMAL({precision}, {scale})))",
            {name: texts},
        )
    ints = _whole_members(members)
    whole_match = f"{{whole}} IN (SELECT unnest(${name}))" if ints else "false"
    params: dict[str, object] = {name: ints} if ints else {}
    if family == "integer":
        return whole_match.format(whole=column), params
    # Text: a plain whole number is compared as a HUGEINT; any other plain
    # number (1.0, 1e0, 0.30000000000000004, or more than 38 digits) by its
    # exact digits. Both are exact: 01 is 1, and 1000000000000000.01 is not
    # 1000000000000000.
    whole = scan.value(text_as_integer(column))
    keys = f"{prefix}keys"
    params[keys] = [numbers.parts(value).key for value in members]
    return (
        f"CASE WHEN {whole} IS NOT NULL THEN {whole_match.format(whole=whole)} "
        f"ELSE regexp_full_match({column}, '{NUMBER_PATTERN}') "
        f"AND {number_key(column)} IN (SELECT unnest(${keys})) END",
        params,
    )


def _whole_members(members: list[int | Decimal]) -> list[int]:
    """The members that are whole numbers within HUGEINT, the range of integer columns."""
    whole = [numbers.scaled_exact(value, 0) for value in members]
    return [value for value in whole if value is not None and HUGEINT_MIN <= value <= HUGEINT_MAX]


def _decimal_type(column_type: str) -> tuple[int, int]:
    match = _DECIMAL_TYPE.fullmatch(column_type)
    if match is None:
        raise AssertionError(f"unexpected decimal type {column_type!r}")
    return int(match[1]), int(match[2])


def _in_range(
    check: Range, column: str, column_type: str, family: Family, prefix: str, scan: Scan
) -> tuple[str, dict[str, object]]:
    params: dict[str, object] = {}
    parts: list[str] = []
    if check.kind == "date":
        value = column
        if family == "text":  # dates written as text
            value = scan.value(text_as_date(column))
            parts.append(f"{value} IS NOT NULL")
        for bound, op, name in _bounds(check, prefix):
            params[name] = bound
            parts.append(f"{value} {op} ${name}")
        return " AND ".join(parts), params
    if family == "float":
        # Compared in the column's own type: the bound becomes a FLOAT for a
        # FLOAT column, so a value stored as 0.1 passes max: 0.1. NaN and
        # infinity fail; DuckDB orders NaN above every number.
        sql_type = _FLOAT_TYPES[column_type]
        parts.append(f"isfinite({column})")
        for bound, op, name in _number_bounds(check, prefix):
            params[name] = str(bound)
            parts.append(f"{column} {op} CAST(${name} AS {sql_type})")
        return " AND ".join(parts), params
    if family in ("integer", "decimal"):
        precision, scale = _decimal_type(column_type) if family == "decimal" else (0, 0)
        for bound, op, name in _number_bounds(check, prefix):
            parts.append(_exact_bound(column, bound, op, name, precision, scale, params))
        return " AND ".join(parts), params
    # Text: the number must be plain and finite as a DOUBLE (nan, inf and 1e400
    # fail). It is compared as a DOUBLE where that settles the question, and
    # by its exact digits where its DOUBLE is next to the bound's.
    number = scan.value(text_as_number(column))
    parts.append(f"isfinite({number})")
    for bound, op, name in _number_bounds(check, prefix):
        below, above = numbers.neighbours(bound)
        bound_parts = numbers.parts(bound)
        params |= {
            f"{name}_below": below,
            f"{name}_above": above,
            f"{name}_sign": bound_parts.sign,
            f"{name}_digits": bound_parts.digits,
            f"{name}_exponent": bound_parts.exponent,
        }
        exact = number_compare(column, f"{name}_sign", f"{name}_digits", f"{name}_exponent")
        parts.append(
            f"(CASE WHEN {number} < ${name}_below THEN -1 WHEN {number} > ${name}_above THEN 1 "
            f"ELSE {exact} END) {op} 0"
        )
    return " AND ".join(parts), params


def _bounds(check: Range, prefix: str) -> list[tuple[int | Decimal | date, str, str]]:
    bounds = [(check.min, ">=", f"{prefix}min"), (check.max, "<=", f"{prefix}max")]
    return [(bound, op, name) for bound, op, name in bounds if bound is not None]


def _number_bounds(check: Range, prefix: str) -> list[tuple[int | Decimal, str, str]]:
    found: list[tuple[int | Decimal, str, str]] = []
    for bound, op, name in _bounds(check, prefix):
        if isinstance(bound, date):  # only possible if validation was bypassed
            raise AssertionError("a date bound in a range of numbers")
        found.append((bound, op, name))
    return found


def _exact_bound(
    column: str,
    bound: int | Decimal,
    op: str,
    name: str,
    precision: int,
    scale: int,
    params: dict[str, object],
) -> str:
    """``column op bound`` for an integer column (precision 0) or DECIMAL(precision, scale).

    The column's values are whole multiples of 10^-scale, so value <= bound
    exactly when value <= floor(bound) at that scale (and >= with ceil). That
    bound is exact in the column's type, unless it lies outside the type's
    range, where the answer is the same for every value.
    """
    if op == "<=":
        rounded = numbers.scaled_floor(bound, scale)
    else:
        rounded = numbers.scaled_ceil(bound, scale)
    # Integer columns from Parquet have at most 64 bits; HUGEINT holds them all.
    low, high = (HUGEINT_MIN, HUGEINT_MAX)
    if precision:
        low, high = -(10**precision) + 1, 10**precision - 1
    if rounded is None:  # more than 40 digits: far outside the type
        rounded = high + 1 if bound > 0 else low - 1
    if rounded > high:
        return "true" if op == "<=" else "false"
    if rounded < low:
        return "false" if op == "<=" else "true"
    if precision:
        params[name] = numbers.at_scale(rounded, scale)
        return f"{column} {op} CAST(${name} AS DECIMAL({precision}, {scale}))"
    params[name] = rounded
    return f"{column} {op} ${name}"
