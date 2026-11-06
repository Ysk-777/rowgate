"""Opening the data file: the M2 source-loading requirements, one section each."""

from __future__ import annotations

import bz2
import csv
import gzip
import io
import lzma
import os
import re
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import duckdb
import pytest

from rowgate import database
from rowgate import source as source_module
from rowgate.errors import DataError
from rowgate.rules import Source
from rowgate.source import (
    Column,
    CsvLayout,
    Dataset,
    _describe,
    _find_invalid_utf8,
    _metadata_error,
    check_names,
    csv_read_error,
    open_dataset,
    query_error,
)
from rowgate.sql import quote_identifier, text_as_date, text_as_number


def source(path: str, **fields: str) -> Source:
    return Source.model_validate({"path": path, **fields})


def open_file(tmp_path: Path, name: str, content: bytes, **fields: str) -> Dataset:
    (tmp_path / name).write_bytes(content)
    return open_dataset(tmp_path / "rules.yaml", source(name, **fields))


def open_csv(tmp_path: Path, content: bytes, **fields: str) -> Dataset:
    return open_file(tmp_path, "data.csv", content, **fields)


def data_error(tmp_path: Path, content: bytes, name: str = "data.csv", **fields: str) -> str:
    with pytest.raises(DataError) as excinfo:
        open_file(tmp_path, name, content, **fields)
    assert excinfo.value.details == []
    return excinfo.value.message


def rows(dataset: Dataset) -> list[tuple[object, ...]]:
    columns = ", ".join(quote_identifier(name) for name in dataset.column_names)
    sql = f"SELECT list(struct_pack({columns})) FROM {dataset.relation}"  # noqa: S608
    packed = dataset.scalar(sql)
    assert isinstance(packed, list)
    return [tuple(row.values()) for row in packed]


def write_parquet(path: Path, select: str) -> None:
    # Test data is written with a separate, unrestricted connection.
    duckdb.connect(":memory:").execute(f"COPY ({select}) TO '{path}' (FORMAT parquet)")


# --- 1. glob characters ---------------------------------------------------------------


def test_glob_characters_in_the_folder_path_are_refused(tmp_path: Path) -> None:
    folder = tmp_path / "exports[old]"
    folder.mkdir()
    (folder / "data.csv").write_bytes(b"id\n1\n")
    with pytest.raises(DataError) as excinfo:
        open_dataset(folder / "rules.yaml", source("data.csv"))
    message = excinfo.value.message
    assert message.startswith("the full path of 'data.csv' is ")
    assert message.endswith(
        "which contains '['; DuckDB would read it as a file pattern, so move the file to a "
        "folder without these characters"
    )


# --- 2. fixed CSV options, no guessing --------------------------------------------------


def test_ragged_rows_are_an_error_not_a_reshaped_file(tmp_path: Path) -> None:
    # With DuckDB's detection on, this file became one column named "a,b".
    message = data_error(tmp_path, b"a,b\n1,2\n3\n4,5,6\n7,8\n")
    assert message == "line 3 of 'data.csv' has 1 field, but the header has 2 fields"


def test_too_many_fields_is_an_error(tmp_path: Path) -> None:
    message = data_error(tmp_path, b"a,b\n1,2\n4,5,6\n")
    assert message == "line 3 of 'data.csv' has 3 fields, but the header has 2 fields"


def test_the_delimiter_is_never_detected(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"id;name\n1;a\n") as dataset:
        assert dataset.column_names == ["id;name"]
        assert rows(dataset) == [("1;a",)]


@pytest.mark.parametrize(
    ("delimiter", "sep"), [(";", b";"), ("\t", b"\t"), ("|", b"|"), ("§", "§".encode())]
)
def test_the_configured_delimiter_is_used(tmp_path: Path, delimiter: str, sep: bytes) -> None:
    content = sep.join([b"id", b"name"]) + b"\n" + sep.join([b"1", b"a,b"]) + b"\n"
    with open_csv(tmp_path, content, delimiter=delimiter) as dataset:
        assert dataset.column_names == ["id", "name"]
        assert rows(dataset) == [("1", "a,b")]


def test_quoted_fields_follow_rfc_4180(tmp_path: Path) -> None:
    content = b'id,"na,me","say ""hi"""\n1,"x,y","multi\nline"\n2,plain,"q""uote"\n'
    with open_csv(tmp_path, content) as dataset:
        assert dataset.column_names == ["id", "na,me", 'say "hi"']
        assert rows(dataset) == [("1", "x,y", "multi\nline"), ("2", "plain", 'q"uote')]
        assert dataset.row_count == 2


def test_a_quote_inside_an_unquoted_field_is_kept(tmp_path: Path) -> None:
    with open_csv(tmp_path, b'id,name\n1,a"b\n') as dataset:
        assert rows(dataset) == [("1", 'a"b')]


def test_an_unterminated_quote_is_an_error_at_the_line_it_starts(tmp_path: Path) -> None:
    message = data_error(tmp_path, b'id,name\n1,a\n2,"unterminated\n3,c\n')
    assert message.startswith("line 3 of 'data.csv' is not valid CSV (")
    assert message.endswith(QUOTE_HINT)


def test_a_multi_line_header_is_one_record(tmp_path: Path) -> None:
    with open_csv(tmp_path, b'id,"two\nlines"\n1,a\n') as dataset:
        assert dataset.column_names == ["id", "two\nlines"]
        assert dataset.row_count == 1


# --- 3. values that do not convert never break the read ----------------------------------


@pytest.fixture
def late_bad_value(tmp_path: Path) -> Path:
    """50,001 data rows; the last amount is not a number."""
    lines = ["id,amount", *(f"{i},{i % 100}" for i in range(1, 50_001)), "50001,not-a-number"]
    path = tmp_path / "data.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_bad_value_on_the_last_row_does_not_break_reading(
    tmp_path: Path, late_bad_value: Path
) -> None:
    with open_dataset(tmp_path / "rules.yaml", source("data.csv")) as dataset:
        assert dataset.row_count == 50_001
        assert [(c.name, c.type) for c in dataset.columns] == [
            ("id", "VARCHAR"),
            ("amount", "VARCHAR"),
        ]
        last = dataset.scalar(f"SELECT max_by(amount, id::INTEGER) FROM {dataset.relation}")  # noqa: S608
        assert last == "not-a-number"


