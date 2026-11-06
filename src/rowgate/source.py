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
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO

import duckdb

from rowgate import database
from rowgate.errors import DataError
from rowgate.rules import FileFormat, Source, glob_characters

# The longest CSV record RowGate reads: one line, or several lines inside
# quotes, counted in bytes with the line break.
MAX_RECORD_BYTES = 16 * 1024 * 1024
# DuckDB stops at its own max_line_size (2 MiB by default, also counted per
# record). For a file with a record over 1 MiB, it is set well above RowGate's
# limit, so RowGate's check, which names the line, always comes first. It is
# not raised for other files, because a larger setting slows every scan
# (measured: about three times slower at 32 MiB).
_DUCKDB_MAX_LINE_SIZE = 2 * MAX_RECORD_BYTES
_DUCKDB_DEFAULT_IS_ENOUGH = 1024 * 1024

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
        # A folder named like key=value (year=2024) is not a column.
        "hive_partitioning := false",
        # DuckDB's parallel CSV reader gives up on some valid files with quoted
        # line breaks ("does not support a full read on this file"), from
        # about 8 MB. Reading in one thread is slower but always works.
        "parallel := false",
        # Never decompress because of the file name: d.csv.gz is read as text.
        "compression := 'none'",
    ]
)
CSV_RELATION = f"read_csv($rowgate_path, {_CSV_OPTIONS})"
CSV_RELATION_LONG_RECORDS = (
    f"read_csv($rowgate_path, {_CSV_OPTIONS}, max_line_size := {_DUCKDB_MAX_LINE_SIZE})"
)
# hive_partitioning off: a folder named like key=value (year=2024) is not a column.
PARQUET_RELATION = "read_parquet($rowgate_path, hive_partitioning := false)"

DEFAULT_DELIMITER = ","
_CHUNK_SIZE = 1 << 20
# How common compressed files start. Checked only when a file is not valid
# UTF-8, so a text file is never mistaken for one.
_COMPRESSED_SIGNATURES = (
    (b"\x1f\x8b", "gzip"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"PK\x03\x04", "zip"),
    (b"\x04\x22\x4d\x18", "lz4"),
)
_ENDING_NAMES = {"\r\n": "CRLF (\\r\\n)", "\n": "LF (\\n)", "\r": "CR (\\r)"}
_QUOTE_HINT = (
    "check the quotes on that line: a quoted field must be closed, and only the delimiter "
    "or a line break may follow its closing quote"
)
# DuckDB's CSV errors start with "CSV Error on Line: N". Only that number is
# kept; the rest can quote the row itself, so it is never shown.
_DUCKDB_CSV_LINE = re.compile(r"CSV Error on Line: ([0-9]+)")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


