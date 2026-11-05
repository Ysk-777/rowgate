"""The rules file format (version 1) as pydantic models.

Every model rejects unknown fields and uses strict types: ``"5"`` is not
accepted where a number is expected, and ``true`` is not accepted as ``1``.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import PurePath
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    ValidationInfo,
    field_validator,
    model_validator,
)

from rowgate.database import regex_error

Severity = Literal["error", "warn"]
FileFormat = Literal["csv", "parquet"]
ValueKind = Literal["text", "number", "boolean"]
BoundKind = Literal["number", "date"]

InSetValue = str | bool | int | float
RangeBound = int | float | date

FORMAT_BY_EXTENSION: dict[str, FileFormat] = {".csv": "csv", ".parquet": "parquet"}

_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MAX_SHOWN = 40


def describe_value(value: object) -> str:
    """Name a YAML value and its type for error messages, e.g. ``string 'abc'``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"integer {value}"
    if isinstance(value, float):
        return f"number {value}"
    if isinstance(value, str):
        shown = value if len(value) <= _MAX_SHOWN else value[: _MAX_SHOWN - 3] + "..."
        return f"string {shown!r}"
    if isinstance(value, datetime):
        return f"timestamp {value.isoformat(sep=' ')}"
    if isinstance(value, date):
        return f"date {value.isoformat()}"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a mapping"
    return f"a value of type {type(value).__name__}"