def test_values_that_do_not_convert_are_counted_not_raised(
    tmp_path: Path, late_bad_value: Path
) -> None:
    with open_dataset(tmp_path / "rules.yaml", source("data.csv")) as dataset:
        number = text_as_number("amount")
        sql = (
            f"SELECT count(*) FILTER (WHERE amount IS NOT NULL AND {number} IS NULL), "  # noqa: S608
            f"sum({number}) FROM {dataset.relation}"
        )
        assert dataset.scalar(sql) == 1


# --- 4. what values mean -----------------------------------------------------------------


def test_empty_fields_are_null_and_quoted_empty_fields_are_empty_text(tmp_path: Path) -> None:
    content = b'id,code,note\n1,,x\n2,"",y\n3,007,"z"\n4,NULL,"NULL"\n'
    with open_csv(tmp_path, content) as dataset:
        assert rows(dataset) == [
            ("1", None, "x"),  # empty field: NULL
            ("2", "", "y"),  # quoted empty field: empty text
            ("3", "007", "z"),  # leading zeros are kept
            ("4", "NULL", "NULL"),  # the word NULL is text
        ]


def test_csv_values_stay_text_exactly_as_written(tmp_path: Path) -> None:
    content = b"d,n,b\n01/02/2024,1.50,TRUE\n2024-01-31, 7 ,yes\n"
    with open_csv(tmp_path, content) as dataset:
        assert {column.type for column in dataset.columns} == {"VARCHAR"}
        assert rows(dataset) == [("01/02/2024", "1.50", "TRUE"), ("2024-01-31", " 7 ", "yes")]


@pytest.mark.parametrize(
    ("text", "date"),
    [
        ("2024-01-31", "2024-01-31"),
        ("2024-02-29", "2024-02-29"),
        ("01/02/2024", None),  # never guessed
        ("02.01.2024", None),
        ("2024-1-31", None),  # DuckDB's cast would accept this; RowGate does not
        ("2024-02-30", None),  # not a real date
        (" 2024-01-31", None),
        ("2024-01-31 10:00:00", None),
        ("20240131", None),
    ],
)
def test_only_iso_dates_are_dates(text: str, date: str | None) -> None:
    connection = duckdb.connect(":memory:")
    row = connection.execute(
        f"SELECT CAST({text_as_date('$v')} AS VARCHAR)", {"v": text}
    ).fetchone()
    assert row == (date,)


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("12", 12.0),
        ("-3.5", -3.5),
        ("+0.25", 0.25),
        (".5", 0.5),
        ("1e3", 1000.0),
        ("2.5E-1", 0.25),
        ("007", 7.0),
        (" 12", None),  # DuckDB's cast would accept this; RowGate does not
        ("1_000", None),
        ("1,000", None),
        ("0x10", None),
        ("NaN", None),
        ("inf", None),
        ("", None),
        ("12abc", None),
    ],
)
def test_only_plain_numbers_are_numbers(text: str, number: float | None) -> None:
    connection = duckdb.connect(":memory:")
    row = connection.execute(f"SELECT {text_as_number('$v')}", {"v": text}).fetchone()
    assert row == (number,)


def test_parquet_keeps_its_own_types(tmp_path: Path) -> None:
    write_parquet(
        tmp_path / "data.parquet",
        "SELECT 7::INTEGER AS qty, 1.5::DOUBLE AS price, DATE '2024-01-31' AS day, "
        "true AS paid, '007' AS code, NULL::VARCHAR AS note",
    )
    with open_dataset(tmp_path / "rules.yaml", source("data.parquet")) as dataset:
        assert [(c.name, c.type) for c in dataset.columns] == [
            ("qty", "INTEGER"),
            ("price", "DOUBLE"),
            ("day", "DATE"),
            ("paid", "BOOLEAN"),
            ("code", "VARCHAR"),
            ("note", "VARCHAR"),
        ]
        assert dataset.row_count == 1
        assert dataset.file_format == "parquet"


# --- 5. rows are never lost silently -------------------------------------------------------


def test_data_rows_starting_with_hash_are_counted(tmp_path: Path) -> None:
    # DuckDB's detection treated '#' lines as comments and dropped them, even
    # with skip=0. RowGate turns detection off and sets no comment character.
    content = b"id,name\n1,a\n#2,starts with a hash\n3,c\n# 4,looks like a comment\n"
    with open_csv(tmp_path, content) as dataset:
        assert dataset.row_count == 4
        assert [row[0] for row in rows(dataset)] == ["1", "#2", "3", "# 4"]


def test_comment_lines_above_the_header_are_an_error(tmp_path: Path) -> None:
    message = data_error(tmp_path, b"# exported by a tool\n# for team X\nid,name\n1,a\n")
    assert message == (
        "line 3 of 'data.csv' has 2 fields, but the header has 1 field; the first line "
        "starts with '#', and RowGate does not skip comment lines"
    )


def test_blank_lines_are_an_error(tmp_path: Path) -> None:
    # DuckDB skips blank lines in a file with several columns, even with
    # detection off, so RowGate refuses them instead of losing count.
    message = data_error(tmp_path, b"id,name\n1,a\n\n2,b\n")
    assert message == (
        "line 3 of 'data.csv' is blank; RowGate ignores blank lines only at the end of a file, "
        "so remove it"
    )


def test_a_blank_line_in_a_one_column_file_is_an_empty_value(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"v\n1\n\n2\n") as dataset:
        assert dataset.row_count == 3
        assert rows(dataset) == [("1",), (None,), ("2",)]


def test_a_blank_first_line_is_an_error(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"\nid,name\n1,a\n") == (
        "line 1 of 'data.csv' is blank; the first line must be the header"
    )


def test_row_count_matches_the_data_lines(tmp_path: Path) -> None:
    body = b"".join(b"%d,row %d\n" % (i, i) for i in range(1, 1001))
    with open_csv(tmp_path, b"id,name\n" + body) as dataset:
        assert dataset.row_count == 1000


