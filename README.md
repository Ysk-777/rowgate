# RowGate

RowGate is a command-line tool that checks a local CSV or Parquet file against
rules you write in YAML. It runs the checks with DuckDB, reports failures
clearly, and exits with a code that CI can use.

RowGate is in early development. The full documentation will be written before
the first release; this page records the behaviour decided so far.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | No check with severity `error` failed (warnings are allowed). |
| 1 | At least one check with severity `error` failed. |
| 2 | Bad usage, a bad rules file, an unreadable data file, or an internal error. |

A traceback is printed only when `--debug` is passed. When DuckDB fails to
read the data, `--debug` also prints DuckDB's original message, which can
include data values from the file.

## Checking a file

```bash
rowgate check RULES.yaml [--no-samples] [--max-samples N] [--debug]
```

Each check prints one line, in the order of the rules file. A failing check
also lists **samples**: up to 5 of its failing rows (`--max-samples N`, any
N from 1 to 1000; anything else is a usage error, exit 2).

- A sample shows the data row number and the value: `row 4: 'abc'`. Rows are
  numbered from 1, the header is not counted, and a record that spans lines
  inside quotes is one row. Samples are in file order: CSV files are read in
  one thread, and Parquet rows keep their stored order.
- Text values are shown in quotes, with Python's escapes for anything that is
  not printable, so a line break, a terminal escape code or markup in the
  data is shown as `\n`, `\x1b` and so on, never sent to the terminal.
  Values of other types (numbers, dates) are shown in DuckDB's text form,
  without quotes. A missing value is shown as `NULL`.
- A value longer than 80 characters is cut to its first 80, followed by
  `[cut: N characters in all]`.
- For `unique`, each repeated value is shown once, with the row where it
  first appears and how many rows have it, in order of first appearance.
- If there are more failing rows than samples, the last line says how many
  more (`... and 7 more rows`).

