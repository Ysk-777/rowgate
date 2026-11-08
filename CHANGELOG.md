# Changelog

All notable changes to RowGate are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-08

The first version.

### Added

- `rowgate check RULES.yaml`: check a local CSV or Parquet file against
  the rules in a YAML file, with exit code 0 (no check with severity
  `error` failed), 1 (one did) or 2 (the rules or the data cannot be used).
- `rowgate validate RULES.yaml`: check a rules file without reading data.
- `rowgate init [PATH]`: write a commented starter rules file; never
  overwrites a file.
- `rowgate --version`.
- Seven rules: `columns_exist`, `row_count`, `not_null`, `unique`,
  `in_set`, `range` (numbers and dates) and `regex` (RE2, full match),
  each with severity `error` or `warn`.
- Exact number comparison, digit by digit, for text and Parquet integer and
  decimal columns, with no limit on the number of digits; `FLOAT` and
  `DOUBLE` columns compared in their own type.
- CSV reading with no guessing: fixed DuckDB options, RowGate's own check of
  UTF-8, line endings, field counts and the row count, and errors that name
  the line without printing it.
- Text report with up to 5 samples per failing check (`--max-samples N`,
  1 to 1000), in file order, with row numbers, escaped values and long values
  cut at 80 characters.
- JSON report (`--format json`, schema version 1): every key always
  present, every data value a string, strict ASCII JSON, and errors as JSON
  on stderr.
- `--no-samples`: no data value anywhere in the output, error messages
  included.
- `--debug`: a traceback and DuckDB's own message, for a failure to read
  the data.
- Safety: values bound as SQL parameters, column names quoted, and an
  in-memory DuckDB connection that can read only the data file, with
  extension loading off and its settings locked. No network access and no
  telemetry.
- Examples with invented CSV and Parquet data, and a test that the README's
  example commands give exactly the output it shows.

[0.1.0]: https://github.com/Ysk-777/rowgate/releases/tag/v0.1.0