def test_a_disagreement_with_duckdb_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def miscount(path: Path, delimiter: str, shown: str) -> CsvLayout:
        return CsvLayout(["id"], rows=3, trailing_blank_lines=0)

    monkeypatch.setattr(source_module, "scan_csv", miscount)
    assert data_error(tmp_path, b"id\n1\n2\n") == (
        "'data.csv' has 3 data rows, but DuckDB read 2; RowGate stops rather than check a "
        "partial file"
    )


# --- 6. column names --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "listed"),
    [
        (b"a,a,A", "'a' (column 1), 'a' (column 2), 'A' (column 3)"),
        (b"id,name,ID", "'id' (column 1), 'ID' (column 3)"),
        (b"x,Name,y,NAME,nAmE", "'Name' (column 2), 'NAME' (column 4), 'nAmE' (column 5)"),
    ],
)
def test_duplicate_column_names_are_an_error(tmp_path: Path, header: bytes, listed: str) -> None:
    message = data_error(tmp_path, header + b"\n" + b",".join([b"1"] * (header.count(b",") + 1)))
    assert message == (
        f"'data.csv' has duplicate column names (compared ignoring case): {listed}; give every "
        "column a different name"
    )


def test_names_differing_in_non_ascii_case_are_different_columns(tmp_path: Path) -> None:
    # DuckDB itself treats é and É, or ß and SS, as different names.
    with open_csv(tmp_path, "é,É,ß,SS\n1,2,3,4\n".encode()) as dataset:
        assert dataset.column_names == ["é", "É", "ß", "SS"]


def test_pandas_default_output_gets_a_name_for_its_index_column(tmp_path: Path) -> None:
    # pandas' to_csv writes the index with an empty header name.
    with open_csv(tmp_path, b",order_id,status\n0,1001,paid\n1,1002,new\n") as dataset:
        assert dataset.column_names == ["column_1", "order_id", "status"]
        assert rows(dataset) == [("0", "1001", "paid"), ("1", "1002", "new")]


def test_every_empty_header_name_is_named_by_its_position(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"a,,b,\n1,2,3,4\n") as dataset:
        assert dataset.column_names == ["a", "column_2", "b", "column_4"]


@pytest.mark.parametrize(
    ("header", "listed"),
    [
        (b",column_1", "'column_1' (column 1, unnamed in the header), 'column_1' (column 2)"),
        (b"COLUMN_2,", "'COLUMN_2' (column 1), 'column_2' (column 2, unnamed in the header)"),
    ],
)
def test_a_positional_name_that_clashes_is_an_error(
    tmp_path: Path, header: bytes, listed: str
) -> None:
    assert data_error(tmp_path, header + b"\n1,2\n") == (
        f"'data.csv' has duplicate column names (compared ignoring case): {listed}; give every "
        "column a different name"
    )


def test_an_empty_parquet_column_name_is_an_error() -> None:
    # Parquet names come from the file and are not renamed by RowGate.
    with pytest.raises(DataError) as excinfo:
        check_names(["", "a"], "data.parquet")
    assert excinfo.value.message == (
        "column 1 of 'data.parquet' has an empty name; give every column a name"
    )


UNUSUAL_NAMES = [
    "order id",
    'say "hi"',
    "it's",
    "select",
    "x'); DROP TABLE t; --",
    "日付",
    "$1",
    "?",
]


def test_unusual_column_names_are_kept_exactly(tmp_path: Path) -> None:
    header = ",".join('"' + name.replace('"', '""') + '"' for name in UNUSUAL_NAMES)
    with open_csv(
        tmp_path, (header + "\n" + ",".join("v" for _ in UNUSUAL_NAMES) + "\n").encode()
    ) as dataset:
        assert dataset.column_names == UNUSUAL_NAMES
        for name in UNUSUAL_NAMES:
            column = quote_identifier(name)
            assert dataset.scalar(f"SELECT count({column}) FROM {dataset.relation}") == 1  # noqa: S608


def parquet_with_names(tmp_path: Path, first: bytes, second: bytes) -> Path:
    # DuckDB will not write two columns with clashing names, so write
    # distinct names of the same length and patch the second one in place.
    path = tmp_path / "data.parquet"
    write_parquet(path, "SELECT 1 AS aaaaaaa, 2 AS bbbbbbb, [1, 2] AS nested")
    raw = path.read_bytes().replace(b"aaaaaaa", first).replace(b"bbbbbbb", second)
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize(
    ("second", "shown"), [(b"colname", "'colname'"), (b"COLNAME", "'COLNAME'")]
)
def test_duplicate_parquet_column_names_are_an_error(
    tmp_path: Path, second: bytes, shown: str
) -> None:
    parquet_with_names(tmp_path, b"colname", second)
    with pytest.raises(DataError) as excinfo:
        open_dataset(tmp_path / "rules.yaml", source("data.parquet"))
    assert excinfo.value.message == (
        "'data.parquet' has duplicate column names (compared ignoring case): 'colname' "
        f"(column 1), {shown} (column 2); give every column a different name"
    )


def test_nested_field_names_are_not_top_level_duplicates(tmp_path: Path) -> None:
    # parquet_schema lists every nested field, so "id" appears four times in
    # this file's schema: only the top-level columns are compared.
    write_parquet(
        tmp_path / "data.parquet",
        "SELECT 1 AS id, {'id': 2, 'name': 'b'} AS billing, {'id': 3, 'name': 's'} AS shipping, "
        "[{'id': 4}, {'id': 5}] AS items, [[1, 2], [3]] AS matrix, "
        "{'inner': {'id': 6}} AS deep",
    )
    with open_dataset(tmp_path / "rules.yaml", source("data.parquet")) as dataset:
        assert [(c.name, c.type) for c in dataset.columns] == [
            ("id", "INTEGER"),
            ("billing", 'STRUCT(id INTEGER, "name" VARCHAR)'),
            ("shipping", 'STRUCT(id INTEGER, "name" VARCHAR)'),
            ("items", "STRUCT(id INTEGER)[]"),
            ("matrix", "INTEGER[][]"),
            ("deep", 'STRUCT("inner" STRUCT(id INTEGER))'),
        ]
        assert dataset.row_count == 1


