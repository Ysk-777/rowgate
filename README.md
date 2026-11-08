# RowGate

RowGate checks a local CSV or Parquet file against rules you write in YAML,
and tells you which rows break them. It runs the checks with DuckDB, prints
a clear report (as text or as JSON), and exits with a code that CI can use:
0 if the data passes, 1 if a check fails, 2 if the rules or the data cannot
be used.

```yaml
version: 1
source:
  path: orders.csv
checks:
  - rule: unique
    column: order_id
  - rule: in_set
    column: status
    values: [new, paid, shipped]
  - rule: range
    column: amount
    min: 0
    max: 10000
```

## Who it is for

RowGate is for people who receive data files, such as exports, extracts or
files from a partner, and want to stop a bad file before it is loaded or
published: as a step in CI, a pre-load check in a pipeline, or a quick check
on the command line.

- It runs locally, on one file, with nothing to set up: no server, no
  database, no account.
- It needs no network, sends nothing anywhere, and refuses URLs.
- It guesses nothing about your data. A CSV file is read exactly as written,
  and numbers are compared exactly, digit by digit.
- Its output is deterministic, and `--no-samples` keeps every data value
  out of it, for logs that others can read.

## Built with

- Python 3.11 or newer
- [DuckDB](https://duckdb.org) to read and check the data (MIT license)
- [Typer](https://typer.tiangolo.com) for the command line (MIT)
- [Rich](https://github.com/Textualize/rich) for the text report (MIT)
- [PyYAML](https://pyyaml.org) for the rules file (MIT)
- [pydantic](https://docs.pydantic.dev) to validate the rules (MIT)

The tests use pytest, pytest-cov, Hypothesis, ruff and mypy.

## Install

RowGate is not on PyPI yet. Install it from the repository, into a virtual
environment:

```bash
git clone https://github.com/Ysk-777/rowgate.git
```

```bash
cd rowgate
```

```bash
python -m pip install .
```

Or build a wheel and install that (`python -m pip install build`, then
`python -m build`, then `python -m pip install dist/rowgate-0.1.0-py3-none-any.whl`).

Check that it works:

<!-- readme-test: run in empty, exit 0 -->
```bash
rowgate --version
```

```text
rowgate 0.1.0
```

## Quick start

Write a starter rules file:

<!-- readme-test: run in empty, exit 0 -->
```bash
rowgate init
```

```text
created rowgate.yaml; set source.path to your data file, then run rowgate validate rowgate.yaml
```

`rowgate.yaml` has one active check and a commented-out example of every
other rule. It is valid as written:

<!-- readme-test: run in empty, exit 0 -->
```bash
rowgate validate rowgate.yaml
```

```text
rowgate.yaml is valid: 1 check on data.csv (csv)
```

It points at `data.csv`, which does not exist yet, so a check stops with
exit code 2:

<!-- readme-test: run in empty, exit 2 -->
```bash
rowgate check rowgate.yaml
```

```text
error: data file 'data.csv' not found, relative to the rules file 'rowgate.yaml'
```

Set `source.path` to your file, add checks, and run `rowgate check` again.

## A full example

The [`examples`](examples) folder has two invented data files, with a rules
file for each; together they use all seven rules. All of the data there is
fictional. The commands below run in that folder (`cd examples`).

`orders.yaml` checks `orders.csv`, and everything passes:

<!-- readme-test: run in examples, exit 0 -->
```bash
rowgate validate orders.yaml
```

```text
orders.yaml is valid: 8 checks on orders.csv (csv)
```

<!-- readme-test: run in examples, exit 0 -->
```bash
rowgate check orders.yaml
```

```text
Source: orders.csv (csv, 8 rows, 5 columns)

PASS  1. columns_exist: all 5 columns present
PASS  2. row_count: 8 rows, expected between 1 and 100000
PASS  3. not_null 'order_id': no NULL values
PASS  4. unique 'order_id': no repeated values
PASS  5. in_set 'status': every value is in the set
PASS  6. range 'amount': every value is a number from 0 to 10000
PASS  7. range 'ordered_on': every value is a date from 2026-01-01 to 2026-12-31
PASS  8. regex 'sku': every value matches the pattern

8 checks: 8 passed, 0 failed, 0 warned
```

`shipments.yaml` checks `shipments.parquet`, which has problems on purpose:

```yaml
version: 1
source:
  path: shipments.parquet
checks:
  - rule: columns_exist
    columns: [shipment_id, order_id, method, weight_kg, shipped_on, tracking]
  - rule: row_count
    min: 1
  - rule: not_null
    column: tracking
  - rule: unique
    column: shipment_id
  - rule: in_set
    column: method
    values: [ground, air, sea]
  - rule: range
    column: weight_kg
    min: 0
    max: 50
    severity: warn
  - rule: range
    column: shipped_on
    min: 2026-01-01
  - rule: regex
    column: tracking
    pattern: 'TRK[0-9]{6}'
```

<!-- readme-test: run in examples, exit 1 -->
```bash
rowgate check shipments.yaml
```

```text
Source: shipments.parquet (parquet, 8 rows, 6 columns)

PASS  1. columns_exist: all 6 columns present
PASS  2. row_count: 8 rows, expected at least 1
FAIL  3. not_null 'tracking': 1 of 8 rows is NULL
        row 3: NULL
FAIL  4. unique 'shipment_id': 2 of 8 rows have a value that appears more than once
        row 3: 5003, appears 2 times
FAIL  5. in_set 'method': 2 of 8 rows have a value outside the set
        row 4: 'Air'
        row 7: 'drone'
WARN  6. range 'weight_kg': 2 of 8 rows are not a number from 0 to 50
        row 5: nan
        row 6: -2.0
FAIL  7. range 'shipped_on': 1 of 8 rows is not a date of at least 2026-01-01
        row 6: 2025-12-31
FAIL  8. regex 'tracking': 1 of 8 rows does not match the pattern
        row 6: 'trk106'

8 checks: 2 passed, 5 failed, 1 warned
```

Five checks fail, so the exit code is 1. The `weight_kg` range has
`severity: warn`: it is reported, but never fails the run on its own.
Each failing check lists up to five failing rows, by row number, with the
value; for `unique`, each repeated value is shown once, with the row where
it first appears.

With `--no-samples`, no value from the data appears anywhere in the output:

<!-- readme-test: run in examples, exit 1 -->
```bash
rowgate check shipments.yaml --no-samples
```

```text
Source: shipments.parquet (parquet, 8 rows, 6 columns)

PASS  1. columns_exist: all 6 columns present
PASS  2. row_count: 8 rows, expected at least 1
FAIL  3. not_null 'tracking': 1 of 8 rows is NULL
FAIL  4. unique 'shipment_id': 2 of 8 rows have a value that appears more than once
FAIL  5. in_set 'method': 2 of 8 rows have a value outside the set
WARN  6. range 'weight_kg': 2 of 8 rows are not a number from 0 to 50
FAIL  7. range 'shipped_on': 1 of 8 rows is not a date of at least 2026-01-01
FAIL  8. regex 'tracking': 1 of 8 rows does not match the pattern

8 checks: 2 passed, 5 failed, 1 warned
```

The same run as JSON, with at most one sample per check:

<!-- readme-test: run in examples, exit 1 -->
```bash
rowgate check shipments.yaml --format json --max-samples 1
```

<details>
<summary>The JSON report</summary>

```json
{
  "schema_version": 1,
  "rowgate_version": "0.1.0",
  "source": {
    "path": "shipments.parquet",
    "format": "parquet",
    "columns": 6
  },
  "summary": {
    "passed": 2,
    "failed": 5,
    "warned": 1,
    "total_rows": 8
  },
  "checks": [
    {
      "index": 1,
      "rule": "columns_exist",
      "column": null,
      "column_type": null,
      "severity": "error",
      "status": "passed",
      "failing_rows": null,
      "message": "all 6 columns present",
      "samples": null,
      "not_shown": null
    },
    {
      "index": 2,
      "rule": "row_count",
      "column": null,
      "column_type": null,
      "severity": "error",
      "status": "passed",
      "failing_rows": null,
      "message": "8 rows, expected at least 1",
      "samples": null,
      "not_shown": null
    },
    {
      "index": 3,
      "rule": "not_null",
      "column": "tracking",
      "column_type": "VARCHAR",
      "severity": "error",
      "status": "failed",
      "failing_rows": 1,
      "message": "1 of 8 rows is NULL",
      "samples": [
        {
          "row": 3,
          "value": null,
          "count": null,
          "length": null,
          "truncated": false
        }
      ],
      "not_shown": 0
    },
    {
      "index": 4,
      "rule": "unique",
      "column": "shipment_id",
      "column_type": "BIGINT",
      "severity": "error",
      "status": "failed",
      "failing_rows": 2,
      "message": "2 of 8 rows have a value that appears more than once",
      "samples": [
        {
          "row": 3,
          "value": "5003",
          "count": 2,
          "length": 4,
          "truncated": false
        }
      ],
      "not_shown": 0
    },
    {
      "index": 5,
      "rule": "in_set",
      "column": "method",
      "column_type": "VARCHAR",
      "severity": "error",
      "status": "failed",
      "failing_rows": 2,
      "message": "2 of 8 rows have a value outside the set",
      "samples": [
        {
          "row": 4,
          "value": "Air",
          "count": null,
          "length": 3,
          "truncated": false
        }
      ],
      "not_shown": 1
    },
    {
      "index": 6,
      "rule": "range",
      "column": "weight_kg",
      "column_type": "DOUBLE",
      "severity": "warn",
      "status": "warned",
      "failing_rows": 2,
      "message": "2 of 8 rows are not a number from 0 to 50",
      "samples": [
        {
          "row": 5,
          "value": "nan",
          "count": null,
          "length": 3,
          "truncated": false
        }
      ],
      "not_shown": 1
    },
    {
      "index": 7,
      "rule": "range",
      "column": "shipped_on",
      "column_type": "DATE",
      "severity": "error",
      "status": "failed",
      "failing_rows": 1,
      "message": "1 of 8 rows is not a date of at least 2026-01-01",
      "samples": [
        {
          "row": 6,
          "value": "2025-12-31",
          "count": null,
          "length": 10,
          "truncated": false
        }
      ],
      "not_shown": 0
    },
    {
      "index": 8,
      "rule": "regex",
      "column": "tracking",
      "column_type": "VARCHAR",
      "severity": "error",
      "status": "failed",
      "failing_rows": 1,
      "message": "1 of 8 rows does not match the pattern",
      "samples": [
        {
          "row": 6,
          "value": "trk106",
          "count": null,
          "length": 6,
          "truncated": false
        }
      ],
      "not_shown": 0
    }
  ]
}
```

</details>

## Commands

- `rowgate init [PATH]`
- `rowgate validate RULES.yaml [--debug]`
- `rowgate check RULES.yaml [--format text|json] [--no-samples] [--max-samples N] [--debug]`
- `rowgate --version`

In detail:

- `init` writes a commented starter rules file to `PATH` (`rowgate.yaml` by
  default). It never overwrites anything: if a file, or a link (even a
  broken one), already has that name, it stops with exit code 2. The folder
  must exist, and `PATH` must not be a folder or a URL. The file is UTF-8
  with `\n` line endings on every system.
- `validate` checks a rules file for mistakes without reading any data:
  YAML errors, unknown rules and fields, wrong types, and invalid regular
  expressions, each with the check number and field.
- `check` reads the data file the rules name and runs every check, in order.
  `--format json` prints JSON instead of text. `--max-samples N` (1 to
  1000, default 5) sets how many failing rows each check lists;
  `--no-samples` lists none and keeps every data value out of the output.
  `--debug` prints a traceback, and DuckDB's own message when it fails to
  read the data. That message can include data values, so `--debug` cannot
  be combined with `--no-samples`.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | No check with severity `error` failed (warnings are allowed). |
| 1 | At least one check with severity `error` failed. |
| 2 | Bad usage, a bad rules file, an unreadable data file, or an internal error. |

Every error is one `error:` line on stderr, with any details indented below
it, and never a traceback unless `--debug` is given. That includes mistakes
on the command line itself:

<!-- readme-test: run in examples, exit 2 -->
```bash
rowgate check orders.yaml --max-samples 0
```

```text
error: --max-samples must be a whole number from 1 to 1000, got '0'
```

## The rules file

```yaml
version: 1                  # always 1
source:
  path: data/orders.csv     # relative to this rules file
  format: csv               # optional: csv or parquet; otherwise from the extension
  delimiter: ";"            # optional, CSV only: one character; the default is a comma
checks:
  - rule: not_null
    column: order_id
    severity: warn          # optional on every check: error (default) or warn
```

- `checks` is a list of at least one check, run in the order written.
- A check with `severity: warn` is reported as `WARN` when it fails, and never
  makes the exit code 1.
- Unknown fields, unknown rules and wrong types are errors that name the
  check number and field, often with a suggestion (`colum`: did you mean
  `column`?).
- Every rule except `not_null` ignores NULL values. An empty text (`""` in a
  CSV file) is a value, not NULL, so it is checked like any other value.

### The seven rules

| Rule | Fields | Fails when |
| ---- | ------ | ---------- |
| `columns_exist` | `columns`: a list of names | a listed column is missing (the whole file fails, not rows) |
| `row_count` | `min`, `max`: whole numbers, at least one | the number of data rows is outside the bounds (inclusive) |
| `not_null` | `column` | a value is NULL; each such row fails |
| `unique` | `column` | a value appears more than once; every row with it fails, so three copies are three failing rows |
| `in_set` | `column`, `values`: a list | a value is not in the list |
| `range` | `column`, `min` and/or `max`: numbers or dates | a value is outside the bounds (inclusive), or is not a number or date at all |
| `regex` | `column`, `pattern` | a value does not match the whole pattern |

A check on a column the file does not have stops the run with exit code 2
and lists the columns the file does have. Use `columns_exist` to assert that
columns are present as a check that fails like any other.

### in_set

The values are all text, all numbers or all booleans (see
[Writing values in the rules file](#writing-values-in-the-rules-file)), and
that decides how a value in the data is compared:

| Set of | Text value (CSV, or a Parquet text column) | Parquet column of another type |
| ------ | ------------------------------------------- | ------------------------------ |
| text, `[new, paid]` | exact text, case-sensitive: `New` is not `new`, `1.0` is not `'1'` | exit 2 |
| numbers, `[1, 2.5]` | the value must be a plain number equal to one in the set | integer, decimal or float columns only, else exit 2 |
| booleans, `[true]` | only the lowercase text `true` or `false` matches | boolean columns only, else exit 2 |

- With a set of numbers, a text value counts if it is a plain number (see
  [CSV files](#csv-files)) and equals a number in the set: `1`, `01`, `+1`,
  `1.0`, `1.` and `1e0` all match `1`. `" 1"` does not (spaces make it text),
  and neither do `NaN` or `inf`.
- Numbers are compared exactly, digit by digit: `9007199254740993` is not
  `9007199254740992`, `1000000000000000.01` is not `1000000000000000`, and
  `0.30000000000000004` is not `0.3`, though each pair is one number as
  64-bit floating point. There is no limit on the number of digits (see
  [Exact numbers](#exact-numbers)).
- On Parquet columns: an integer column matches only whole members; a
  decimal column, such as `DECIMAL(18,2)`, matches only members it can hold
  exactly (`0.005` never matches); a `FLOAT` or `DOUBLE` column is compared
  in its own type, with each member turned into the nearest `FLOAT` or
  `DOUBLE`, so a `FLOAT` stored as `0.1` is in `[0.1]`. NaN and infinity are
  never in a set.
- With a set of booleans, `TRUE`, `True`, `1`, `yes` and `t` fail. To accept
  other spellings, use a set of text: `['TRUE', 'FALSE']`.
- An empty text is a value: put `''` in the set to accept it.
- `in_set` does not run on Parquet date, timestamp, list or other columns
  (exit 2).
- Numbers in the rules file may have any number of digits, whole or not.
  A whole number means exactly the same as that number written with `.0`:
  `[100000000000000000000000000000000000000000]` and
  `[100000000000000000000000000000000000000000.0]` are the same set.

### range

`min` and `max` are inclusive, and at least one is required; `min` may not
be greater than `max`. They are both numbers or both dates (`2024-01-31`,
quoted or not), and the column must fit:

| Bounds | Text value (CSV, or a Parquet text column) | Parquet column |
| ------ | ------------------------------------------- | -------------- |
| numbers | must be a plain, finite number within the bounds | integer, decimal or float; a date column is exit 2 |
| dates | must be a real ISO date (`YYYY-MM-DD`) within the bounds | date; a number column is exit 2 |

- Text values and the bounds are compared exactly, digit by digit:
  `9007199254740993` fails `max: 9007199254740992`, `1000000000000000.01`
  fails `max: 1000000000000000`, `12345678.123456789012` fails
  `max: 12345678.12345678901`, and `0.30000000000000004` fails `max: 0.3`,
  though each would pass as 64-bit floating point. There is no limit on the
  number of digits (see [Exact numbers](#exact-numbers)).
- In text, anything that is not a plain number is a failing row: `nan`,
  `inf`, `Infinity`, `" 5"`, `1_000`, `abc`, and the empty text. So is a
  number too large for a 64-bit float, such as `1e400`: it would be infinity.
- Parquet integer and decimal columns are compared exactly too: an integer
  column against `max: 2.5` passes 2 and fails 3, and a `DECIMAL(18,2)`
  value `1000000000000000.01` fails `max: 1000000000000000.005`.
- `FLOAT` and `DOUBLE` columns are compared in their own type: the bound is
  turned into the nearest `FLOAT` or `DOUBLE`, so a `FLOAT` value stored as
  `0.1` passes `max: 0.1` and `min: 0.1`. (The `FLOAT` nearest to 0.1 is
  0.100000001490116..., slightly above the `DOUBLE` nearest to it.) A bound
  too large for the type becomes infinity.
- In a float column, NaN and infinity are failing rows for any range. (DuckDB
  orders NaN above every number, so on its own it would pass a range that has
  only a `min`.)
- Timestamp columns are not supported yet (exit 2); see the
  [roadmap](ROADMAP.md). Boolean, time, list and other columns are exit 2.

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

### Exact numbers

RowGate never rounds a number before comparing it, in `in_set` or `range`:

- Numbers in the rules file keep every digit you write: `0.1` is one tenth,
  not the 64-bit float nearest to it, and `12345678.12345678901` keeps all
  19 digits. Whole numbers of any size are kept exactly too, beyond the 39
  digits of DuckDB's largest integer type, and `12` means exactly the same as
  `12.0`. (Python reads at most 4,300 digits into a whole number; a longer
  one is read as the same number with `.0`.)
- A text value is compared with each bound as a 64-bit float first, which
  settles almost every value at full speed. A value whose float is next to
  the bound's (within one step) is settled by its text if it is written
  exactly like the bound (`100` for `max: 100`), and otherwise compared digit
  by digit, as sign, digits and exponent. For `in_set`, a whole number from
  -2^127 to 2^127 - 1 (DuckDB's `HUGEINT`) is compared as a `HUGEINT`,
  which is faster, and any other number by its digits.
- So there is no limit at 38 significant digits, the most a DuckDB
  `DECIMAL` holds: a value with 60 digits is compared exactly with a bound
  with 50. (A fixed `DECIMAL(38,12)` would not do: it rounds
  `0.30000000000000004` to `0.3`.)
- The one limit is the exponent: a text value whose exponent does not fit a
  64-bit integer (more than 18 digits, as in `1e-99999999999999999999`) is a
  failing row when it is next to a bound of the same sign, because only the
  exponent could decide. Python's `Decimal`, which reads the rules file,
  refuses such exponents in the rules file itself.
- Parquet integer and decimal columns hold exact values, and the bound is
  rounded to the column's scale in the safe direction (down for `max`, up for
  `min`), so the comparison is exact. A bound outside the column type's range
  passes or fails every value, as it should.

### Writing values in the rules file

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

## Text output

Each check prints one line, in the order of the rules file. A failing check
also lists **samples**: up to 5 of its failing rows (`--max-samples N`, any
N from 1 to 1000; anything else is a usage error, exit 2).

- A sample shows the data row number and the value: `row 4: 'abc'`. Rows are
  numbered from 1, the header is not counted, and a record that spans lines
  inside quotes is one row. Samples are in file order. See
  [How rows are numbered](#how-rows-are-numbered).
- Text values are shown in quotes, exactly as written, except that
  characters that cannot be printed are written as escapes: a line break is
  `\n`, a tab `\t`, a terminal escape code `\x1b`, and invisible characters
  such as a zero-width space or a right-to-left mark `\u200b` and `\u202e`.
  So nothing in the data can break, colour or rearrange the output, and rich
  markup such as `[bold]` is shown as text. Backslashes are shown as written,
  one for one: `C:\data` is `'C:\data'`. (The other side of that choice: a
  value holding a backslash and an `n` looks the same as one holding a line
  break.) Values of other types (numbers, dates) are shown in DuckDB's text
  form, without quotes. A missing value is shown as `NULL`.
- A value longer than 80 characters is cut to its first 80, followed by
  `[cut: N characters in all]`.
- For `unique`, each repeated value is shown once, with the row where it
  first appears and how many rows have it, in order of first appearance.
- If there are more failing rows than samples, the last line says how many
  more (`... and 7 more rows`).
- Only the status words are coloured, and only when the output is a
  terminal: `NO_COLOR=1` removes the colour (`FAIL` stays bold),
  `FORCE_COLOR=1` forces it even into a file or pipe, and `--format json`
  never contains escape codes, whatever is set.

`--no-samples` promises that no data value from the file appears anywhere in
the output, error messages included: no samples are collected, an
unexpected internal error is reported by its type only, and `--debug` (which
can print DuckDB's own messages) cannot be combined with it (exit 2). If both
are given, `--no-samples` wins over `--max-samples`. Column names, the file
path and the values in your rules file are still shown: they are not data.
This is tested by putting a marker string in the data of every rule and of
every kind of unreadable file, and checking it never reaches stdout or stderr.

### How rows are numbered

- **Parquet** rows are numbered by DuckDB's `file_row_number`: each row's
  position in the file. It does not depend on the order in which DuckDB's
  threads happen to read the row groups.
- **CSV** files have no such number, so RowGate numbers rows with
  `row_number() OVER ()` as DuckDB reads them. That order is the file's
  order because RowGate reads CSV files with `parallel := false`: DuckDB
  runs a query in as many threads as its data source allows, so with a
  one-thread reader the rows reach the numbering one chunk at a time, in the
  order the reader reads them from the file. (DuckDB's parallel reader would
  split the file into ranges read side by side; see
  [CSV files](#csv-files) for why RowGate does not use it.)
- A Parquet file with its own column named `file_row_number` (in any letter
  case) hides DuckDB's row numbers behind that column, so it cannot show
  samples: such a run stops with exit 2 and says so, and works with
  `--no-samples`. In a CSV file that name is an ordinary column.

Both are tested against known answers: a 30,000-record CSV file with line
breaks inside quotes, and a 200,000-row Parquet file in 98 row groups.

## JSON output

`rowgate check RULES.yaml --format json` prints the report as one JSON
document on stdout, and nothing else; exit codes are the same as for text.
[The full example](#a-full-example) shows a complete report. Every key is
always present, in this order:

| Key | Value |
| --- | ----- |
| `schema_version` | `1`; it changes only when a key is removed or changes meaning (new keys may be added) |
| `rowgate_version` | the RowGate version, such as `"0.1.0"` |
| `source` | `path` (as written in the rules file), `format` (`"csv"` or `"parquet"`), `columns` (how many) |
| `summary` | `passed`, `failed`, `warned`: numbers of checks; `total_rows`: data rows in the file |
| `checks` | one object per check, in the order of the rules file (below) |

Each check:

| Key | Value |
| --- | ----- |
| `index` | the check's number in the rules file, from 1 |
| `rule`, `column`, `severity` | as in the rules file; `column` is `null` for `columns_exist` and `row_count` |
| `column_type` | DuckDB's type of the column (`"VARCHAR"` for every CSV column); `null` for whole-file checks |
| `status` | `"passed"`, `"failed"` (severity `error`) or `"warned"` (severity `warn`) |
| `failing_rows` | how many rows fail; `null` for whole-file checks |
| `message` | the text report's line for the check |
| `samples` | a list of failing rows in file order (`[]` when none fail), or `null` with `--no-samples` and for whole-file checks |
| `not_shown` | failing rows beyond the samples (for `unique`, repeated values); `null` when `samples` is |

Each sample has `row` (1-based; for `unique`, where the value first
appears), `value`, `count` (for `unique`, how many rows have the value;
otherwise `null`), `length` (the value's length in characters) and
`truncated`. A missing value is `null`, with `length` `null`.

How values are written:

- **Every data value is a JSON string**: CSV text exactly as written, or
  DuckDB's text form of a Parquet value (`column_type` says which type it
  came from). So a `DOUBLE` NaN is `"nan"`, infinity `"inf"`, a `BIGINT`
  `9007199254740993` keeps every digit (a JSON number would become
  `9007199254740992` in many readers), and dates are `"2024-01-31"`.
- The output is strict JSON, written with Python's `allow_nan=False`: there
  are no `NaN` or `Infinity` literals anywhere, so every JSON parser reads
  it. The only JSON numbers are counts, row numbers and lengths, all exact
  integers.
- The output is ASCII: control characters, terminal escape codes and every
  non-ASCII character are `\u` escapes (`"caf\u00e9"`), so a value can never
  break the document or a terminal.
- A value longer than 80 characters is cut to its first 80, with
  `"truncated": true` and its full `length` in characters.
- `--no-samples` sets every `samples` and `not_shown` to `null`, and no data
  value appears in stdout or stderr, as for text.

Errors are one JSON object on one line of stderr, with exit code 2:

<!-- readme-test: run in examples, exit 2 -->
```bash
rowgate check missing.yaml --format json
```

```json
{"error": "invalid rules file 'missing.yaml' (1 problem)", "details": ["file not found"]}
```

`details` lists further lines, and is `[]` when there are none; for a rules
file, it lists every problem. With `--debug`, a `debug` key adds DuckDB's
original message and the traceback, as lines. Mistakes on the command line
are JSON too whenever it asks for `--format json`.

## Using RowGate in CI

`rowgate check` exits with 1 when a check with severity `error` fails, and
with 2 when the rules or the data cannot be used, so either stops a CI job.
A GitHub Actions job that checks a file on every push:

```yaml
name: Data checks
on: [push, pull_request]

permissions:
  contents: read

jobs:
  rowgate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.13"
      # Pin a tag or commit you have reviewed, rather than the latest code.
      - run: python -m pip install "rowgate @ git+https://github.com/Ysk-777/rowgate@main"
      - run: rowgate check rules/orders.yaml --no-samples
```

- `--no-samples` keeps every data value out of the CI log, which matters
  when the log is visible to more people than the data. Leave it out where
  the samples help and may be shown.
- To keep a machine-readable report, write the JSON to a file, such as
  `rowgate check rules/orders.yaml --format json > rowgate-report.json`, and
  keep it with your CI's artifact step. The exit code is the same.
- To check rules files without data (for example, in a repository that holds
  only rules), run `rowgate validate` on each.

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
  Paths and column names are quoted and escaped the same way as sample
  values, with backslashes kept: a Windows path is shown as
  `'C:\Users\me\rules.yaml'`, not with doubled backslashes.
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
  pass about 8 MB, so RowGate does not use it (see [Limitations](#limitations)).
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

For `read_parquet` the only options are `hive_partitioning := false` and,
in the scans that collect samples, `file_row_number := true`.

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

## How RowGate builds SQL

RowGate never puts text from a rules file or a data file name into SQL.
Values (in_set members, bounds, patterns, the delimiter and the file path)
are bound as query parameters, and column names are written as quoted
identifiers with any `"` inside doubled. Everything else in RowGate's SQL is
fixed text in its source code. DuckDB runs in memory, can read only the one
data file, cannot write files or install or load extensions, and its
settings are locked before any check runs.

The test suite checks this with hostile names, values, patterns and file
names, such as `x" ; drop table y; --`, `$rowgate_path`, rich markup,
terminal escape codes, and the same name in two Unicode forms (NFC and
NFD, which stay different columns). Besides checking the results, it
records every SQL statement sent to DuckDB and checks that none holds any
of that text once the quoted column names are taken out.

## Running the tests

From a clone, in a virtual environment:

```bash
python -m pip install -e ".[dev]"
```

```bash
ruff check .
```

```bash
ruff format --check .
```

```bash
mypy
```

```bash
pytest --cov
```

The tests need no network and create their data in temporary folders.
Among them:

- `tests/test_readme.py` runs every command shown with its output in this
  README, against a copy of `examples/` or an empty folder, and checks that
  the output and exit code match exactly.
- `tests/test_reference.py` compares every rule with a simple pure-Python
  reference on CSV files generated by Hypothesis. Set
  `HYPOTHESIS_PROFILE=ci` for the fixed examples CI uses, or
  `HYPOTHESIS_PROFILE=long` for 2,000 random ones.
- `tests/golden/` holds the exact JSON output for a CSV file and a Parquet
  file.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full set of checks a change
must pass.

## Limitations

- **One local file per rules file.** No URLs, glob patterns, several files,
  compressed CSV or Hive-partitioned Parquet; see the [roadmap](ROADMAP.md).
- **No timestamps in `range`** (exit 2), and `in_set` does not run on date
  columns.
- **CSV files are read in one thread**, because DuckDB's parallel reader
  gives up on some valid files. On a 3-million-row file with seven checks, a
  run takes about 3.7 s on the machine RowGate is developed on.
- **Exact number comparison costs time when many values sit next to a
  bound** and are written differently from it. In the worst case, when
  every one of 3 million values is within one step of the bound, a range
  check scans in about 3.5 s instead of 0.8 s, and `in_set` with decimal
  members on decimal text in about 4 s.
- **A text exponent of more than 18 digits** (as in `1e-99999999999999999999`)
  next to a bound of the same sign is a failing row, because it cannot be
  compared exactly.
- **A Parquet column named `file_row_number`** blocks samples for that file
  (exit 2); `--no-samples` works.
- **Regular expressions are RE2**, not Python's `re`: no lookarounds or
  backreferences.
- **In the text report, a backslash followed by `n` in the data looks the
  same as a line break**, which is shown as `\n`. The JSON report has no
  such ambiguity.
- RowGate is tested on Linux and, in CI, on Windows, with Python 3.11, 3.12
  and 3.13 and DuckDB 2.0. It has not been tested on macOS.

## License

MIT. See [LICENSE](LICENSE).
