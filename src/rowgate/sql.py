"""Small building blocks for the SQL RowGate sends to DuckDB.

User text never becomes SQL text. Column names are written as quoted
identifiers by quote_identifier, and every other value from the rules file or
the command line is bound as a query parameter. The only other text in
RowGate's SQL is fixed text written in RowGate's source code.
"""

from __future__ import annotations

# A number written the plain way: 12, -3.5, .5, 1e3, 2.5E-4. Spaces, digit
# separators (1_000, 1,000), hex, NaN and infinity do not count as numbers.
NUMBER_PATTERN = r"[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][-+]?[0-9]+)?"

# A whole number written the plain way: 12, -3, +007. Compared exactly, as HUGEINT.
INTEGER_PATTERN = r"[-+]?[0-9]+"

# Only an ISO date counts as a date: four-digit year, two-digit month and day.
# 01/02/2024 is never guessed to be a date, and 2024-02-30 is not a real one.
DATE_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"


def quote_identifier(name: str) -> str:
    """Quote a column name for DuckDB SQL, doubling any double quotes inside it."""
    return '"' + name.replace('"', '""') + '"'


def text_as_number(expression: str) -> str:
    """SQL that turns a text value into a DOUBLE, or NULL if it is not a plain number."""
    return (
        f"CASE WHEN regexp_full_match({expression}, '{NUMBER_PATTERN}') "
        f"THEN TRY_CAST({expression} AS DOUBLE) END"
    )


def text_as_integer(expression: str) -> str:
    """SQL that turns a plain whole number into a HUGEINT, or NULL.

    NULL also for whole numbers too large for a HUGEINT (over 38 digits);
    callers compare those by their digits instead (see number_parts).
    """
    return (
        f"CASE WHEN regexp_full_match({expression}, '{INTEGER_PATTERN}') "
        f"THEN TRY_CAST({expression} AS HUGEINT) END"
    )


def text_as_date(expression: str) -> str:
    """SQL that turns a text value into a DATE, or NULL if it is not a real ISO date."""
    return (
        f"CASE WHEN regexp_full_match({expression}, '{DATE_PATTERN}') "
        f"THEN TRY_CAST({expression} AS DATE) END"
    )


# The pieces of a plain number: sign, digits before and after the point, and
# the exponent. Every text that matches NUMBER_PATTERN matches this too.
_NUMBER_PIECES = r"^([-+]?)([0-9]*)\.?([0-9]*)(?:[eE]([-+]?[0-9]+))?$"


def _field(name: str, field: str) -> str:
    # struct_extract, never name.field: DuckDB reads name.field as a data
    # column called name if the file has one, and would use its values.
    return f"struct_extract({name}, '{field}')"


_PIECE_SIGN = _field("rowgate_pieces", "s")
_PIECE_WHOLE = _field("rowgate_pieces", "i")
_PIECE_FRACTION = _field("rowgate_pieces", "f")
_PIECE_POWER = _field("rowgate_pieces", "x")
_SIGN = _field("rowgate_number", "sign")
_DIGITS = _field("rowgate_number", "digits")
_EXPONENT = _field("rowgate_number", "exponent")


def _with_number_parts(expression: str, body: str) -> str:
    """SQL that evaluates ``body`` with the parts of a plain number bound to a name.

    In ``body``, the struct rowgate_number has the fields sign (-1, 0 or 1),
    digits (the significant digits, without leading or trailing zeros) and
    exponent, as in numbers.Parts; the exponent is NULL if the text's own
    exponent does not fit a 64-bit integer. ``expression`` must be text that
    matches NUMBER_PATTERN.

    Each lambda binds one value to a name, so the regular expression runs
    once per value instead of once per mention. Written out in full, the
    comparison mentions each part several times, and inside a CASE branch
    DuckDB took about 8 microseconds a row for it; this takes under one.
    Lambda parameters take precedence over data columns of the same name.
    """
    pieces = f"regexp_extract({expression}, '{_NUMBER_PIECES}', ['s', 'i', 'f', 'x'])"
    digits = "rtrim(rowgate_core, '0')"
    parts = (
        f"struct_pack(sign := CASE WHEN {digits} = '' THEN 0 "
        f"WHEN {_PIECE_SIGN} = '-' THEN -1 ELSE 1 END, "
        f"digits := {digits}, "
        f"exponent := CAST(length({_PIECE_WHOLE}) AS HUGEINT) "
        f"- (length({_PIECE_WHOLE} || {_PIECE_FRACTION}) - length(rowgate_core)) "
        f"+ CASE WHEN {_PIECE_POWER} = '' THEN 0 ELSE TRY_CAST({_PIECE_POWER} AS BIGINT) END)"
    )
    return (
        f"list_transform([{pieces}], lambda rowgate_pieces: "
        f"list_transform([ltrim({_PIECE_WHOLE} || {_PIECE_FRACTION}, '0')], "
        "lambda rowgate_core: "
        f"list_transform([{parts}], lambda rowgate_number: {body})[1])[1])[1]"
    )


def number_parts(expression: str) -> str:
    """SQL for the parts of a plain number as a struct of sign, digits and exponent.

    See _with_number_parts; compare_parts compares such a struct with a bound.
    """
    return _with_number_parts(expression, "rowgate_number")


def compare_parts(parts: str, sign: str, digits: str, exponent: str) -> str:
    """SQL that is -1, 0 or 1 as a number is below, equal to or above a bound.

    ``parts`` names a struct made by number_parts. The bound is given by the
    names of three parameters holding its numbers.Parts. The comparison
    works on the digits, so it is exact for any number of digits. It is NULL
    if the number's exponent is NULL.
    """
    own_sign, own_digits, own_exponent = (
        _field(parts, "sign"),
        _field(parts, "digits"),
        _field(parts, "exponent"),
    )
    magnitude = (
        f"CASE WHEN {own_exponent} IS NULL THEN NULL "
        f"WHEN {own_exponent} < ${exponent} THEN -1 WHEN {own_exponent} > ${exponent} THEN 1 "
        f"WHEN {own_digits} < ${digits} THEN -1 WHEN {own_digits} > ${digits} THEN 1 ELSE 0 END"
    )
    return (
        f"CASE WHEN {own_sign} < ${sign} THEN -1 WHEN {own_sign} > ${sign} THEN 1 "
        f"WHEN {own_sign} = 0 THEN 0 ELSE {own_sign} * ({magnitude}) END"
    )


def number_key(expression: str) -> str:
    """SQL for a text that two plain numbers share exactly when they are equal.

    The same text as numbers.Parts.key, such as "+3:12" for 120 and "0" for
    zero; NULL if the exponent is NULL.
    """
    body = (
        f"CASE WHEN {_SIGN} = 0 THEN '0' "
        f"ELSE CASE WHEN {_SIGN} < 0 THEN '-' ELSE '+' END "
        f"|| CAST({_EXPONENT} AS VARCHAR) || ':' || {_DIGITS} END"
    )
    return _with_number_parts(expression, body)