def test_nested_parquet_columns_are_one_column_each(tmp_path: Path) -> None:
    parquet_with_names(tmp_path, b"colname", b"another")
    with open_dataset(tmp_path / "rules.yaml", source("data.parquet")) as dataset:
        assert dataset.column_names == ["colname", "another", "nested"]


# --- 7. the connection can read only the data file ------------------------------------------


def test_queries_cannot_read_any_other_file(tmp_path: Path) -> None:
    (tmp_path / "other.csv").write_text("id\nnot-for-rowgate\n", encoding="utf-8")
    with open_csv(tmp_path, b"id\n1\n") as dataset:
        with pytest.raises(DataError):
            dataset.scalar("SELECT * FROM read_csv($p)", {"p": str(tmp_path / "other.csv")})
        assert dataset.row_count == 1  # the data file itself is still readable


# --- 8. encodings, line endings and nearly empty files -----------------------------------------


def test_invalid_utf8_is_an_error_with_its_position(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"id,name\n1,ok\n2,caf\xe9\n") == (
        "'data.csv' is not valid UTF-8 text: invalid byte at line 3 (byte offset 18); save "
        "the file as UTF-8"
    )


def test_invalid_utf8_in_the_header(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"id,n\xe4me\n1,a\n") == (
        "'data.csv' is not valid UTF-8 text: invalid byte at line 1 (byte offset 4); save "
        "the file as UTF-8"
    )


def test_invalid_utf8_far_into_a_large_file(tmp_path: Path) -> None:
    # The bad byte sits past the first 1 MiB chunk, right after a character
    # that is split across the chunk boundary.
    filler = "é" * 600_000  # 1.2 MB of two-byte characters
    content = b"id,name\n1," + filler.encode() + b"\n2,bad\xff\n"
    message = data_error(tmp_path, content)
    offset = len(b"id,name\n1,") + len(filler.encode()) + len(b"\n2,bad")
    assert message == (
        f"'data.csv' is not valid UTF-8 text: invalid byte at line 3 (byte offset {offset}); "
        "save the file as UTF-8"
    )


def test_a_character_cut_off_at_the_end_of_the_file(tmp_path: Path) -> None:
    content = b"id,name\n1,caf\xc3"
    assert data_error(tmp_path, content) == (
        "'data.csv' is not valid UTF-8 text: invalid byte at line 2 (byte offset 13); save "
        "the file as UTF-8"
    )


def test_finding_invalid_utf8_in_a_valid_file_reports_its_end(tmp_path: Path) -> None:
    path = tmp_path / "ok.csv"
    path.write_bytes(b"a\n1\n")
    assert _find_invalid_utf8(path) == (3, 4)


@pytest.mark.parametrize("newline", [b"\r\n", b"\r", b"\n"], ids=["crlf", "cr", "lf"])
def test_line_endings(tmp_path: Path, newline: bytes) -> None:
    content = newline.join([b"id,name", b"1,a", b'2,"x' + newline + b'y"', b""])
    with open_csv(tmp_path, content) as dataset:
        assert dataset.column_names == ["id", "name"]
        assert rows(dataset) == [("1", "a"), ("2", "x" + newline.decode() + "y")]


def test_a_byte_order_mark_is_not_part_of_the_first_name(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"\xef\xbb\xbfid,name\n1,a\n") as dataset:
        assert dataset.column_names == ["id", "name"]
        assert dataset.row_count == 1


@pytest.mark.parametrize("content", [b"id,name\n", b"id,name", b"id,name\r\n"])
def test_a_header_only_file_has_zero_rows(tmp_path: Path, content: bytes) -> None:
    with open_csv(tmp_path, content) as dataset:
        assert dataset.column_names == ["id", "name"]
        assert dataset.row_count == 0


def test_a_zero_byte_file_is_an_error(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"") == "'data.csv' is empty (0 bytes)"


def test_a_file_holding_only_a_byte_order_mark_is_an_error(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"\xef\xbb\xbf") == "'data.csv' has no header row"


def test_a_zero_byte_parquet_file_is_an_error(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"", name="data.parquet") == "'data.parquet' is empty (0 bytes)"


def test_a_file_that_is_not_parquet_is_an_error(tmp_path: Path) -> None:
    message = data_error(tmp_path, b"id,name\n1,a\n", name="data.parquet")
    assert message.startswith("cannot read 'data.parquet' as Parquet: ")


# --- finding the file ------------------------------------------------------------------------


def test_the_path_is_relative_to_the_rules_file_not_the_working_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "data.csv").write_bytes(b"id\n1\n")
    monkeypatch.chdir(tmp_path)
    with open_dataset(Path("rules/rules.yaml"), source("data.csv")) as dataset:
        assert dataset.row_count == 1


def test_an_absolute_path_is_used_as_is(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n2\n")
    elsewhere = tmp_path / "elsewhere" / "rules.yaml"
    with open_dataset(elsewhere, source(str(tmp_path / "data.csv"))) as dataset:
        assert dataset.row_count == 2


def test_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DataError) as excinfo:
        open_dataset(tmp_path / "rules.yaml", source("nope.csv"))
    assert excinfo.value.message == (
        f"data file 'nope.csv' not found (looked for {(tmp_path / 'nope.csv').resolve()})"
    )


def test_a_directory(tmp_path: Path) -> None:
    (tmp_path / "data.csv").mkdir()
    with pytest.raises(DataError) as excinfo:
        open_dataset(tmp_path / "rules.yaml", source("data.csv"))
    assert excinfo.value.message == "'data.csv' is a directory, not a data file"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_something_that_is_not_a_regular_file(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "data.csv")
    with pytest.raises(DataError) as excinfo:
        open_dataset(tmp_path / "rules.yaml", source("data.csv"))
    assert excinfo.value.message == "'data.csv' is not a regular file"