@dataclass(frozen=True)
class CsvLayout:
    """What the Python pass learned about a CSV file."""

    header: list[str]  # column names, with positional names for empty ones
    rows: int  # data rows, not counting blank lines at the end of the file
    trailing_blank_lines: int
    longest_record_bytes: int = 0


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
        params = {**self._params, **(params or {})}
        return _scalar(self._connection, sql, params, self.shown, self.file_format)

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
        if source.file_format == "csv":
            delimiter = source.delimiter or DEFAULT_DELIMITER
            layout = scan_csv(path, delimiter, shown)
            params["rowgate_columns"] = dict.fromkeys(layout.header, "VARCHAR")
            params["rowgate_delimiter"] = delimiter
            long_records = layout.longest_record_bytes > _DUCKDB_DEFAULT_IS_ENOUGH
            relation = CSV_RELATION_LONG_RECORDS if long_records else CSV_RELATION
        else:
            check_names(_parquet_names(connection, params, shown), shown)
            relation = PARQUET_RELATION

        columns = _describe(connection, relation, params, shown, source.file_format)
        # relation is one of the *_RELATION constants: fixed text, values bound.
        count_sql = f"SELECT count(*) FROM {relation}"  # noqa: S608
        row_count = _scalar(connection, count_sql, params, shown, source.file_format)
        if not isinstance(row_count, int):
            raise DataError(f"DuckDB returned no row count for {shown!r}")
        if source.file_format == "csv":
            if [c.name for c in columns] != layout.header:
                raise DataError(
                    f"DuckDB read different column names from {shown!r} than its header"
                )
            relation, row_count = _without_trailing_blank_lines(
                layout, relation, row_count, params, shown
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


def _without_trailing_blank_lines(
    layout: CsvLayout, relation: str, duckdb_rows: int, params: dict[str, object], shown: str
) -> tuple[str, int]:
    """Check DuckDB's row count against RowGate's and leave out trailing blank lines.

    DuckDB skips blank lines in a file with several columns, but reads each
    one as an empty value in a one-column file. Blank lines at the end of a
    file are not data, so there RowGate reads only the rows before them.
    """
    one_column = len(layout.header) == 1
    blank_rows = layout.trailing_blank_lines if one_column else 0
    if duckdb_rows - blank_rows != layout.rows:
        raise DataError(
            f"{shown!r} has {layout.rows} data rows, but DuckDB read {duckdb_rows - blank_rows}; "
            "RowGate stops rather than check a partial file"
        )
    if not blank_rows:
        return relation, layout.rows
    params["rowgate_rows"] = layout.rows
    # LIMIT keeps file order, because connect() preserves insertion order.
    # relation is one of the *_RELATION constants: fixed text, values bound.
    return f"(SELECT * FROM {relation} LIMIT $rowgate_rows)", layout.rows  # noqa: S608


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


def scan_csv(path: Path, delimiter: str, shown: str) -> CsvLayout:
    """Stream a CSV file once and return its header and number of data rows.

    Raises DataError for anything RowGate refuses to guess about: invalid
    UTF-8, a missing or blank header, empty or duplicate column names,
    blank lines, lines with the wrong number of fields, and records longer
    than MAX_RECORD_BYTES.
    """
    limit = MAX_RECORD_BYTES
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle, _csv_field_limit(limit):
            lines = _PhysicalLines(handle, shown, limit)
            try:
                # strict: a stray quote such as 2,"ok"x is an error, as it is for DuckDB.
                reader = csv.reader(lines, delimiter=delimiter, strict=True)
                return _read_records(reader, lines, shown)
            except csv.Error as exc:
                raise DataError(
                    f"line {lines.record_start} of {shown!r} is not valid CSV ({exc}); "
                    f"{_QUOTE_HINT}"
                ) from None
    except UnicodeDecodeError:
        kind = _compression(path)
        if kind is not None:
            raise DataError(
                f"{shown!r} is a {kind}-compressed file; RowGate reads only uncompressed CSV "
                "files, so decompress it first"
            ) from None
        raise DataError(_invalid_utf8_message(path, shown)) from None
    except OSError as exc:
        raise DataError(f"cannot read {shown!r}: {exc.strerror or exc}") from None


@contextmanager
def _csv_field_limit(limit: int) -> Iterator[None]:
    """Raise Python's csv field limit (128 KiB by default) for one scan, then restore it.

    A field can be no longer than its record, so RowGate's record limit
    always applies first; the csv module's own limit never triggers.
    """
    previous = csv.field_size_limit(limit)
    try:
        yield
    finally:
        csv.field_size_limit(previous)


class _PhysicalLines:
    """Hands csv.reader one physical line at a time, measuring each record.

    ``record_start`` is the line a record starts on. A record over the limit
    is refused before csv.reader sees it, and each line is read with a size
    cap, so one enormous line is never loaded whole.
    """

    def __init__(self, handle: TextIO, shown: str, limit: int) -> None:
        self._handle = handle
        self._shown = shown
        self._limit = limit
        self.line = 0  # physical lines read so far
        self.record_start = 1
        self.ending = ""  # line break of the last line read: \r\n, \n, \r or none
        self.longest_record = 0  # bytes
        self._record_bytes = 0

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> str:
        text = self._handle.readline(self._limit + 1)  # at most limit + 1 characters
        if not text:
            raise StopIteration
        self.line += 1
        self._record_bytes += len(text) if text.isascii() else len(text.encode("utf-8"))
        if self._record_bytes > self._limit:
            raise DataError(
                f"line {self.record_start} of {self._shown!r} is longer than "
                f"{_size(self._limit)}, the most RowGate reads for one CSV record"
            )
        if text.endswith("\r\n"):
            self.ending = "\r\n"
        elif text.endswith(("\n", "\r")):
            self.ending = text[-1]
        else:
            self.ending = ""  # the last line of a file without a final line break
        return text

    def next_record(self) -> None:
        """Call after csv.reader returns a record: the next one starts on the next line."""
        self.record_start = self.line + 1
        self.longest_record = max(self.longest_record, self._record_bytes)
        self._record_bytes = 0


def _size(count: int) -> str:
    mib = 1024 * 1024
    return f"{count // mib} MiB" if count % mib == 0 else f"{count} bytes"


def _read_records(reader: Iterator[list[str]], lines: _PhysicalLines, shown: str) -> CsvLayout:
    header = next(reader, None)
    if header is None:
        raise DataError(f"{shown!r} has no header row")
    if not header:
        raise DataError(f"line 1 of {shown!r} is blank; the first line must be the header")
    first_ending = lines.ending
    lines.next_record()
    header, unnamed = positional_names(header)
    check_names(header, shown, unnamed)
    width = len(header)
    looks_commented = header[0].startswith("#")
    rows = 0
    blank_lines: list[int] = []  # blank lines not yet followed by more data
    for record in reader:
        start, end, ending = lines.record_start, lines.line, lines.ending
        lines.next_record()
        # Only the line break that ends a record matters; one inside quotes is data.
        # (A header without a line break ends the file, so first_ending is set.)
        if ending and ending != first_ending:
            raise DataError(
                f"line {end} of {shown!r} ends with {_ENDING_NAMES[ending]}, but line 1 "
                f"ends with {_ENDING_NAMES[first_ending]}; DuckDB cannot read a "
                "file that mixes line endings, so save it with one kind"
            )
        if not record:
            blank_lines.append(start)
            continue
        if blank_lines:
            if width > 1:
                raise DataError(
                    f"line {blank_lines[0]} of {shown!r} is blank; RowGate ignores blank lines "
                    "only at the end of a file, so remove it"
                )
            # In a one-column file a blank line inside the data is an empty (NULL) value.
            rows += len(blank_lines)
            blank_lines.clear()
        if len(record) != width:
            hint = (
                "; the first line starts with '#', and RowGate does not skip comment lines"
                if looks_commented
                else ""
            )
            raise DataError(
                f"line {start} of {shown!r} has {_fields(len(record))}, but the header has "
                f"{_fields(width)}{hint}"
            )
        rows += 1
    return CsvLayout(header, rows, len(blank_lines), lines.longest_record)


def _fields(count: int) -> str:
    return f"{count} field" if count == 1 else f"{count} fields"


def positional_names(header: list[str]) -> tuple[list[str], frozenset[int]]:
    """Name each empty header field after its position: column_1, column_2, ...

    pandas writes an empty first header name for the index column by default.
    Returns the names and the (1-based) positions that had no name.
    """
    unnamed = frozenset(position for position, name in enumerate(header, 1) if name == "")
    names = [name or f"column_{position}" for position, name in enumerate(header, 1)]
    return names, unnamed


def check_names(names: list[str], shown: str, unnamed: frozenset[int] = frozenset()) -> None:
    """Refuse empty column names and names that differ only in ASCII case.

    ``unnamed`` marks positions whose name RowGate made up, for the message.
    """
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
            f"{names[position - 1]!r} (column {position}"
            f"{', unnamed in the header' if position in unnamed else ''})"
            for group in clashes
            for position in group
        )
        raise DataError(
            f"{shown!r} has duplicate column names (compared ignoring case): {listed}; "
            "give every column a different name"
        )


