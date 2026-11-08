# Roadmap

Planned work after v0.1. None of it is built yet: everything RowGate 0.1.0
does is described in the [README](README.md). Each item lists what "done"
means and a rough estimate of the effort, in hours.

**Total: 147 hours** for the 14 items below.

### 1. Cross-column rules

Compare two columns of the same row, such as `start_date <= end_date`.

- A new rule names two columns and a comparison (`<`, `<=`, `=`, `!=`, `>=`,
  `>`); both columns must have compatible types, otherwise exit 2.
- Rows where either value is NULL are ignored, as for other rules.
- Failing rows are counted and sampled like other row-level checks, in the
  same scan.

Estimate: 10 hours.

### 2. Foreign-key checks between two files

Every value of a column in one file must appear in a column of another file.

- The rules file can name a second local file; both are read with the same
  options and safety rules as the source.
- Missing values are failing rows, sampled with their row numbers.
- Works for CSV and Parquet, in any combination, with a test of 1M rows.

Estimate: 14 hours.

### 3. Custom SQL checks in a restricted, read-only form

Let a check be a SQL expression over one row.

- Only an expression is accepted (no statements), parsed by DuckDB and checked
  against an allowlist of functions; anything else is a rules error.
- It runs on the same locked connection that can read only the source file.
- Tests prove that file access, `COPY`, `ATTACH`, `INSTALL` and settings
  changes are refused.

Estimate: 16 hours.

### 4. Multiple files and glob patterns

Check several files, or a pattern such as `exports/*.csv`, with one rules file.

- Each file is checked on its own and reported separately, with its own exit
  status; the run exits 1 if any file fails.
- Glob characters are allowed only in this new form, never by accident in a
  plain path.

Estimate: 12 hours.

### 5. HTML report

- `--format html` writes one self-contained HTML file: no scripts or styles
  loaded from the network.
- It shows the same results and samples as the text report, escaped for HTML.
- `--no-samples` keeps every data value out of it, tested like the text output.

Estimate: 8 hours.

### 6. JUnit XML output for CI systems

- `--format junit` writes one test case per check: passing, failing (severity
  error) or skipped-with-message (severity warn).
- The output validates against the JUnit XML schema used by GitHub Actions,
  GitLab and Jenkins.

Estimate: 6 hours.

### 7. Official GitHub Action and pre-commit hook

- A GitHub Action that installs a pinned RowGate version and runs `rowgate
  check`, failing the job on exit 1 or 2.
- A pre-commit hook that runs `rowgate validate` on changed rules files.
- Both documented, with a working example repository.

Estimate: 8 hours.

### 8. Rule suggestions from statistical profiling, with no AI

- `rowgate suggest DATA` profiles a file in one scan (NULL counts, distinct
  counts, minimum and maximum, common values) and prints a starter rules file.
- Suggestions come only from these statistics; no models or network.
- The suggested file always passes `rowgate validate` and passes on the file it
  came from.

Estimate: 12 hours.

### 9. Schema and type checks, with drift detection

- A rule that asserts column types (for Parquet) or that text columns parse as
  a type (for CSV).
- `rowgate baseline` saves the schema to a file; a later check reports added,
  removed and changed columns against it.

Estimate: 14 hours.

### 10. Benchmarks on 10M+ row files, and a performance pass

- A reproducible benchmark script with generated data of 10M and 50M rows, for
  CSV and Parquet, reporting time and peak memory.
- Targets set from the first measurement and checked in CI on a smaller size.
- Candidates: one DuckDB pass instead of RowGate's own CSV pass, grouping
  several unique checks in one scan, and DuckDB's parallel CSV reader once it
  reads every valid file (a test in `tests/test_source.py` reports when the
  known failing file starts to work).

Estimate: 19 hours.

### 11. Rule severity thresholds

Allow a number or share of failing rows, such as "up to 1% may be NULL".

- A check can set `max_failing_rows` or `max_failing_share`; within the
  threshold it passes and says how many rows failed.
- Thresholds are validated (non-negative, share at most 100%).

Estimate: 5 hours.

### 12. Compressed CSV and Hive-partitioned Parquet

- Gzip and zstd CSV files, chosen explicitly in the rules (never guessed from
  the file name), with the same row-count and UTF-8 guarantees as plain files.
- A folder of Hive-partitioned Parquet files, where partition columns are
  opted into by name.

Estimate: 12 hours.

### 13. Timestamp columns in range

`range` on timestamp columns is refused in v0.1 (exit 2).

- Bounds can be timestamps (`2024-01-31T10:00:00`) as well as dates; a date
  bound on a timestamp column means the whole day.
- Time zones are handled explicitly: `TIMESTAMP WITH TIME ZONE` values are
  compared in UTC, and text timestamps must include an offset or are failing
  rows.
- Tests cover the edges of each day and daylight-saving changes.

Estimate: 8 hours.

### 14. Samples for Parquet files with a column named file_row_number

Such a file hides DuckDB's own row numbers, so v0.1 refuses to show samples
for it (exit 2) and asks for `--no-samples`.

- Row numbers come from a source that a data column cannot hide, such as
  reading the column under another name.
- Samples for such a file have the right row numbers, tested with the column
  in every letter case and with several row groups.

Estimate: 3 hours.