def test_an_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n")

    def deny(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    # Tests may run as root, where chmod cannot make a file unreadable.
    monkeypatch.setattr(Path, "open", deny)
    with pytest.raises(DataError) as excinfo:
        open_dataset(tmp_path / "rules.yaml", source("data.csv"))
    assert excinfo.value.message == "cannot read 'data.csv': Permission denied"


def test_csv_errors_from_python_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_reader(*args: object, **kwargs: object) -> object:
        raise csv.Error("something odd")

    monkeypatch.setattr(csv, "reader", broken_reader)
    assert data_error(tmp_path, b"id\n1\n").startswith(
        "line 1 of 'data.csv' is not valid CSV (something odd); check the quotes"
    )


def test_the_connection_is_closed_when_opening_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[duckdb.DuckDBPyConnection] = []
    real_connect = database.connect

    def tracked(path: Path | None = None) -> duckdb.DuckDBPyConnection:
        opened.append(real_connect(path))
        return opened[-1]

    monkeypatch.setattr(database, "connect", tracked)
    data_error(tmp_path, b"a,a\n1,2\n")  # fails after the connection is open
    [connection] = opened
    with pytest.raises(duckdb.ConnectionException):
        connection.execute("SELECT 1")  # already closed


# --- guards that should never trigger, tested so they work if they do ---------------------------


def test_column_names_that_differ_from_the_header_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def renamed(*args: object) -> list[Column]:
        return [Column("id_1", "VARCHAR")]

    monkeypatch.setattr(source_module, "_describe", renamed)
    assert data_error(tmp_path, b"id\n1\n") == (
        "DuckDB read different column names from 'data.csv' than its header"
    )


def test_a_missing_row_count_stops_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_module, "_scalar", lambda *args: None)
    assert data_error(tmp_path, b"id\n1\n") == "DuckDB returned no row count for 'data.csv'"


def test_a_schema_that_cannot_be_read_is_a_data_error(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"id\n1\n")
    params: dict[str, object] = {"rowgate_path": str(tmp_path / "data.csv")}
    with pytest.raises(DataError) as excinfo:
        # A connection that may not read the file at all.
        _describe(database.connect(), source_module.CSV_RELATION, params, "data.csv", "csv")
    assert excinfo.value.message == (
        "RowGate could not read 'data.csv': DuckDB failed without naming a line"
    )
    assert excinfo.value.duckdb_message is not None


def test_columns_are_looked_up_by_exact_name(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"id,Name\n1,a\n") as dataset:
        assert dataset.column("Name") == Column("Name", "VARCHAR")
        assert dataset.column("name") is None


# --- long fields and records -------------------------------------------------------------------


KNOWN_LIMIT = 54_321  # an unusual value no scan would ever set


@pytest.fixture
def known_csv_limit() -> Iterator[int]:
    """Set Python's csv field limit to a known value, and put the original back after."""
    original = csv.field_size_limit(KNOWN_LIMIT)
    try:
        yield KNOWN_LIMIT
    finally:
        csv.field_size_limit(original)


def test_a_200_kb_field_is_read_and_the_csv_limit_is_restored(
    tmp_path: Path, known_csv_limit: int
) -> None:
    # Python's csv module refuses fields over 128 KiB unless its limit is raised,
    # and the 200 KB field is well over the known limit too.
    with open_csv(tmp_path, b"id,blob\n1," + b"x" * 200_000 + b"\n") as dataset:
        assert dataset.row_count == 1
        assert dataset.scalar(f"SELECT length(blob) FROM {dataset.relation}") == 200_000  # noqa: S608
    assert csv.field_size_limit() == known_csv_limit


def test_the_csv_limit_is_restored_after_an_error(tmp_path: Path, known_csv_limit: int) -> None:
    data_error(tmp_path, b"id,blob\n1," + b"x" * 200_000 + b",extra\n")
    assert csv.field_size_limit() == known_csv_limit


def test_a_3_mb_field_is_read(tmp_path: Path) -> None:
    # Over DuckDB's default max_line_size of 2 MiB, which RowGate raises.
    with open_csv(tmp_path, b"id,blob\n1," + b"x" * 3_000_000 + b"\n2,ok\n") as dataset:
        assert dataset.row_count == 2
        assert dataset.scalar(f"SELECT max(length(blob)) FROM {dataset.relation}") == 3_000_000  # noqa: S608


def test_a_record_over_the_limit_is_an_error_naming_line_and_limit(tmp_path: Path) -> None:
    content = b"id,blob\n1,ok\n2," + b"x" * source_module.MAX_RECORD_BYTES + b"\n"
    assert data_error(tmp_path, content) == (
        "line 3 of 'data.csv' is longer than 16 MiB, the most RowGate reads for one CSV record"
    )


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(source_module, "MAX_RECORD_BYTES", 100)
    return 100


def test_the_limit_counts_bytes_including_the_line_break(tmp_path: Path, small_limit: int) -> None:
    # "2," + 97 x + "\n" is exactly 100 bytes: allowed. One more byte is not.
    with open_csv(tmp_path, b"id,blob\n2," + b"x" * 97 + b"\n") as dataset:
        assert dataset.row_count == 1
    assert data_error(tmp_path, b"id,blob\n2," + b"x" * 98 + b"\n") == (
        "line 2 of 'data.csv' is longer than 100 bytes, the most RowGate reads for one CSV record"
    )


def test_the_limit_counts_utf8_bytes_not_characters(tmp_path: Path, small_limit: int) -> None:
    # 40 characters, but 80 bytes of é plus "2," and "\n": 83 bytes, allowed;
    # 50 characters of é is 103 bytes, refused.
    with open_csv(tmp_path, ("id,blob\n2," + "é" * 40 + "\n").encode()) as dataset:
        assert dataset.row_count == 1
    message = data_error(tmp_path, ("id,blob\n2," + "é" * 50 + "\n").encode())
    assert message.startswith("line 2 of 'data.csv' is longer than 100 bytes")


def test_a_record_of_many_short_lines_inside_quotes_counts_as_one(
    tmp_path: Path, small_limit: int
) -> None:
    content = b'id,blob\n1,ok\n2,"' + b"line\n" * 30 + b'"\n3,ok\n'
    assert data_error(tmp_path, content) == (
        "line 3 of 'data.csv' is longer than 100 bytes, the most RowGate reads for one CSV record"
    )


