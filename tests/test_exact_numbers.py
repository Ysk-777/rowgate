"""Numbers are compared exactly: decimal text, Parquet decimals and integers, and FLOAT."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from rowgate import numbers
from rowgate.checks import CheckResult, run_checks
from rowgate.conditions import Scan, row_test
from rowgate.errors import RulesError
from rowgate.loader import load_rules, parse_rules_yaml
from rowgate.rules import InSet, Range


def check(
    tmp_path: Path, rule: str, data: bytes | None = None, name: str = "data.csv"
) -> CheckResult:
    if data is not None:
        (tmp_path / name).write_bytes(data)
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        f"version: 1\nsource: {{path: {name}}}\nchecks:\n  - {{{rule}}}\n", encoding="utf-8"
    )
    [result] = run_checks(load_rules(rules), rules, max_samples=100).results
    return result


def failing(result: CheckResult) -> list[str | None]:
    return [sample.value for sample in result.samples or ()]


def column(*values: str) -> bytes:
    return ("v\n" + "".join(f"{value}\n" for value in values)).encode()


def parquet(tmp_path: Path, sql_type: str, *values: str) -> None:
    """A Parquet file with one column v of ``sql_type``; each value is SQL text."""
    rows = ", ".join(f"(CAST({value} AS {sql_type}))" for value in values)
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        f"COPY (SELECT * FROM (VALUES {rows}) t(v)) TO '{target}' (FORMAT parquet)"  # noqa: S608 - test data
    )


# --- decimal text in CSV, compared with range bounds -------------------------------------


@pytest.mark.parametrize(
    ("bound", "text"),
    [
        ("max: 1000000000000000", "1000000000000000.01"),
        ("max: 12345678.12345678901", "12345678.123456789012"),
        # A fixed DECIMAL(38,12) would round this to 0.3 and let it pass.
        ("max: 0.3", "0.30000000000000004"),
        ("max: 0.3", "0.300000000000000000000000000000000000000000000001"),
        ("min: 0.3", "0.29999999999999999"),
        ("max: 1000000000000000", "1.00000000000000001e15"),
        ("max: 0", "1e-400"),
        ("min: 0", "-1e-400"),
        ("max: 100000000000000000000000000000000000000", "100000000000000000000000000000000000001"),
    ],
)
def test_decimal_text_that_rounds_to_the_bound_still_fails(
    tmp_path: Path, bound: str, text: str
) -> None:
    # Each value has the same 64-bit float as the bound, so a DOUBLE comparison passes it.
    result = check(tmp_path, f"rule: range, column: v, {bound}", column(text))
    assert failing(result) == [text]


@pytest.mark.parametrize(
    ("bound", "text"),
    [
        ("max: 1000000000000000", "1000000000000000.00"),
        ("max: 12345678.12345678901", "12345678.123456789010"),
        ("max: 0.3", "0.3"),
        ("max: 0.3", "3e-1"),
        ("max: 0.3", "0.29999999999999999"),
        ("min: 0.3", "0.300000000000000000000000000000000000000000000001"),
        ("min: 0", "1e-400"),
        ("min: 0", "-0"),
        ("max: 0", "-0.000"),
        ("min: -0.0", "0"),
    ],
)
def test_decimal_text_within_the_bound_passes(tmp_path: Path, bound: str, text: str) -> None:
    result = check(tmp_path, f"rule: range, column: v, {bound}", column(text))
    assert (result.failing_rows, result.samples) == (0, ())


def test_more_than_38_digits_are_compared_exactly(tmp_path: Path) -> None:
    # No digit limit: 60 significant digits, on both sides of a 50-digit bound.
    bound = "1." + "0" * 48 + "1"
    below = "1." + "0" * 48 + "09" + "9" * 9
    above = "1." + "0" * 48 + "10" + "0" * 9 + "1"
    result = check(
        tmp_path, f"rule: range, column: v, max: {bound}", column(below, bound, above, "1")
    )
    assert failing(result) == [above]


def test_a_text_exponent_beyond_64_bits_fails_only_next_to_the_bound(
    tmp_path: Path,
) -> None:
    tiny = "1e-99999999999999999999"  # positive, but 0.0 as a DOUBLE
    huge = "1e99999999999999999999"  # infinity as a DOUBLE: fails any range
    zero = "0e99999999999999999999"
    result = check(tmp_path, "rule: range, column: v, min: 0", column(tiny, huge, zero))
    assert failing(result) == [huge]  # the sign alone settles tiny and zero
    # Next to a tiny bound with the same sign, only the exponent could settle
    # it, and it does not fit a 64-bit integer: a failing row.
    assert failing(check(tmp_path, "rule: range, column: v, max: 1.0e-400", column(tiny))) == [tiny]
    assert check(tmp_path, "rule: range, column: v, max: 5", column(tiny)).failing_rows == 0


# --- decimal text in CSV, compared with in_set members -----------------------------------


def test_in_set_compares_decimal_text_exactly(tmp_path: Path) -> None:
    data = column(
        "1000000000000000.01", "1000000000000000.00", "1e15", "0.30000000000000004", "0.300",
        "3e-1", "1" + "0" * 39, "1" + "0" * 38 + "1", "0.30000000000000001",
    )  # fmt: skip
    result = check(
        tmp_path, "rule: in_set, column: v, values: [1000000000000000, 0.3, 1.0e+39]", data
    )
    assert failing(result) == [
        "1000000000000000.01", "0.30000000000000004", "1" + "0" * 38 + "1", "0.30000000000000001",
    ]  # fmt: skip


# --- Parquet DECIMAL and integer columns --------------------------------------------------


def test_parquet_decimal_with_a_whole_bound_stays_exact(tmp_path: Path) -> None:
    # Regression: this was already right in M3 and must stay right.
    parquet(tmp_path, "DECIMAL(18,2)", "1000000000000000.01", "1000000000000000.00")
    result = check(tmp_path, "rule: range, column: v, max: 1000000000000000", name="data.parquet")
    assert failing(result) == ["1000000000000000.01"]


@pytest.mark.parametrize(
    ("bound", "expected"),
    [
        ("max: 1000000000000000.005", ["1000000000000000.01"]),
        ("min: 1000000000000000.005", ["1000000000000000.00", "-5.00"]),
        ("max: 1.0e+50", []),
        ("min: 1.0e+50", ["1000000000000000.01", "1000000000000000.00", "-5.00"]),
        ("max: -1.0e+50", ["1000000000000000.01", "1000000000000000.00", "-5.00"]),
        ("min: -99999999999999999.99", []),
        ("min: -1.0e+16", []),
    ],
)
def test_parquet_decimal_with_decimal_bounds(
    tmp_path: Path, bound: str, expected: list[str]
) -> None:
    # As DOUBLEs, 1000000000000000.01, .00 and .005 are all the same number.
    parquet(tmp_path, "DECIMAL(18,2)", "1000000000000000.01", "1000000000000000.00", "-5")
    result = check(tmp_path, f"rule: range, column: v, {bound}", name="data.parquet")
    assert failing(result) == expected


def test_in_set_on_parquet_decimal_is_exact(tmp_path: Path) -> None:
    parquet(tmp_path, "DECIMAL(18,2)", "1000000000000000.01", "1000000000000000.00", "0.5")
    result = check(
        tmp_path,
        "rule: in_set, column: v, values: [1000000000000000, 0.50, 0.505, 1.0e+30]",
        name="data.parquet",
    )
    assert failing(result) == ["1000000000000000.01"]
    # 0.005 cannot be a DECIMAL(18,2) value, so nothing is in this set.
    result = check(tmp_path, "rule: in_set, column: v, values: [0.005]", name="data.parquet")
    assert result.failing_rows == 3


@pytest.mark.parametrize(
    ("bound", "expected"),
    [
        ("max: 9007199254740992.5", ["9007199254740993"]),
        ("min: 9007199254740992.5", ["2", "9007199254740992"]),
        ("min: 2.5", ["2"]),
        ("max: 2.5", ["9007199254740992", "9007199254740993"]),
        ("max: 1.0e+40", []),
        ("min: 1.0e+40", ["2", "9007199254740992", "9007199254740993"]),
        ("min: -1.0e+60", []),
        ("max: -1.0e+60", ["2", "9007199254740992", "9007199254740993"]),
    ],
)
def test_parquet_integers_with_decimal_bounds(
    tmp_path: Path, bound: str, expected: list[str]
) -> None:
    parquet(tmp_path, "BIGINT", "2", "9007199254740992", "9007199254740993")
    result = check(tmp_path, f"rule: range, column: v, {bound}", name="data.parquet")
    assert failing(result) == expected


def test_in_set_on_parquet_integers_only_matches_whole_members(tmp_path: Path) -> None:
    parquet(tmp_path, "BIGINT", "1", "2", "9007199254740993")
    result = check(
        tmp_path,
        "rule: in_set, column: v, values: [1.0, 2.5, 9007199254740993.0]",
        name="data.parquet",
    )
    assert failing(result) == ["2"]
    result = check(tmp_path, "rule: in_set, column: v, values: [2.5]", name="data.parquet")
    assert result.failing_rows == 3


# --- Parquet FLOAT and DOUBLE --------------------------------------------------------------


@pytest.mark.parametrize("bound", ["max: 0.1", "min: 0.1", "min: 0.1, max: 0.1"])
def test_a_float_stored_as_0_1_is_within_0_1(tmp_path: Path, bound: str) -> None:
    # The FLOAT nearest to 0.1 is 0.100000001490116..., above the DOUBLE 0.1.
    parquet(tmp_path, "FLOAT", "0.1")
    result = check(tmp_path, f"rule: range, column: v, {bound}", name="data.parquet")
    assert (result.failing_rows, result.samples) == (0, ())


def test_float_bounds_become_floats(tmp_path: Path) -> None:
    parquet(tmp_path, "FLOAT", "0.1", "0.2", "'nan'", "3.4e38")
    rule = "rule: range, column: v, min: 0.10000001, max: 0.2"
    assert failing(check(tmp_path, rule, name="data.parquet")) == ["0.1", "nan", "3.4e+38"]
    # Bounds too large for a FLOAT become infinity.
    rule = "rule: range, column: v, max: 1.0e+39"
    assert failing(check(tmp_path, rule, name="data.parquet")) == ["nan"]
    rule = "rule: range, column: v, min: 1.0e+39"
    assert failing(check(tmp_path, rule, name="data.parquet")) == ["0.1", "0.2", "nan", "3.4e+38"]


def test_double_columns_compare_with_the_double_nearest_the_bound(tmp_path: Path) -> None:
    parquet(tmp_path, "DOUBLE", "0.1", "0.30000000000000004")
    result = check(tmp_path, "rule: range, column: v, min: 0.1, max: 0.3", name="data.parquet")
    assert failing(result) == ["0.30000000000000004"]
    result = check(tmp_path, "rule: range, column: v, max: 1.0e+400", name="data.parquet")
    assert result.failing_rows == 0


def test_in_set_on_float_columns_uses_the_column_type(tmp_path: Path) -> None:
    parquet(tmp_path, "FLOAT", "0.1", "'inf'", "0.2")
    rule = "rule: in_set, column: v, values: [0.1, 1.0e+39]"
    assert failing(check(tmp_path, rule, name="data.parquet")) == ["inf", "0.2"]
    parquet(tmp_path, "DOUBLE", "0.1", "'inf'", "0.2")
    rule = "rule: in_set, column: v, values: [0.1, 1.0e+400]"
    assert failing(check(tmp_path, rule, name="data.parquet")) == ["inf", "0.2"]


# --- the rules file keeps every digit ----------------------------------------------------


def test_yaml_numbers_keep_every_digit() -> None:
    loaded = parse_rules_yaml("v: [12345678.12345678901, 0.1, 1.5e+3, !!float 1_000.5]\n", "t")
    assert loaded == {
        "v": [Decimal("12345678.12345678901"), Decimal("0.1"), Decimal("1500"), Decimal("1000.5")]
    }


def test_numbers_beyond_what_python_decimal_holds_are_yaml_errors(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "version: 1\nsource: {path: d.csv}\n"
        "checks: [{rule: range, column: v, max: !!float 1e+1234567890123456789}]\n",
        encoding="utf-8",
    )
    with pytest.raises(RulesError) as caught:
        load_rules(rules)
    [problem] = caught.value.problems
    assert problem.startswith("invalid YAML at line 3, column 40: cannot read the value")


# --- the helpers in rowgate.numbers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "sign", "digits", "exponent", "key"),
    [
        (Decimal("120"), 1, "12", 3, "+3:12"),
        (Decimal("0.0012"), 1, "12", -2, "+-2:12"),
        (Decimal("-1.20E+3"), -1, "12", 4, "-4:12"),
        (Decimal("-0.00"), 0, "", 0, "0"),
        (7, 1, "7", 1, "+1:7"),
    ],
)
def test_number_parts(
    value: numbers.Number, sign: int, digits: str, exponent: int, key: str
) -> None:
    parts = numbers.parts(value)
    assert (parts.sign, parts.digits, parts.exponent, parts.key) == (sign, digits, exponent, key)


@pytest.mark.parametrize("helper", [numbers.parts, lambda value: numbers.scaled_floor(value, 2)])
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_number_helpers_refuse_nan_and_infinity(helper: object, value: Decimal) -> None:
    assert callable(helper)
    with pytest.raises(ValueError, match="not a finite number"):
        helper(value)


@pytest.mark.parametrize(
    ("value", "scale", "floor", "ceil", "exact"),
    [
        (Decimal("12.345"), 2, 1234, 1235, None),
        (Decimal("-12.345"), 2, -1235, -1234, None),
        (Decimal("2.50"), 1, 25, 25, 25),
        (Decimal("1e-50"), 2, 0, 1, None),
        (Decimal("-1e-50"), 2, -1, 0, None),
        (Decimal("1e50"), 0, None, None, None),
        (0, 5, 0, 0, 0),
        (
            170141183460469231731687303715884105727,
            0,
            *[170141183460469231731687303715884105727] * 3,
        ),
    ],
)
def test_scaled_floor_ceil_and_exact(
    value: numbers.Number, scale: int, floor: int | None, ceil: int | None, exact: int | None
) -> None:
    assert numbers.scaled_floor(value, scale) == floor
    assert numbers.scaled_ceil(value, scale) == ceil
    assert numbers.scaled_exact(value, scale) == exact


@pytest.mark.parametrize(
    ("unscaled", "scale", "text"),
    [(-1230, 2, "-12.30"), (5, 3, "0.005"), (0, 2, "0.00"), (42, 0, "42"), (-5, 2, "-0.05")],
)
def test_at_scale(unscaled: int, scale: int, text: str) -> None:
    assert numbers.at_scale(unscaled, scale) == text


def test_neighbours() -> None:
    below, above = numbers.neighbours(Decimal("0.1"))
    assert below < 0.1 < above
    assert numbers.neighbours(Decimal("1e400")) == (1.7976931348623157e308, float("inf"))


# --- conditions refuse inputs that validation rules out ----------------------------------


def test_conditions_refuse_what_validation_rules_out() -> None:
    dated = Range.model_construct(rule="range", column="v", min=Decimal(1), max=date(2024, 1, 1))
    with pytest.raises(AssertionError, match="date bound in a range of numbers"):
        row_test(1, dated, "BIGINT", Scan(["v"]))
    members = InSet(rule="in_set", column="v", values=[1])
    with pytest.raises(AssertionError, match="unexpected decimal type"):
        row_test(1, members, "DECIMAL", Scan(["v"]))


# --- how the exact comparison is computed ---------------------------------------------------


def test_columns_named_like_the_lambda_parameters_change_nothing(tmp_path: Path) -> None:
    # DuckDB reads name.field as a column called name when the query selects
    # one. Here the checks select struct columns named like RowGate's lambda
    # parameters, with the same fields; struct_extract keeps them out.
    target = tmp_path / "data.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT v, {'s': '-', 'i': '9', 'f': '9', 'x': '9'} AS rowgate_pieces, "  # noqa: S608 - test data
        "{'sign': -1, 'digits': '9', 'exponent': 99} AS rowgate_number, '99' AS rowgate_core "
        "FROM (VALUES ('0.30000000000000004'), ('0.3'), ('1e15'), ('1000000000000000.01')) t(v)) "
        f"TO '{target}' (FORMAT parquet)"
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "version: 1\nsource: {path: data.parquet}\nchecks:\n"
        "  - {rule: not_null, column: rowgate_pieces}\n"
        "  - {rule: not_null, column: rowgate_number}\n"
        "  - {rule: not_null, column: rowgate_core}\n"
        "  - {rule: range, column: v, max: 0.3}\n"
        "  - {rule: in_set, column: v, values: [0.3, 1000000000000000]}\n",
        encoding="utf-8",
    )
    *_, in_range, in_set = run_checks(load_rules(rules), rules).results
    assert failing(in_range) == ["0.30000000000000004", "1e15", "1000000000000000.01"]
    assert failing(in_set) == ["0.30000000000000004", "1000000000000000.01"]


def test_digits_are_worked_out_only_for_values_next_to_a_bound() -> None:
    scan = Scan(["v"])
    test = row_test(
        1, Range(rule="range", column="v", min=Decimal(0), max=Decimal("0.3")), "VARCHAR", scan
    )
    [(expression, name)] = scan.derived.items()
    assert expression.startswith("CASE WHEN (")  # NULL unless the DOUBLE is next to a bound
    assert "regexp_extract" not in test.fails
    assert name in test.fails


@pytest.mark.parametrize(
    ("bound", "text", "fails"),
    [
        ("max: 1.0e+60", "1" + "0" * 60, False),
        ("max: 1.0e+60", "1" + "0" * 59 + "1", True),
        ("min: 1.0e-60", "0." + "0" * 59 + "1", False),
        ("min: 1.0e-60", "0." + "0" * 60 + "9", True),
    ],
)
def test_bounds_too_long_to_write_plainly_are_still_exact(
    tmp_path: Path, bound: str, text: str, fails: bool
) -> None:
    result = check(tmp_path, f"rule: range, column: v, {bound}", column(text))
    assert result.failing_rows == int(fails)


@pytest.mark.parametrize(
    ("bound", "texts", "expected"),
    [
        ("max: 100", ["100", "100.0", "1e2", "100.00000000000001"], ["100.00000000000001"]),
        ("min: 100", ["100", "100.0", "99.999999999999999"], ["99.999999999999999"]),
        ("max: 0.10", ["0.10", "0.1", "0.100000000000000001"], ["0.100000000000000001"]),
    ],
)
def test_values_written_like_the_bound_and_others_next_to_it(
    tmp_path: Path, bound: str, texts: list[str], expected: list[str]
) -> None:
    result = check(tmp_path, f"rule: range, column: v, {bound}", column(*texts))
    assert failing(result) == expected


# --- whole numbers of any size in the rules ---------------------------------------------------

BIG = [
    10**39 - 1,  # 39 nines
    10**40 - 1,  # 40 nines
    2**127 - 1,  # the largest HUGEINT
    2**127,
    -(2**127) - 1,
    10**59 + 7,  # 60 digits
    -(10**60 - 1),  # 60 digits, negative
]


def results_for(tmp_path: Path, data_name: str, written: str) -> list[tuple[int | None, list[int]]]:
    """Failing rows of range max, range min and in_set with the number written as given."""
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        f"version: 1\nsource: {{path: {data_name}}}\nchecks:\n"
        f"  - {{rule: range, column: v, max: {written}}}\n"
        f"  - {{rule: range, column: v, min: {written}}}\n"
        f"  - {{rule: in_set, column: v, values: [{written}]}}\n",
        encoding="utf-8",
    )
    report = run_checks(load_rules(rules), rules, max_samples=100)
    return [(r.failing_rows, [s.row for s in r.samples or ()]) for r in report.results]


def expected(
    values: list[object], fits_max: object, fits_min: object, equal: object
) -> list[tuple[int | None, list[int]]]:
    found: list[tuple[int | None, list[int]]] = []
    for passes in (fits_max, fits_min, equal):
        assert callable(passes)
        rows = [row for row, value in enumerate(values, 1) if not passes(value)]
        found.append((len(rows), rows))
    return found


@pytest.mark.parametrize("number", BIG, ids=lambda n: f"{'-' if n < 0 else ''}{len(str(abs(n)))}d")
def test_a_huge_whole_number_on_text_is_exact_and_the_same_as_with_point_zero(
    tmp_path: Path, number: int
) -> None:
    texts = [str(number - 1), str(number), str(number + 1), f"{number}.0", f"{number}e0",
             f"{number}.5", "1", "-1"]  # fmt: skip
    (tmp_path / "data.csv").write_bytes(column(*texts))
    parquet(tmp_path, "VARCHAR", *(f"'{text}'" for text in texts))
    want = expected(
        [Decimal(text) for text in texts],
        lambda v: v <= number,
        lambda v: v >= number,
        lambda v: v == number,
    )
    for name in ("data.csv", "data.parquet"):
        assert results_for(tmp_path, name, str(number)) == want
        assert results_for(tmp_path, name, f"{number}.0") == want


@pytest.mark.parametrize("number", BIG, ids=lambda n: f"{'-' if n < 0 else ''}{len(str(abs(n)))}d")
@pytest.mark.parametrize(
    ("sql_type", "values"),
    [
        ("DOUBLE", ["1e39", "-1e39", "1.7e38", "1e60", "-1e60", "1"]),
        ("DECIMAL(38,0)", [str(10**38 - 1), str(-(10**38) + 1), "0"]),
        ("BIGINT", [str(2**63 - 1), str(-(2**63)), "0"]),
    ],
)
def test_a_huge_whole_number_on_parquet_numbers_is_the_same_as_with_point_zero(
    tmp_path: Path, number: int, sql_type: str, values: list[str]
) -> None:
    parquet(tmp_path, sql_type, *values)
    if sql_type == "DOUBLE":  # compared in the column's own type
        bound = float(number)
        want = expected([float(v) for v in values], lambda v: v <= bound,
                        lambda v: v >= bound, lambda v: v == bound)  # fmt: skip
    else:  # exact: the number is outside the type, so it passes or fails every value
        want = expected([int(v) for v in values], lambda v: v <= number,
                        lambda v: v >= number, lambda v: v == number)  # fmt: skip
    assert results_for(tmp_path, "data.parquet", str(number)) == want
    assert results_for(tmp_path, "data.parquet", f"{number}.0") == want
