"""Open the data file: check it, read its header and schema, and count its rows.

CSV files are read without any guessing. RowGate parses the header itself and
gives DuckDB the column names, the delimiter and the quoting rules, with
DuckDB's automatic detection switched off. Every CSV column is text, so a
value that is not a valid number or date stays a value (and can fail a check)
instead of making the whole file unreadable.

Before DuckDB reads a CSV file, RowGate streams it once in Python, in
chunks rather than all at once. This pass checks that the file is UTF-8,
that every line has as many fields as the header, and that no line is blank,
and counts the data rows. DuckDB's row count must match that count, so rows
are never dropped silently.
"""

from __future__ import annotations

import codecs
import csv
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import duckdb

from rowgate import database
from rowgate.errors import DataError
from rowgate.rules import FileFormat, Source, glob_characters

# Fixed reading options for CSV files. auto_detect is off, so DuckDB guesses
# nothing: not the delimiter, quoting, header, comment character or types.
# (With detection on, DuckDB can decide that lines starting with '#' are
# comments and drop them, even data rows, and even with skip=0.)
_CSV_OPTIONS = ", ".join(
    [
        "header := true",
        "auto_detect := false",
        "all_varchar := true",
        "columns := $rowgate_columns",
        "delim := $rowgate_delimiter",
        "quote := '\"'",
        "escape := '\"'",
        "skip := 0",
        "strict_mode := true",
        "allow_quoted_nulls := false",
        "comment := ''",
        "null_padding := false",
    ]
)
CSV_RELATION = f"read_csv($rowgate_path, {_CSV_OPTIONS})"
PARQUET_RELATION = "read_parquet($rowgate_path)"

DEFAULT_DELIMITER = ","
_CHUNK_SIZE = 1 << 20
# Python's csv module refuses fields over 128 KiB by default; let DuckDB's
# own line-size limit be the one that applies.
_FIELD_SIZE_LIMIT = min(sys.maxsize, 2**31 - 1)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


@dataclass(frozen=True)
class Column:
    """A column of the data file and its DuckDB type (always VARCHAR for CSV)."""

    name: str
    type: str


class Dataset:
    """An opened data file and a DuckDB connection that can read only that file."""

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        *,
        relation: str,
        params: dict[str, object],
        shown: str,
        file_format: FileFormat,
        columns: list[Column],
        row_count: int,
    ) -> None:
        self._connection = connection
        self.relation = relation
        self._params = params
        self.shown = shown
        self.file_format = file_format
        self.columns = columns
        self.row_count = row_count

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    def column(self, name: str) -> Column | None:
        """The column with exactly this name (names are case-sensitive)."""
        for column in self.columns:
            if column.name == name:
                return column
        return None

    def scalar(self, sql: str, params: dict[str, object] | None = None) -> object:
        """Run a query that reads ``self.relation`` and return its single value."""
        return _scalar(self._connection, sql, {**self._params, **(params or {})}, self.shown)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_dataset(rules_path: Path, source: Source) -> Dataset:
    """Check and open the data file named in the rules, relative to the rules file."""
    shown = source.path
    path = resolve_data_path(rules_path, source)
    connection = database.connect(path)
    try:
        params: dict[str, object] = {"rowgate_path": str(path)}
        scanned_rows: int | None = None
        if source.file_format == "csv":
            delimiter = source.delimiter or DEFAULT_DELIMITER
            header, scanned_rows = scan_csv(path, delimiter, shown)
            params["rowgate_columns"] = dict.fromkeys(header, "VARCHAR")
            params["rowgate_delimiter"] = delimiter
            relation = CSV_RELATION
        else:
            check_names(_parquet_names(connection, params, shown), shown)
            relation = PARQUET_RELATION

        columns = _describe(connection, relation, params, shown)
        if source.file_format == "csv" and [c.name for c in columns] != header:
            raise DataError(f"DuckDB read different column names from {shown!r} than its header")
        # relation is CSV_RELATION or PARQUET_RELATION: fixed text, values bound.
        count_sql = f"SELECT count(*) FROM {relation}"  # noqa: S608
        row_count = _scalar(connection, count_sql, params, shown)
        if not isinstance(row_count, int):
            raise DataError(f"DuckDB returned no row count for {shown!r}")
        if scanned_rows is not None and row_count != scanned_rows:
            raise DataError(
                f"{shown!r} has {scanned_rows} data rows, but DuckDB read {row_count}; "
                "RowGate stops rather than check a partial file"
            )
        return Dataset(
            connection,
            relation=relation,
            params=params,
            shown=shown,
            file_format=source.file_format,
            columns=columns,
            row_count=row_count,
        )
    except BaseException:
        connection.close()
        raise


def resolve_data_path(rules_path: Path, source: Source) -> Path:
    """The absolute path of the data file, after checking it is a readable, non-empty file."""
    shown = source.path
    given = Path(source.path)
    path = (given if given.is_absolute() else rules_path.parent / given).resolve()
    glob = glob_characters(str(path))
    if glob:
        raise DataError(
            f"the full path of {shown!r} is {path}, which contains {glob}; DuckDB would read "
            "it as a file pattern, so move the file to a folder without these characters"
        )
    if not path.exists():
        raise DataError(f"data file {shown!r} not found (looked for {path})")
    if path.is_dir():
        raise DataError(f"{shown!r} is a directory, not a data file")
    if not path.is_file():
        raise DataError(f"{shown!r} is not a regular file")
    if path.stat().st_size == 0:
        raise DataError(f"{shown!r} is empty (0 bytes)")
    return path