def test_one_huge_line_is_refused_without_reading_it_whole(
    tmp_path: Path, small_limit: int
) -> None:
    message = data_error(tmp_path, b"id,blob\n1," + b"x" * 5_000_000 + b"\n")
    assert message.startswith("line 2 of 'data.csv' is longer than 100 bytes")


def test_the_header_is_also_limited(tmp_path: Path, small_limit: int) -> None:
    message = data_error(tmp_path, b"id," + b"n" * 200 + b"\n1,2\n")
    assert message.startswith("line 1 of 'data.csv' is longer than 100 bytes")


# --- CSV errors are RowGate's words, never DuckDB's or the row's ----------------------------------

QUOTE_HINT = (
    "check the quotes on that line: a quoted field must be closed, and only the delimiter "
    "or a line break may follow its closing quote"
)
DEBUG_HINT = (
    "run again with --debug to see DuckDB's own message, which can include data values "
    "from the file"
)


@pytest.mark.parametrize(
    "line",
    [b'2,"ok"x', b'2,"ok" ', b'2,"o"k"', b'2,"ok""'],
    ids=["text-after-quote", "space-after-quote", "quote-in-quoted-field", "unclosed-escaped"],
)
def test_stray_quotes_are_an_error_naming_the_line(tmp_path: Path, line: bytes) -> None:
    message = data_error(tmp_path, b"id,name\n1,a\n" + line + b"\n3,c\n")
    # Python's csv module explains the problem in its own words between the
    # parentheses; the line number and the hint are RowGate's.
    assert message.startswith("line 3 of 'data.csv' is not valid CSV (")
    assert message.endswith(QUOTE_HINT)
    assert "ok" not in message  # no data values


def test_a_quote_inside_an_unquoted_field_is_still_fine(tmp_path: Path) -> None:
    with open_csv(tmp_path, b'id,name\n1,a"b\n2,"x""y"\n') as dataset:
        assert rows(dataset) == [("1", 'a"b'), ("2", 'x"y')]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            b"id,name\n1,a\r\n2,b\n",
            "line 2 of 'data.csv' ends with CRLF (\\r\\n), but line 1 ends with LF (\\n)",
        ),
        (
            b"id,name\r\n1,a\r\n2,b\n3,c\r\n",
            "line 3 of 'data.csv' ends with LF (\\n), but line 1 ends with CRLF (\\r\\n)",
        ),
        (
            b"id,name\r1,a\n2,b\n",
            "line 2 of 'data.csv' ends with LF (\\n), but line 1 ends with CR (\\r)",
        ),
        (
            b"id,name\n1,a\n\r\n",
            "line 3 of 'data.csv' ends with CRLF (\\r\\n), but line 1 ends with LF (\\n)",
        ),
    ],
    ids=["lf-then-crlf", "crlf-then-lf", "cr-then-lf", "blank-line-with-crlf"],
)
def test_mixed_line_endings_are_an_error(tmp_path: Path, content: bytes, expected: str) -> None:
    # DuckDB cannot read such a file: its parser stops in an invalid state.
    assert data_error(tmp_path, content) == (
        f"{expected}; DuckDB cannot read a file that mixes line endings, so save it with one kind"
    )


def test_a_line_break_inside_quotes_is_data_not_a_line_ending(tmp_path: Path) -> None:
    with open_csv(tmp_path, b'id,name\n1,"a\r\nb"\n2,c\n') as dataset:
        assert rows(dataset) == [("1", "a\r\nb"), ("2", "c")]


def test_spaces_around_a_quoted_field_are_dropped_by_duckdb(tmp_path: Path) -> None:
    # Documented DuckDB behaviour: the quoted value is kept, the spaces are not.
    with open_csv(tmp_path, b'id,name\n1, "a"\n2, b \n') as dataset:
        assert rows(dataset) == [("1", "a"), ("2", " b ")]


def test_a_duckdb_csv_error_names_the_line_but_never_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Skip RowGate's own checks so that DuckDB meets the stray quote itself.
    monkeypatch.setattr(source_module, "scan_csv", lambda *args: CsvLayout(["id", "name"], 3, 0))
    (tmp_path / "data.csv").write_bytes(b'id,name\n1,a\n2,"private-value"x\n3,c\n')
    with pytest.raises(DataError) as excinfo:
        open_dataset(tmp_path / "rules.yaml", source("data.csv"))
    error = excinfo.value
    assert error.lines() == [
        f"error: DuckDB could not read line 3 of 'data.csv' as CSV; {DEBUG_HINT}"
    ]
    # DuckDB's own text, kept for --debug, quotes the row.
    assert error.duckdb_message is not None
    assert "private-value" in error.duckdb_message


PRIVATE_LINE = (
    "Invalid Input Error: CSV Error on Line: 7\nOriginal Line: 7,private-value\n"
    "Value with unterminated quote found.\n\nPossible fixes:\n* Disable strict mode"
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (PRIVATE_LINE, "DuckDB could not read line 7 of 'data.csv' as CSV"),
        (
            "Invalid Input Error: The CSV Parser state machine reached an invalid state.\n"
            "Possible fixes:\n* Disable the parser's strict mode",
            "RowGate could not read 'data.csv': DuckDB failed without naming a line",
        ),
        (
            "Not implemented Error: The Parallel CSV Reader currently does not support a full "
            "read on this file.",
            "RowGate could not read 'data.csv': DuckDB failed without naming a line",
        ),
    ],
    ids=["names-a-line", "no-line", "not-implemented"],
)
def test_csv_read_errors_keep_only_the_line_number(raw: str, expected: str) -> None:
    error = csv_read_error(duckdb.InvalidInputException(raw), "data.csv")
    assert error.message == expected
    assert error.lines() == [f"error: {expected}; {DEBUG_HINT}"]
    assert error.duckdb_message == raw