def _compression(path: Path) -> str | None:
    """The compression format a file's first bytes show, if any."""
    with path.open("rb") as handle:
        start = handle.read(8)
    for signature, kind in _COMPRESSED_SIGNATURES:
        if start.startswith(signature):
            return kind
    return None


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
        raise _metadata_error(exc, shown) from None
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
    connection: duckdb.DuckDBPyConnection,
    relation: str,
    params: dict[str, object],
    shown: str,
    file_format: FileFormat,
) -> list[Column]:
    try:
        # relation is CSV_RELATION or PARQUET_RELATION: fixed text, values bound.
        describe_sql = f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM {relation})"  # noqa: S608
        rows = connection.execute(describe_sql, params).fetchall()
    except duckdb.Error as exc:
        if file_format == "csv":
            raise csv_read_error(exc, shown) from None
        raise _metadata_error(exc, shown) from None
    return [Column(name, column_type) for name, column_type in rows]


def _scalar(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    params: dict[str, object],
    shown: str,
    file_format: FileFormat,
) -> object:
    try:
        row = connection.execute(sql, params).fetchone()
    except duckdb.Error as exc:
        raise query_error(exc, shown, file_format) from None
    return None if row is None else row[0]


_NOT_SHOWN = "RowGate does not show DuckDB's explanation, because it can include data values"


def csv_read_error(exc: duckdb.Error, shown: str) -> DataError:
    """DuckDB failed while reading a CSV file: name the line if DuckDB gave one."""
    match = _DUCKDB_CSV_LINE.search(str(exc))
    where = f"line {match[1]} of {shown!r}" if match else repr(shown)
    return DataError(f"DuckDB could not read {where} as CSV; {_NOT_SHOWN}")


def query_error(exc: duckdb.Error, shown: str, file_format: FileFormat) -> DataError:
    """DuckDB failed while running a query over the data."""
    if file_format == "csv":
        return csv_read_error(exc, shown)
    return DataError(f"DuckDB could not read {shown!r} as Parquet; {_NOT_SHOWN}")


def _metadata_error(exc: duckdb.Error, shown: str) -> DataError:
    # Errors about a Parquet file's metadata (not a Parquet file, a broken
    # footer) hold no row values, so DuckDB's first line is shown.
    detail = database.error_text(exc).splitlines()[0] if str(exc) else "unknown error"
    return DataError(f"cannot read {shown!r} as Parquet: {detail}")