def value_kind(value: InSetValue) -> ValueKind:
    """The kind of an ``in_set`` value: text, number or boolean."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "text"
    return "number"


def bound_kind(value: RangeBound) -> BoundKind:
    """The kind of a ``range`` bound: number or date."""
    return "date" if isinstance(value, date) else "number"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Source(_StrictModel):
    """The data file to check."""

    path: Annotated[str, Field(min_length=1)]
    format: Annotated[FileFormat | None, Field(validate_default=True)] = None
    delimiter: str | None = None

    @field_validator("path")
    @classmethod
    def _local_file_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        if "\x00" in value:
            raise ValueError("must not contain a NUL character")
        if _URL.match(value):
            raise ValueError("URLs are not supported; RowGate reads local files only")
        return value

    @field_validator("format")
    @classmethod
    def _format_from_extension(
        cls, value: FileFormat | None, info: ValidationInfo
    ) -> FileFormat | None:
        if value is not None:
            return value
        path = info.data.get("path")
        if path is None:  # the path itself is invalid and reported separately
            return None
        suffix = PurePath(path).suffix.lower()
        guessed = FORMAT_BY_EXTENSION.get(suffix)
        if guessed is None:
            what = f"the extension {suffix!r}" if suffix else "a path without an extension"
            raise ValueError(
                f"cannot tell the file format from {what}; set format: csv or format: parquet"
            )
        return guessed

    @field_validator("delimiter")
    @classmethod
    def _one_character(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if len(value) != 1:
            raise ValueError(f"must be exactly one character, got {describe_value(value)}")
        if value in {'"', "\r", "\n"}:
            raise ValueError("cannot be a double quote or a line break")
        if info.data.get("format") == "parquet":
            raise ValueError("only applies to csv files")
        return value

    @property
    def file_format(self) -> FileFormat:
        """The format, given explicitly or inferred from the file extension."""
        if self.format is None:  # only possible if validation was bypassed
            raise AssertionError("format was not resolved during validation")
        return self.format


ColumnName = Annotated[str, Field(min_length=1)]


class _Check(_StrictModel):
    severity: Severity = "error"


class ColumnsExist(_Check):
    """Every listed column is present in the file."""

    rule: Literal["columns_exist"]
    columns: Annotated[list[ColumnName], Field(min_length=1)]


class RowCount(_Check):
    """The number of data rows is within bounds (inclusive)."""

    rule: Literal["row_count"]
    min: Annotated[int, Field(ge=0)] | None = None
    max: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("set at least one of min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min ({self.min}) is greater than max ({self.max})")
        return self


class NotNull(_Check):
    """The column has no NULL values."""

    rule: Literal["not_null"]
    column: ColumnName


class Unique(_Check):
    """No non-NULL value appears in the column more than once."""

    rule: Literal["unique"]
    column: ColumnName


def _in_set_value(value: object) -> InSetValue:
    if isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"must be a finite number, got {describe_value(value)}")
        return value
    if value is None:
        raise ValueError("null is not allowed; in_set always ignores NULL values")
    if isinstance(value, date):
        raise ValueError(
            f"dates are not supported here; write it in quotes, like '{value}', "
            "to compare it as text"
        )
    raise ValueError(f"expected a string, number or boolean, got {describe_value(value)}")


def _mixed_kinds_hint(kinds: set[ValueKind]) -> str:
    if kinds == {"boolean", "text"}:
        return (
            "YAML reads unquoted yes, no, true, false, on and off as booleans, "
            "so quote them to use text"
        )
    if "text" in kinds:
        return "quote numbers, like '1', to compare them as text"
    return "booleans and numbers are different kinds"


class InSet(_Check):
    """Every non-NULL value is one of the listed values."""

    rule: Literal["in_set"]
    column: ColumnName
    values: Annotated[
        list[Annotated[InSetValue, PlainValidator(_in_set_value)]], Field(min_length=1)
    ]

    @field_validator("values")
    @classmethod
    def _one_kind(cls, values: list[InSetValue]) -> list[InSetValue]:
        kinds = {value_kind(value) for value in values}
        if len(kinds) > 1:
            names = " and ".join(sorted(kinds))
            raise ValueError(
                f"all values must be the same kind, but this list mixes {names} values; "
                f"{_mixed_kinds_hint(kinds)}"
            )
        return values

    @property
    def kind(self) -> ValueKind:
        """Whether the values are text, numbers or booleans."""
        return value_kind(self.values[0])


def _range_bound(value: object) -> RangeBound:
    if isinstance(value, bool):
        raise ValueError(f"expected a number or a date, got {describe_value(value)}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"must be a finite number, got {describe_value(value)}")
        return value
    if isinstance(value, datetime):
        raise ValueError(
            f"timestamps are not supported; use a date such as {value.date().isoformat()}"
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{value!r} is not a valid calendar date") from None
    raise ValueError(f"expected a number or a date (YYYY-MM-DD), got {describe_value(value)}")


def _greater(low: RangeBound, high: RangeBound) -> bool:
    if isinstance(low, date) and isinstance(high, date):
        return low > high
    if not isinstance(low, date) and not isinstance(high, date):
        return low > high
    raise TypeError("bounds of different kinds cannot be compared")


Bound = Annotated[RangeBound, PlainValidator(_range_bound)]


class Range(_Check):
    """Every non-NULL value is between min and max (inclusive)."""

    rule: Literal["range"]
    column: ColumnName
    min: Bound | None = None
    max: Bound | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("set at least one of min or max")
        if self.min is not None and self.max is not None:
            if bound_kind(self.min) != bound_kind(self.max):
                raise ValueError("min and max must both be numbers or both be dates")
            if _greater(self.min, self.max):
                raise ValueError(f"min ({self.min}) is greater than max ({self.max})")
        return self

    @property
    def kind(self) -> BoundKind:
        """Whether the bounds are numbers or dates."""
        bound = self.min if self.min is not None else self.max
        if bound is None:  # only possible if validation was bypassed
            raise AssertionError("range has no bounds")
        return bound_kind(bound)


class Regex(_Check):
    """Every non-NULL value fully matches the pattern."""

    rule: Literal["regex"]
    column: ColumnName
    pattern: Annotated[str, Field(min_length=1)]

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        problem = regex_error(value)
        if problem is not None:
            raise ValueError(f"invalid regular expression: {problem}")
        return value


Check = Annotated[
    ColumnsExist | RowCount | NotNull | Unique | InSet | Range | Regex,
    Field(discriminator="rule"),
]

RULE_MODELS: dict[str, type[_Check]] = {
    "columns_exist": ColumnsExist,
    "row_count": RowCount,
    "not_null": NotNull,
    "unique": Unique,
    "in_set": InSet,
    "range": Range,
    "regex": Regex,
}


class RulesFile(_StrictModel):
    """A whole rules file."""

    version: Literal[1]
    source: Source
    checks: Annotated[list[Check], Field(min_length=1)]

    @field_validator("version", mode="before")
    @classmethod
    def _exactly_the_integer_one(cls, value: object) -> object:
        # Literal[1] alone accepts true and 1.0, because True == 1.0 == 1 in Python.
        if isinstance(value, bool | float):
            raise ValueError(f"unsupported version; expected 1, got {describe_value(value)}")
        return value