def test_debug_output_holds_duckdb_original_message() -> None:
    error = csv_read_error(duckdb.InvalidInputException(PRIVATE_LINE), "data.csv")
    assert error.lines(debug=True) == ["error: DuckDB could not read line 7 of 'data.csv' as CSV"]
    assert error.debug_lines() == [
        "DuckDB's original message (it can include data values from the file):",
        "  Invalid Input Error: CSV Error on Line: 7",
        "  Original Line: 7,private-value",
        "  Value with unterminated quote found.",
        "  ",
        "  Possible fixes:",
        "  * Disable strict mode",
    ]
    assert not any("private-value" in line for line in error.lines())


def test_parquet_query_errors_show_no_details() -> None:
    raw = "bad value 'private-value'"
    error = query_error(duckdb.InvalidInputException(raw), "d.parquet", "parquet")
    assert error.lines() == [
        "error: RowGate could not read 'd.parquet': DuckDB failed while reading the Parquet "
        f"data; {DEBUG_HINT}"
    ]
    assert error.duckdb_message == raw


def test_parquet_metadata_errors_show_duckdb_first_line_only(tmp_path: Path) -> None:
    (tmp_path / "data.parquet").write_bytes(b"id,name\n1,a\n")
    with pytest.raises(DataError) as excinfo:
        open_dataset(tmp_path / "rules.yaml", source("data.parquet"))
    error = excinfo.value
    assert error.message.startswith("cannot read 'data.parquet' as Parquet: ")
    assert "\n" not in error.message
    assert "Possible fixes" not in error.message
    assert error.duckdb_message is not None  # the full text, for --debug


def test_a_parquet_schema_that_cannot_be_described_is_a_data_error(tmp_path: Path) -> None:
    params: dict[str, object] = {"rowgate_path": str(tmp_path / "data.parquet")}
    with pytest.raises(DataError) as excinfo:
        _describe(
            database.connect(), source_module.PARQUET_RELATION, params, "d.parquet", "parquet"
        )
    assert excinfo.value.message.startswith("cannot read 'd.parquet' as Parquet: ")


def test_a_parquet_metadata_error_without_text() -> None:
    assert _metadata_error(duckdb.IOException(""), "d.parquet").message == (
        "cannot read 'd.parquet' as Parquet: unknown error"
    )


# --- blank lines at the end of a file ------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [b"id,name\n1,a\n2,b\n\n", b"id,name\n1,a\n2,b\n\n\n\n", b"id,name\r\n1,a\r\n2,b\r\n\r\n\r\n"],
    ids=["one", "three", "crlf"],
)
def test_trailing_blank_lines_are_ignored(tmp_path: Path, content: bytes) -> None:
    with open_csv(tmp_path, content) as dataset:
        assert dataset.row_count == 2
        assert rows(dataset) == [("1", "a"), ("2", "b")]


def test_trailing_blank_lines_are_not_nulls_in_a_one_column_file(tmp_path: Path) -> None:
    # DuckDB alone reads these blank lines as two NULL rows.
    with open_csv(tmp_path, b"v\n1\n2\n\n\n") as dataset:
        assert dataset.row_count == 2
        assert rows(dataset) == [("1",), ("2",)]


def test_a_blank_line_inside_a_one_column_file_is_still_a_null(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"v\n1\n\n2\n\n") as dataset:
        assert dataset.row_count == 3
        assert rows(dataset) == [("1",), (None,), ("2",)]


def test_trailing_blank_lines_in_a_large_one_column_file(tmp_path: Path) -> None:
    content = b"v\n" + b"".join(b"%d\n" % i for i in range(1, 100_001)) + b"\n\n\n"
    with open_csv(tmp_path, content) as dataset:
        assert dataset.row_count == 100_000
        numbered = f"SELECT row_number() OVER () AS rn, v FROM {dataset.relation}"  # noqa: S608
        summary = (
            "SELECT [count(*), count(*) FILTER (WHERE v IS NULL), max(v::INTEGER), "  # noqa: S608
            f"count(*) FILTER (WHERE v::INTEGER <> rn)] FROM ({numbered})"
        )
        # 100,000 rows in file order, no NULLs from the blank lines.
        assert dataset.scalar(summary) == [100_000, 0, 100_000, 0]


@pytest.mark.parametrize(
    "content", [b"id,name\n\n\n", b"v\n\n\n"], ids=["two-columns", "one-column"]
)
def test_a_header_followed_only_by_blank_lines_has_no_rows(tmp_path: Path, content: bytes) -> None:
    with open_csv(tmp_path, content) as dataset:
        assert dataset.row_count == 0


def test_blank_lines_followed_by_data_are_still_an_error(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"id,name\n1,a\n\n\n2,b\n\n").startswith(
        "line 3 of 'data.csv' is blank; RowGate ignores blank lines only at the end of a file"
    )


def test_a_line_of_spaces_at_the_end_is_not_blank(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"id,name\n1,a\n   \n") == (
        "line 3 of 'data.csv' has 1 field, but the header has 2 fields"
    )


@pytest.mark.parametrize(
    ("blob_bytes", "long_records"),
    [(1024 * 1024 - 3, False), (1024 * 1024 - 2, True)],
    ids=["exactly-1-mib", "one-byte-more"],
)
def test_duckdb_line_limit_is_raised_only_for_long_records(
    tmp_path: Path, blob_bytes: int, long_records: bool
) -> None:
    # A larger max_line_size slows every DuckDB scan, so it is used only for
    # files with a record over 1 MiB ("1," + blob + "\n").
    with open_csv(tmp_path, b"id,blob\n1," + b"x" * blob_bytes + b"\n") as dataset:
        assert ("max_line_size" in dataset.relation) is long_records
        assert dataset.row_count == 1


def test_a_normal_file_uses_duckdb_default_line_limit(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"id,name\n1,a\n") as dataset:
        assert dataset.relation == source_module.CSV_RELATION


def test_long_records_and_trailing_blank_lines_together(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"v\n" + b"x" * 3_000_000 + b"\nshort\n\n\n") as dataset:
        assert "max_line_size" in dataset.relation
        assert "LIMIT" in dataset.relation
        assert dataset.row_count == 2


# --- folders named like key=value ----------------------------------------------------------------

HIVE_FOLDERS = ["year=2024", "region=eu/2024/q1", "a=1/b=2"]


