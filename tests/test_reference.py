"""RowGate agrees with a simple pure-Python reference on generated CSV files.

Hypothesis generates CSV files with quoted fields, line breaks inside
quotes, empty fields (NULL), empty text, Unicode, and many spellings of
numbers and dates, plus a rules file using all seven rules. The reference
below computes each check's failing rows directly from the generated cells,
with exact Decimal arithmetic; RowGate must report the same failing-row
counts and the same samples (row numbers and values, in file order).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from rowgate.checks import DEFAULT_SAMPLES, SAMPLE_CHARS, run_checks
from rowgate.loader import load_rules

NUMBER = re.compile(r"[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][-+]?[0-9]+)?")
DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
DOUBLE_MAX = Decimal("1.7976931348623157e308")

COLUMNS = ["a", "b", "n"]

EDGE_TEXTS = [
    "", " ", "abc", "ABC", "a b", "x,y", 'q"uote', "line\nbreak", "café", "中文",
    "\U0001f600", "#hash", "NULL", "true", "false", "TRUE", "1", "01", "+1", "-0", "1.0", "1.",
    ".5", "1e0", "1E+3", "nan", "NaN", "inf", "Infinity", " 1", "1 ", "1_000", "0x10",
    "9007199254740992", "9007199254740993", "1000000000000000", "1000000000000000.01",
    "1000000000000000.00", "0.3", "0.30000000000000004", "0.29999999999999999", "1e400",
    "-1e400", "1e-400", "-1e-400", "1" + "0" * 40, "1" + "0" * 39 + "1", "2024-01-31",
    "2024-02-29", "2023-02-29", "2024-02-30", "2024-13-01", "01/02/2024", "2024-1-31",
    "9999-12-31", "1000-01-01", "a" * 85,
]  # fmt: skip
EDGE_NUMBERS = [
    Decimal("0"), Decimal("1"), Decimal("-1"), Decimal("0.3"), Decimal("1000000000000000"),
    Decimal("1000000000000000.005"), Decimal("9007199254740992"), Decimal("0.5"),
    Decimal("1.0"), Decimal("1000"), Decimal("1E+40"),
]  # fmt: skip
EDGE_DATES = [date(2024, 1, 31), date(2024, 2, 29), date(2023, 12, 31), date(9999, 12, 31)]
# Patterns on which RE2 (DuckDB) and Python's re agree: ASCII classes, no \d or (?i).
PATTERNS = ["[a-z]+", ".*", ".+", "[0-9]{1,3}", "a|abc", "[^,]*", "[A-Z][a-z]*", "1(\\.0)?"]

text_cells = st.one_of(
    st.sampled_from(EDGE_TEXTS),
    st.text(
        st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00\r"),
        max_size=12,
    ),
    st.decimals(allow_nan=False, allow_infinity=False, places=None).map(str),
    st.integers(-(10**80), 10**80).map(str),
)
cells = st.one_of(st.none(), text_cells)
# Whole numbers of any size are allowed in the rules: up to 80 digits here,
# with the edges of DuckDB's HUGEINT, and written plainly or with ".0".
HUGEINT_EDGES = [2**127 - 1, 2**127, -(2**127), -(2**127) - 1, 10**39 - 1, 10**40 - 1]
numbers = st.one_of(
    st.sampled_from(EDGE_NUMBERS),
    st.integers(-(10**80), 10**80).map(Decimal),
    st.sampled_from(HUGEINT_EDGES).map(Decimal),
)


@dataclass(frozen=True)
class Case:
    rows: list[list[str | None]]
    quote_all: bool
    row_min: int | None
    row_max: int | None
    set_kind: str
    text_members: list[str]
    number_members: list[Decimal]
    boolean_members: list[bool]
    range_kind: str
    number_bounds: tuple[Decimal | None, Decimal | None]
    date_bounds: tuple[date | None, date | None]
    pattern: str
    point_zero: bool  # write whole numbers in the rules as 12.0 instead of 12


@st.composite
def cases(draw: st.DrawFn) -> Case:
    rows = draw(st.lists(st.lists(cells, min_size=3, max_size=3), max_size=25))
    low, high = sorted(draw(st.lists(numbers, min_size=2, max_size=2)))
    first, last = sorted(draw(st.lists(st.sampled_from(EDGE_DATES), min_size=2, max_size=2)))
    keep = draw(st.sampled_from(["both", "min", "max"]))
    members = draw(st.lists(numbers, min_size=1, max_size=4))
    # Values next to the bounds and members, so the exact comparison is tested
    # where it matters, written plainly, with ".0" or with an exponent.
    for anchor, offset, style in draw(
        st.lists(
            st.tuples(
                st.sampled_from([low, high, *members]),
                st.sampled_from([-1, 0, 1]),
                st.sampled_from(["plain", "point", "exponent"]),
            ),
            max_size=6,
        )
    ):
        value = anchor + offset
        text = format(value, "f")
        if style == "point" and "." not in text:
            text += ".0"
        elif style == "exponent":
            text = f"{value:E}".replace("E+", "e")
        rows.append([text, None, text])
    return Case(
        rows=rows,
        quote_all=draw(st.booleans()),
        row_min=draw(st.none() | st.integers(0, 30)),
        row_max=draw(st.none() | st.integers(30, 40)),
        set_kind=draw(st.sampled_from(["text", "number", "boolean"])),
        text_members=draw(st.lists(text_cells, min_size=1, max_size=4)),
        number_members=members,
        boolean_members=draw(st.lists(st.booleans(), min_size=1, max_size=2)),
        range_kind=draw(st.sampled_from(["number", "date"])),
        number_bounds=(low if keep != "max" else None, high if keep != "min" else None),
        date_bounds=(first if keep != "max" else None, last if keep != "min" else None),
        pattern=draw(st.sampled_from(PATTERNS)),
        point_zero=draw(st.booleans()),
    )


# --- writing the files ----------------------------------------------------------------------


def csv_bytes(case: Case) -> bytes:
    def field(value: str | None) -> str:
        if value is None:
            return ""  # an empty field is NULL
        plain = value != "" and re.fullmatch(r"[A-Za-z0-9.+\-]+", value) is not None
        if plain and not case.quote_all:
            return value
        return '"' + value.replace('"', '""') + '"'  # "" is the empty text

    lines = [",".join(COLUMNS)] + [",".join(field(value) for value in row) for row in case.rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


# YAML needs these escaped in a double-quoted scalar: control characters,
# its own line separators, the byte-order mark and the two non-characters.
_YAML_ESCAPE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029\\ufeff\\ufffe\\uffff]")


def yaml_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{_YAML_ESCAPE.sub(_escape, escaped)}"'


def _escape(match: re.Match[str]) -> str:
    return f"\\u{ord(match[0]):04x}"


def yaml_number(value: Decimal, point_zero: bool) -> str:
    """A YAML number that RowGate reads back as exactly ``value``, of any size.

    Whole numbers are written as plain integers, or with ".0" if
    ``point_zero``; both must mean the same.
    """
    if value == value.to_integral_value():
        whole = str(int(value))
        return whole + ".0" if point_zero else whole
    return format(value, "f")


def rules_text(case: Case) -> str:
    if case.set_kind == "text":
        members = ", ".join(map(yaml_string, case.text_members))
    elif case.set_kind == "number":
        members = ", ".join(yaml_number(value, case.point_zero) for value in case.number_members)
    else:
        members = ", ".join("true" if value else "false" for value in case.boolean_members)
    if case.range_kind == "number":
        low, high = (
            None if b is None else yaml_number(b, case.point_zero) for b in case.number_bounds
        )
    else:
        low, high = (None if b is None else b.isoformat() for b in case.date_bounds)
    bounds = ", ".join(f"{key}: {value}" for key, value in (("min", low), ("max", high)) if value)
    counts = ", ".join(
        f"{key}: {value}"
        for key, value in (("min", case.row_min), ("max", case.row_max))
        if value is not None
    )
    checks = [
        "{rule: columns_exist, columns: [a, n, missing]}",
        f"{{rule: row_count, {counts or 'min: 0'}}}",
        "{rule: not_null, column: a}",
        "{rule: unique, column: a}",
        f"{{rule: in_set, column: a, values: [{members}]}}",
        f"{{rule: range, column: n, {bounds}}}",
        f"{{rule: regex, column: b, pattern: {yaml_string(case.pattern)}}}",
    ]
    body = "".join(f"  - {check}\n" for check in checks)
    return f"version: 1\nsource: {{path: data.csv}}\nchecks:\n{body}"


# --- the reference ----------------------------------------------------------------------------


def plain_number(value: str) -> Decimal | None:
    return Decimal(value) if NUMBER.fullmatch(value) else None


def real_date(value: str) -> date | None:
    match = DATE.fullmatch(value)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def in_set_passes(case: Case, value: str) -> bool:
    if case.set_kind == "text":
        return value in case.text_members
    if case.set_kind == "boolean":
        return value in {"true" if member else "false" for member in case.boolean_members}
    number = plain_number(value)
    return number is not None and any(number == member for member in case.number_members)


def range_passes(case: Case, value: str) -> bool:
    if case.range_kind == "number":
        number = plain_number(value)
        # Plain, and finite as a 64-bit float (1e400 would be infinity).
        if number is None or abs(number) > DOUBLE_MAX:
            return False
        low, high = case.number_bounds
        return (low is None or number >= low) and (high is None or number <= high)
    day = real_date(value)
    if day is None:
        return False
    first, last = case.date_bounds
    return (first is None or day >= first) and (last is None or day <= last)


def reference(case: Case) -> list[tuple[int | None, list[tuple[object, ...]]]]:
    """For each check: its failing-row count and its samples, as RowGate shows them."""
    a = [row[0] for row in case.rows]
    b = [row[1] for row in case.rows]
    n = [row[2] for row in case.rows]

    def failing(column: list[str | None], fails: object) -> tuple[int, list[tuple[object, ...]]]:
        assert callable(fails)
        rows = [(i + 1, v) for i, v in enumerate(column) if fails(v)]
        return len(rows), [(row, cut(value)) for row, value in rows[:DEFAULT_SAMPLES]]

    counts: dict[str, list[int]] = {}
    for i, value in enumerate(a):
        if value is not None:
            counts.setdefault(value, []).append(i + 1)
    repeated: list[tuple[object, ...]] = sorted(
        (rows[0], cut(value), len(rows)) for value, rows in counts.items() if len(rows) > 1
    )
    return [
        (None, []),  # columns_exist
        (None, []),  # row_count
        failing(a, lambda v: v is None),
        (sum(len(rows) for rows in counts.values() if len(rows) > 1), repeated[:DEFAULT_SAMPLES]),
        failing(a, lambda v: v is not None and not in_set_passes(case, v)),
        failing(n, lambda v: v is not None and not range_passes(case, v)),
        failing(b, lambda v: v is not None and re.fullmatch(case.pattern, v) is None),
    ]


def cut(value: str | None) -> str | None:
    return None if value is None else value[:SAMPLE_CHARS]


# --- the comparison ---------------------------------------------------------------------------


@given(cases())
def test_rowgate_agrees_with_the_reference(case: Case) -> None:
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "data.csv").write_bytes(csv_bytes(case))
        rules = Path(folder) / "rules.yaml"
        rules.write_text(rules_text(case), encoding="utf-8")
        report = run_checks(load_rules(rules), rules)
    assert report.row_count == len(case.rows)
    columns_exist, row_count, *row_checks = report.results
    assert not columns_exist.ok  # "missing" is never a column
    expected_count_ok = (case.row_min is None or len(case.rows) >= case.row_min) and (
        case.row_max is None or len(case.rows) <= case.row_max
    )
    assert row_count.ok == expected_count_ok
    for result, (count, samples) in zip(row_checks, reference(case)[2:], strict=True):
        found = [
            (s.row, s.value[:SAMPLE_CHARS] if s.value is not None else None)
            + (() if s.count is None else (s.count,))
            for s in result.samples or ()
        ]
        assert (result.rule, result.failing_rows, found) == (result.rule, count, samples)
