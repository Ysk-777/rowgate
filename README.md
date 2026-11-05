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

A traceback is printed only when `--debug` is passed.

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

## How RowGate reads data

### Finding the file

- `source.path` is relative to the rules file, not to the current folder.
- Only local files: a path that looks like a URL (`https://`, `s3://`,
  `file://`, ...) is refused.
- The path may not contain `*`, `?` or `[`, even in a folder name. DuckDB
  treats these as file patterns even in a quoted path (`a[1].csv` would read
  `a1.csv`), and reading several files is not supported yet.
- A zero-byte file is an error (exit 2).
- The format comes from `source.format`, or else from the extension: `.csv`
  or `.parquet`, in any letter case.

### CSV files

RowGate guesses nothing about a CSV file. It reads the header itself and gives
DuckDB fixed options, with DuckDB's automatic detection switched off:

- The delimiter is `source.delimiter` (exactly one character) or a comma. It
  is never detected, so a semicolon file read with the default delimiter has
  one column named like `id;name`.
- Quoting follows RFC 4180: fields may be wrapped in `"`, and `""` inside a
  quoted field is one `"`. Quoted fields may contain delimiters and line
  breaks.
- The first line is always the header. Lines starting with `#` are data, not
  comments. A comment line above the header becomes the header, which then
  fails with a clear error.
- Every line must have as many fields as the header, and blank lines are an
  error. (A blank line in a one-column file is a row with an empty value.)
- The file must be UTF-8. A byte-order mark is allowed and is not part of the
  first column name. Invalid UTF-8 is an error that names the line and byte
  offset.
- Line endings may be `\n`, `\r\n` or `\r`.
- Before checking, RowGate counts the data rows itself and makes sure DuckDB
  read the same number. If they differ, it stops with exit 2 rather than
  check part of the file.

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

### Column names

- Names are matched exactly, including case and spaces. Names with spaces,
  quotes, Unicode or SQL keywords work.
- Two columns whose names differ only in ASCII letter case (`a` and `A`) are
  an error (exit 2), because DuckDB treats them as the same name. Names that
  differ in non-ASCII case, such as `é` and `É`, are different columns, as in
  DuckDB.
- An empty column name is an error. DuckDB would invent a name for it.
- A check on a column the file does not have is an error (exit 2) that lists
  the columns the file does have. `columns_exist` is the rule for asserting
  that columns are present; it fails like any other check.

## License

MIT. See [LICENSE](LICENSE).