@pytest.mark.parametrize("folders", HIVE_FOLDERS, ids=["parent", "grandparent", "both"])
def test_key_value_folders_do_not_add_parquet_columns(tmp_path: Path, folders: str) -> None:
    folder = tmp_path / folders
    folder.mkdir(parents=True)
    write_parquet(folder / "x.parquet", "SELECT 1 AS id, 'a' AS name")
    with open_dataset(folder / "rules.yaml", source("x.parquet")) as dataset:
        assert dataset.column_names == ["id", "name"]
        assert dataset.row_count == 1


@pytest.mark.parametrize("folders", HIVE_FOLDERS, ids=["parent", "grandparent", "both"])
def test_key_value_folders_do_not_add_csv_columns(tmp_path: Path, folders: str) -> None:
    folder = tmp_path / folders
    folder.mkdir(parents=True)
    (folder / "x.csv").write_bytes(b"id,name\n1,a\n")
    with open_dataset(folder / "rules.yaml", source("x.csv")) as dataset:
        assert dataset.column_names == ["id", "name"]
        assert rows(dataset) == [("1", "a")]


# --- DuckDB's parallel CSV reader ----------------------------------------------------------------

# DuckDB's CSV scanner splits a file into chunks of 8,000,000 bytes. When a
# chunk starts inside a quoted field that holds a line break, the parallel
# reader gives up on a perfectly valid file ("does not support a full read on
# this file"). RowGate reads CSV with parallel := false.
CHUNK = 8_000_000


def quoted_line_break_across_a_chunk() -> tuple[bytes, int]:
    """A valid CSV file just over 8 MB whose chunk boundary falls inside quotes."""
    parts = [b"id,text\n"]
    size = len(parts[0])
    number = 0
    while True:
        number += 1
        record = (
            f'{number},"line one {number}\nline two with, comma and ""quotes"" '
            f'{"x" * (number % 97)}"\n'
        ).encode()
        if size + len(record) > CHUNK - 200:
            break
        parts.append(record)
        size += len(record)
    prefix = f'{number},"line one '.encode()
    # This record's first line runs across byte 8,000,000. After its quoted
    # line break comes text that looks like a record of its own, which is what
    # the reader starting the next chunk cannot make sense of.
    second_line = b'\nline two with, comma and ""quotes"" end"\n'
    parts.append(prefix + b"y" * (CHUNK - size - len(prefix) + 50) + second_line)
    for tail in range(number + 1, number + 50):
        parts.append(f'{tail},"line one {tail}\nline two with, comma {tail}"\n'.encode())
    return b"".join(parts), number + 49  # records 1 .. number - 1, the filler, 49 more


def test_a_file_the_parallel_reader_gives_up_on_is_read(tmp_path: Path) -> None:
    content, expected_rows = quoted_line_break_across_a_chunk()
    path = tmp_path / "data.csv"
    path.write_bytes(content)
    assert len(content) > CHUNK

    # The file really does defeat DuckDB's parallel reader, at any thread count.
    options = re.sub(r", parallel := \w+", "", source_module.CSV_RELATION)
    parallel = options[:-1] + ", parallel := true)"
    params: dict[str, object] = {
        "rowgate_path": str(path),
        "rowgate_columns": {"id": "VARCHAR", "text": "VARCHAR"},
        "rowgate_delimiter": ",",
    }
    for threads in (1, 4):
        connection = duckdb.connect(":memory:")
        connection.execute(f"SET threads = {threads}")
        with pytest.raises(duckdb.Error):
            connection.execute(f"SELECT count(*) FROM {parallel}", params).fetchone()  # noqa: S608

    with open_dataset(tmp_path / "rules.yaml", source("data.csv")) as dataset:
        assert dataset.row_count == expected_rows
        last = dataset.scalar(f"SELECT max(id::INTEGER) FROM {dataset.relation}")  # noqa: S608
        assert last == expected_rows


# --- compressed files and file names that look compressed --------------------------------------


@pytest.mark.parametrize("name", ["d.csv.gz", "d.csv.zst", "d.gz", "d.csv.bz2"])
def test_a_compressed_looking_name_does_not_make_rowgate_decompress(
    tmp_path: Path, name: str
) -> None:
    # DuckDB alone picks the compression from the extension and fails on text.
    with open_file(tmp_path, name, b"id,name\n1,a\n", format="csv") as dataset:
        assert rows(dataset) == [("1", "a")]


def _zip(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.csv", content)
    return buffer.getvalue()


CSV_BYTES = b"id,name\n" + b"".join(b"%d,row %d\n" % (i, i) for i in range(200))


@pytest.mark.parametrize(
    ("compress", "kind"),
    [
        (gzip.compress, "gzip"),
        (bz2.compress, "bzip2"),
        (lzma.compress, "xz"),
        (_zip, "zip"),
        # No zstd or lz4 module in the standard library: their frame header
        # followed by bytes that are not UTF-8 stand in for a real file.
        (lambda content: b"\x28\xb5\x2f\xfd\xa4\xff\xfe" + content, "zstd"),
        (lambda content: b"\x04\x22\x4d\x18\x64\xff\xfe" + content, "lz4"),
    ],
)
@pytest.mark.parametrize("name", ["data.csv.gz", "data.csv"])
def test_a_compressed_file_is_an_error_saying_so(
    tmp_path: Path, compress: Callable[[bytes], bytes], kind: str, name: str
) -> None:
    message = data_error(tmp_path, compress(CSV_BYTES), name=name, format="csv")
    assert message == (
        f"{name!r} is a {kind}-compressed file; RowGate reads only uncompressed CSV files, "
        "so decompress it first"
    )


def test_text_that_starts_like_a_signature_is_still_text(tmp_path: Path) -> None:
    with open_csv(tmp_path, b"BZh,PK\n1,2\n") as dataset:
        assert dataset.column_names == ["BZh", "PK"]


def test_invalid_utf8_without_a_signature_still_says_utf8(tmp_path: Path) -> None:
    assert data_error(tmp_path, b"\xff\xfeid\n").startswith("'data.csv' is not valid UTF-8 text")