def scan_csv(path: Path, delimiter: str, shown: str) -> tuple[list[str], int]:
    """Stream a CSV file once and return its header and number of data rows.

    Raises DataError for anything RowGate refuses to guess about: invalid
    UTF-8, a missing or blank header, empty or duplicate column names,
    blank lines, and lines with the wrong number of fields.
    """
    csv.field_size_limit(_FIELD_SIZE_LIMIT)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return _read_records(csv.reader(handle, delimiter=delimiter, strict=False), shown)
    except UnicodeDecodeError:
        raise DataError(_invalid_utf8_message(path, shown)) from None
    except csv.Error as exc:
        raise DataError(f"{shown!r} cannot be read as CSV: {exc}") from None
    except OSError as exc:
        raise DataError(f"cannot read {shown!r}: {exc.strerror or exc}") from None


def _read_records(reader: Iterator[list[str]], shown: str) -> tuple[list[str], int]:
    header = next(reader, None)
    if header is None:
        raise DataError(f"{shown!r} has no header row")
    if not header:
        raise DataError(f"line 1 of {shown!r} is blank; the first line must be the header")
    check_names(header, shown)
    width = len(header)
    looks_commented = header[0].startswith("#")
    rows = 0
    line = _line_number(reader)
    for record in reader:
        start, line = line + 1, _line_number(reader)
        if not record and width > 1:
            raise DataError(
                f"line {start} of {shown!r} is blank; RowGate does not skip blank lines, "
                "so remove it"
            )
        if record and len(record) != width:
            hint = (
                "; the first line starts with '#', and RowGate does not skip comment lines"
                if looks_commented
                else ""
            )
            raise DataError(
                f"line {start} of {shown!r} has {_fields(len(record))}, but the header has "
                f"{_fields(width)}{hint}"
            )
        rows += 1  # in a one-column file a blank line is a row with an empty (NULL) value
    return header, rows


def _line_number(reader: Iterator[list[str]]) -> int:
    line_num = getattr(reader, "line_num", 0)
    return line_num if isinstance(line_num, int) else 0


def _fields(count: int) -> str:
    return f"{count} field" if count == 1 else f"{count} fields"


def check_names(names: list[str], shown: str) -> None:
    """Refuse empty column names and names that differ only in ASCII case."""
    for position, name in enumerate(names, start=1):
        if name == "":
            raise DataError(
                f"column {position} of {shown!r} has an empty name; give every column a name"
            )
    positions: dict[str, list[int]] = {}
    for position, name in enumerate(names, start=1):
        # DuckDB matches column names ignoring ASCII case only: a and A clash,
        # but é and É do not.
        positions.setdefault(name.translate(_ASCII_LOWER), []).append(position)
    clashes = [group for group in positions.values() if len(group) > 1]
    if clashes:
        listed = ", ".join(
            f"{names[position - 1]!r} (column {position})"
            for group in clashes
            for position in group
        )
        raise DataError(
            f"{shown!r} has duplicate column names (compared ignoring case): {listed}; "
            "give every column a different name"
        )


def _invalid_utf8_message(path: Path, shown: str) -> str:
    line, offset = _find_invalid_utf8(path)
    return (
        f"{shown!r} is not valid UTF-8 text: invalid byte at line {line} "
        f"(byte offset {offset}); save the file as UTF-8"
    )


def _find_invalid_utf8(path: Path) -> tuple[int, int]:
    """The line and byte offset of the first byte that is not valid UTF-8.

    Returns the line count and size of the file if every byte is valid.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    offset = 0
    line = 1
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            carried = len(decoder.getstate()[0])  # an unfinished character from the last chunk
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                bad = offset - carried + exc.start
                return line + chunk[: max(bad - offset, 0)].count(b"\n"), bad
            line += chunk.count(b"\n")
            offset += len(chunk)
    carried = len(decoder.getstate()[0])
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return line, offset - carried  # the file ends in the middle of a character
    return line, offset


def _parquet_names(
    connection: duckdb.DuckDBPyConnection, params: dict[str, object], shown: str
) -> list[str]:
    """The top-level column names as stored in the Parquet file.

    DuckDB renames duplicate columns when it reads them (a, a becomes a, a_1),
    so the names are taken from the file's own schema instead.
    """
    try:
        rows = connection.execute(
            "SELECT name, num_children FROM parquet_schema($rowgate_path)", params
        ).fetchall()
    except duckdb.Error as exc:
        raise DataError(f"cannot read {shown!r} as Parquet: {database.error_text(exc)}") from None
    names: list[str] = []
    index = 1  # row 0 is the schema root
    for _ in range(rows[0][1] or 0):
        names.append(rows[index][0])
        remaining = 1  # skip this column and everything nested inside it
        while remaining:
            remaining += (rows[index][1] or 0) - 1
            index += 1
    return names


def _describe(
    connection: duckdb.DuckDBPyConnection, relation: str, params: dict[str, object], shown: str
) -> list[Column]:
    try:
        # relation is CSV_RELATION or PARQUET_RELATION: fixed text, values bound.
        describe_sql = f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM {relation})"  # noqa: S608
        rows = connection.execute(describe_sql, params).fetchall()
    except duckdb.Error as exc:
        raise DataError(f"cannot read {shown!r}: {database.error_text(exc)}") from None
    return [Column(name, column_type) for name, column_type in rows]


def _scalar(
    connection: duckdb.DuckDBPyConnection, sql: str, params: dict[str, object], shown: str
) -> object:
    try:
        row = connection.execute(sql, params).fetchone()
    except duckdb.Error as exc:
        raise DataError(f"cannot read {shown!r}: {database.error_text(exc)}") from None
    return None if row is None else row[0]
