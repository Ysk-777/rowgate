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
    those are compared as DOUBLE instead.
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


def number_parts(expression: str) -> tuple[str, str, str]:
    """SQL for the sign, digits and exponent of a plain number (see numbers.Parts).

    ``expression`` must be text that matches NUMBER_PATTERN. The exponent is
    NULL if the text's own exponent does not fit a 64-bit integer.
    """

    def piece(group: int) -> str:
        return f"regexp_extract({expression}, '{_NUMBER_PIECES}', {group})"

    before, after, written = piece(2), piece(3), piece(4)
    digits = f"({before} || {after})"
    core = f"ltrim({digits}, '0')"
    significant = f"rtrim({core}, '0')"
    exponent = (
        f"(CAST(length({before}) AS HUGEINT) - (length({digits}) - length({core})) + "
        f"CASE WHEN {written} = '' THEN 0 ELSE TRY_CAST({written} AS BIGINT) END)"
    )
    sign = (
        f"CASE WHEN {significant} = '' THEN 0 "
        f"WHEN starts_with({expression}, '-') THEN -1 ELSE 1 END"
    )
    return sign, significant, exponent


def number_compare(expression: str, sign: str, digits: str, exponent: str) -> str:
    """SQL that is -1, 0 or 1 as a plain number is below, equal to or above a bound.

    The bound is given by the names of three parameters holding its
    numbers.Parts. The comparison works on the digits, so it is exact for any
    number of digits. It is NULL if the exponent is NULL.
    """
    own_sign, own_digits, own_exponent = number_parts(expression)
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
    _, digits, exponent = number_parts(expression)
    sign = f"CASE WHEN starts_with({expression}, '-') THEN '-' ELSE '+' END"
    return (
        f"CASE WHEN {digits} = '' THEN '0' "
        f"ELSE {sign} || CAST({exponent} AS VARCHAR) || ':' || {digits} END"
    )
