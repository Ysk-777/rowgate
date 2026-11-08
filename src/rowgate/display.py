"""How RowGate shows paths, column names and values in its output.

Python's repr doubles every backslash, so a Windows path such as
C:\\data\\rules.yaml would be printed with two backslashes between each
part. RowGate shows text as written instead, and escapes only characters
that are not printable: control characters (line breaks, tabs, terminal
escape codes), invisible formatting characters and separators other than
the space. A line break in a value is shown as \\n, so it can never break
or colour the output.
"""

from __future__ import annotations

import sys
from decimal import Decimal


def escaped(text: str) -> str:
    """``text`` with each non-printable character written as an escape such as \\n or \\x1b."""
    if text.isprintable():
        return text
    # repr of one character gives Python's escape for it: \n, \t, \x1b, \u200b.
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def quoted(text: str) -> str:
    """``text`` escaped and in quotes: single quotes, or double quotes if it has a single one.

    Quotes inside the text are not escaped, and backslashes are kept as
    written: C:\\data is shown as 'C:\\data'.
    """
    quote = '"' if "'" in text and '"' not in text else "'"
    return f"{quote}{escaped(text)}{quote}"


def shown(value: object) -> str:
    """A value from a rules file or the data: text quoted as above, anything else as repr."""
    if isinstance(value, str):
        return quoted(value)
    return repr(value)


def digits(value: int | Decimal) -> int:
    """How many decimal digits a number's coefficient has, counted without decimal text.

    Decimal(int) is exact and is not limited the way str(int) is.
    """
    number = value if isinstance(value, Decimal) else Decimal(value)
    return len(number.as_tuple().digits)


def fits_text(value: int | Decimal) -> bool:
    """Whether a number may be written as decimal text.

    Python refuses str() of an int with more than sys.get_int_max_str_digits()
    digits (4,300 by default; 0 means no limit). RowGate keeps to that limit
    for every number it shows, whole or not.
    """
    limit = sys.get_int_max_str_digits()
    return not limit or digits(value) <= limit


def number(value: int | Decimal) -> str:
    """A number from the rules as decimal text, or ``<N-digit number>`` if that is too long."""
    if fits_text(value):
        return str(value)
    return f"<{digits(value)}-digit number>"


def bound(value: object) -> str:
    """A range bound for messages: a number as by number(), anything else as str()."""
    if isinstance(value, int | Decimal) and not isinstance(value, bool):
        return number(value)
    return str(value)
