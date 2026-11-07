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
