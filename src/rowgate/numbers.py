"""Exact numbers from the rules file, prepared for comparison with data.

The YAML loader reads every number with a decimal point as a Decimal, so a
bound such as ``12345678.12345678901`` keeps all its digits. The helpers here
turn such numbers into the parameters that rowgate.conditions compares data
values with, without rounding them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

Number = int | Decimal

# A result with more digits than this is never needed: DuckDB integers and
# decimals have at most 39 digits.
_MAX_DIGITS = 40


@dataclass(frozen=True)
class Parts:
    """A number written as sign x 0.DIGITS x 10^EXPONENT.

    DIGITS has no leading or trailing zeros, so every number has exactly one
    such form: 120 is +, "12", 3 and 0.0012 is +, "12", -2. Zero is 0, "", 0.
    rowgate.sql.number_parts computes the same three values for text in SQL.
    """

    sign: int  # -1, 0 or 1
    digits: str
    exponent: int

    @property
    def key(self) -> str:
        """The same text as rowgate.sql.number_key gives for an equal number."""
        if self.sign == 0:
            return "0"
        return f"{'-' if self.sign < 0 else '+'}{self.exponent}:{self.digits}"


def parts(value: Number) -> Parts:
    """The sign, significant digits and exponent of ``value``, exactly."""
    number = Decimal(value)
    if number == 0:
        return Parts(0, "", 0)
    negative, digit_tuple, exponent = number.as_tuple()
    if not isinstance(exponent, int):  # NaN and infinity are refused by the rules
        raise ValueError(f"not a finite number: {number}")
    digits = "".join(str(digit) for digit in digit_tuple).lstrip("0")
    significant = digits.rstrip("0")
    exponent += len(digits) - len(significant)
    return Parts(-1 if negative else 1, significant, len(significant) + exponent)


def neighbours(value: Number) -> tuple[float, float]:
    """The 64-bit floats just below and just above the one nearest to ``value``.

    A data value whose float is below the first is certainly below ``value``,
    and one above the second is certainly above it, even if the data's float
    were rounded one step wrong. Only values in between need an exact look.
    """
    nearest = float(value)  # correctly rounded; overflows to infinity
    return math.nextafter(nearest, -math.inf), math.nextafter(nearest, math.inf)


def scaled_floor(value: Number, scale: int) -> int | None:
    """floor(value x 10^scale) as an integer, or None if it has more than 40 digits."""
    number = Decimal(value)
    if number == 0:
        return 0
    size = number.adjusted() + scale  # the result has about size + 1 digits
    if size >= _MAX_DIGITS:
        return None
    if size < -1:  # |value x 10^scale| < 0.1
        return -1 if number < 0 else 0
    negative, digit_tuple, exponent = number.as_tuple()
    if not isinstance(exponent, int):  # NaN and infinity are refused by the rules
        raise ValueError(f"not a finite number: {number}")
    whole = int("".join(str(digit) for digit in digit_tuple))
    if negative:
        whole = -whole
    shift = exponent + scale
    factor: int = 10 ** abs(shift)
    if shift >= 0:
        return whole * factor
    return whole // factor  # floor division rounds towards minus infinity


def scaled_ceil(value: Number, scale: int) -> int | None:
    """ceil(value x 10^scale) as an integer, or None if it has more than 40 digits."""
    low = scaled_floor(Decimal(value).copy_negate(), scale)  # exact: unary minus rounds
    return None if low is None else -low


def scaled_exact(value: Number, scale: int) -> int | None:
    """value x 10^scale if that is a whole number of at most 40 digits, else None."""
    low = scaled_floor(value, scale)
    return low if low is not None and low == scaled_ceil(value, scale) else None


def at_scale(unscaled: int, scale: int) -> str:
    """The text of unscaled x 10^-scale, such as "-12.30" for (-1230, 2), exactly."""
    sign = "-" if unscaled < 0 else ""
    digits = str(abs(unscaled)).rjust(scale + 1, "0")
    if scale == 0:
        return sign + digits
    return f"{sign}{digits[:-scale]}.{digits[-scale:]}"