`--no-samples` promises that no data value from the file appears anywhere in
the output, error messages included: no samples are collected, an
unexpected internal error is reported by its type only, and `--debug` (which
can print DuckDB's own messages) cannot be combined with it (exit 2). If both
are given, `--no-samples` wins over `--max-samples`.

## Writing values in the rules file

RowGate reads YAML with PyYAML, which follows YAML 1.1. Some unquoted values
mean something surprising in YAML 1.1, so RowGate refuses them and asks you to
quote them:

| Unquoted | YAML 1.1 reads it as | Write instead |
| -------- | -------------------- | ------------- |
| `0123`, `010`, `00` | octal numbers 83, 8, 0 | `'0123'` for text, or `123` |
| `0x1F`, `0b101` | hex and binary numbers 31, 5 | `'0x1F'`, or `31` |
| `1:30`, `1:30.5` | base-60 numbers 90, 90.5 | `'1:30'` |
| `1_000`, `1_000.5` | 1000, 1000.5 | `1000` |
| `yes`, `no`, `on`, `off`, `True`, `FALSE` | booleans | `true` / `false`, or `'yes'` for text |
| `1e3`, `1.0e3`, `08`, `0o17` | text (YAML 1.2 reads numbers) | `1000.0`, or `'1e3'` for text |

Only lowercase `true` and `false` are booleans, and only plain decimal numbers
(`12`, `-3`, `0.5`) are numbers. Quoted values and explicit tags such as
`!!int 0x1F` are taken as written. A bare `=` is text.

The rules file must also not nest more than 50 levels deep, and an alias may
not refer to the list or mapping that contains it.

## The rules

Every rule except `not_null` ignores NULL values. An empty text (`""` in a
CSV file) is a value, not NULL, so it is checked like any other value.

### regex

The pattern must match the **whole** value, as if it were wrapped in `^` and
`$` (which you may still write). Patterns use RE2, the regular expression
engine in DuckDB, with no flags: matching is case-sensitive, and `(?i)` at
the start of the pattern makes it ignore case. RE2 has no lookarounds or
backreferences; an invalid pattern is a rules error that `rowgate validate`
reports.

A column that is not text (in a Parquet file) is matched on DuckDB's text
form of each value. For a `DOUBLE` column that means `100.0`, `1.5`,
`1e+20` and `nan`, so `[0-9]+[.][0-9]+` matches the first two but not
`1e+20`. Dates are matched as `2024-01-31`, booleans as `true` and `false`.

## How RowGate reads data

### Finding the file

- `source.path` is relative to the rules file, not to the current folder.
- Only local files: a path that looks like a URL (`https://`, `s3://`,
  `file://`, ...) is refused.
- The path may not contain `*`, `?` or `[`, even in a folder name. DuckDB
  treats these as file patterns even in a quoted path (`a[1].csv` would read
  `a1.csv`), and reading several files is not supported yet.
- A zero-byte file is an error (exit 2).
- Error messages show paths as you wrote them (the rules file as given on
  the command line, the data file as written in the rules), never resolved
  to an absolute path, so CI logs do not reveal your folders or user name.
- The format comes from `source.format`, or else from the extension: `.csv`
  or `.parquet`, in any letter case.
- Folders named like `key=value` (for example `year=2024`, anywhere in the
  path) are just folders. DuckDB would otherwise add a `year` column to the
  data (Hive partitioning), so RowGate reads both formats with
  `hive_partitioning := false`. A check on such a column is an error (exit 2)
  for a missing column.

### CSV files

RowGate guesses nothing about a CSV file. It reads the header itself and gives
DuckDB fixed options, with DuckDB's automatic detection switched off:

- The delimiter is `source.delimiter` (exactly one character) or a comma. It
  is never detected, so a semicolon file read with the default delimiter has
  one column named like `id;name`.
- Quoting follows RFC 4180: fields may be wrapped in `"`, and `""` inside a
  quoted field is one `"`. Quoted fields may contain delimiters and line
  breaks. After a closing quote only the delimiter or a line break may follow,
  so `2,"ok"x` and `2,"ok" ` are errors that name the line. A `"` in the
  middle of an unquoted field, as in `a"b`, is kept as text. DuckDB drops
  spaces around a quoted field: `1, "a"` reads as `a`.
- The first line is always the header. Lines starting with `#` are data, not
  comments. A comment line above the header becomes the header, which then
  fails with a clear error.
- Every line must have as many fields as the header. A line of spaces is not
  blank: it is a line with one field.
- Blank lines at the end of the file are ignored. A blank line followed by
  more data is an error in a file with several columns. In a one-column file
  it is an empty value (NULL), so a one-column file cannot end with a NULL.
- The file must be UTF-8. A byte-order mark is allowed and is not part of the
  first column name. Invalid UTF-8 is an error that names the line and byte
  offset.
- Compressed CSV files are not supported. RowGate reads with
  `compression := 'none'`, so a name like `d.csv.gz` never makes it
  decompress: plain text with that name (and `format: csv`) is read as text.
  A file that really is gzip, bzip2, xz, zip, zstd or lz4 compressed is an
  error (exit 2) that says so.
- Line endings may be `\n`, `\r\n` or `\r`, but one file must use only one
  kind: DuckDB cannot read a file that mixes them, so RowGate stops with an
  error naming the first line that differs. A line break inside quotes is part
  of the value and does not count.
- One record (a line, or several lines inside quotes) may be up to 16 MiB,
  counted in bytes including its line break. A longer record is an error that
  names its line. Long fields are fine within that limit.
- DuckDB reads CSV files in one thread (`parallel := false`). Its parallel
  reader gives up on some valid files with line breaks inside quotes once they
  pass about 8 MB, so RowGate does not use it. This costs time on big files:
  a 3-check run on a 3-million-row file took 3.3 s instead of 2.5 s.
- Before checking, RowGate counts the data rows itself and makes sure DuckDB
  read the same number. If they differ, it stops with exit 2 rather than
  check part of the file.
- Error messages name the file and line but never print the line itself, so
  data values do not leak into logs. When DuckDB itself fails, RowGate
  reports the line number if DuckDB gave one, and otherwise says that it could
  not read the file, without suggesting the CSV is invalid. DuckDB's own
  message is left out because it can quote the row; run again with `--debug`
  to print it (along with a traceback). Only do that where data values may
  appear in the output.

Every CSV value is read as text, exactly as written:

| In the file | Value |
| ----------- | ----- |
| empty field: `1,,3` | NULL |
| quoted empty field: `1,"",3` | empty text (not NULL) |
| `007` | the text `007` (leading zeros are kept) |
| `NULL` or `"NULL"` | the text `NULL` (not NULL) |

When a check needs a number or a date, a CSV value counts only if it is
written plainly:

- **Numbers:** `12`, `-3.5`, `.5`, `1e3`. Values with spaces, digit
  separators (`1_000`, `1,000`), hex, `NaN` or `inf` are not numbers.
- **Dates:** ISO `YYYY-MM-DD` only, and the date must exist. `01/02/2024`,
  `2024-1-31` and `2024-02-30` are not dates.

A value that does not convert counts as a failing row for that check. It
never stops the run.

### Parquet files

Parquet columns keep the types stored in the file (`INTEGER`, `DOUBLE`,
`DATE`, `BOOLEAN`, ...).

Floating-point columns follow DuckDB's rules for NaN and zero:

- NaN is a value, not NULL: `not_null` does not count it.
- `unique` treats NaN as equal to NaN, so two NaNs are two failing rows.
- `unique` treats `-0.0` and `0.0` as the same value.

In a CSV file, `NaN` is only text, compared like any other text.

### The exact DuckDB options

Every option RowGate passes to DuckDB's `read_csv`, and why:

| Option | Why |
| ------ | --- |
| `header := true` | the first line is the header |
| `auto_detect := false` | guess nothing: no detected delimiter, quoting, header, comment character or types |
| `columns := {...}` | the names from RowGate's own reading of the header, all `VARCHAR` |
| `all_varchar := true` | every value is text, exactly as written |
| `delim := ...` | `source.delimiter`, or a comma |
| `quote := '"'`, `escape := '"'` | RFC 4180 quoting |
| `skip := 0` | no line before the header is skipped |
| `strict_mode := true` | rows that break the CSV format are errors |
| `allow_quoted_nulls := false` | `""` is empty text, not NULL |
| `comment := ''` | no comment character: lines starting with `#` are data |
| `null_padding := false` | short rows are errors, not padded with NULLs |
| `hive_partitioning := false` | a `key=value` folder is not a column |
| `parallel := false` | the parallel reader gives up on some valid files |
| `compression := 'none'` | the file name never makes DuckDB decompress |
| `max_line_size := 33554432` | only for a file with a record over 1 MiB; DuckDB's 2 MiB default otherwise, because a larger value slows every scan |

For `read_parquet` the only option is `hive_partitioning := false`.

### Column names

- Names are matched exactly, including case and spaces. Names with spaces,
  quotes, Unicode or SQL keywords work.
- Two columns whose names differ only in ASCII letter case (`a` and `A`) are
  an error (exit 2), because DuckDB treats them as the same name. Names that
  differ in non-ASCII case, such as `é` and `É`, are different columns, as in
  DuckDB.
- An empty column name in a CSV header gets a name from its position:
  `column_1` for the first column, `column_2` for the second, and so on. This
  is what pandas writes for its index column by default (`,a,b`). If that name
  is already used by another column (ignoring ASCII case), the header is
  ambiguous and RowGate stops with exit 2. In a Parquet file, an empty column
  name is an error.
- A check on a column the file does not have is an error (exit 2) that lists
  the columns the file does have. `columns_exist` is the rule for asserting
  that columns are present; it fails like any other check.

## License

MIT. See [LICENSE](LICENSE).
