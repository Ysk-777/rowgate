# Contributing to RowGate

Thank you for helping. This page says how to set up a clone, which checks a
change must pass, how to write commits, and how to report a bug.

## Ground rules

- **No real personal data, anywhere.** Test data, examples and bug reports
  use invented values only: no real names, email addresses, phone numbers,
  addresses, account numbers or customer records, and nothing copied from a
  real file. If a bug needs a file to show it, make one up that shows the
  same problem.
- **No secrets.** No passwords, API keys or tokens in code, tests or data,
  not even fake ones that look real.
- **No network and no telemetry.** RowGate reads local files only, and its
  tests must pass without a network connection.
- **No AI or machine-learning features**, models or calls to them.
- New dependencies must have an MIT, BSD or Apache 2.0 license. Ask in an
  issue first.
- Planned work is listed in [ROADMAP.md](ROADMAP.md); please open an issue
  before starting on an item, or on anything not listed there.

## Setting up

You need Python 3.11 or newer.

```bash
git clone https://github.com/Ysk-777/rowgate.git
```

```bash
cd rowgate
```

```bash
python -m venv .venv
```

Activate it (`source .venv/bin/activate`, or `.venv\Scripts\activate` on
Windows), then install RowGate with the development tools:

```bash
python -m pip install -e ".[dev]"
```

## The checks a change must pass

Every commit must pass all of these, which CI runs on Ubuntu and Windows with
Python 3.11, 3.12 and 3.13:

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
mypy --platform win32
```

```bash
mypy --platform darwin
```

```bash
pytest --cov
```

- `mypy` runs in strict mode on `src` and `tests`. Do not add
  `# type: ignore`; write code that mypy understands, such as an
  `if sys.platform == "win32": pytest.skip(...)` at the start of a test
  that cannot run on Windows.
- Coverage, including branches, must stay at or above 90%. It is 100% today;
  please keep it there.
- `pytest` uses the Hypothesis profile `dev` (new random examples each run)
  unless `HYPOTHESIS_PROFILE` is set. CI sets `HYPOTHESIS_PROFILE=ci` (fixed
  examples). For a deep run, use `HYPOTHESIS_PROFILE=long`.

## Writing tests

- Tests create their data in temporary folders (`tmp_path`) and need no
  network.
- Tests assert RowGate's own wording exactly. For messages that come from a
  dependency (PyYAML, pydantic, RE2, DuckDB), assert where the problem is
  (check number, field, line) and the exit code, not the wording; see
  `tests/support.py`.
- Every code path a user can reach needs a test. For a bug fix, add a test
  that fails without the fix.
- **README examples:** every command shown in the README with its output is
  run by `tests/test_readme.py`, against a copy of `examples/` or an empty
  folder. If you change the output, run the command and copy its real output
  into the README; never edit an example output by hand.
- **Golden files:** `tests/golden/*.json` hold the exact JSON report for a
  CSV and a Parquet file. After a deliberate change to the JSON output, check
  the new output by eye, copy it over the golden file, and say why in the
  commit message.
- **Examples:** `examples/shipments.parquet` is written by
  `examples/make_shipments_parquet.py`; change the rows there and run it
  again, rather than editing the file another way.

## Commits

- Make small commits that each do one thing and each pass every check above.
- Write the subject line in the imperative and in plain words, at most about
  72 characters: "Show the rules file path exactly as it was typed".
- In the body, say what changed and why, with any measurements; wrap it at
  about 72 characters.
- Never rewrite history that has been pushed.

## Reporting a bug

Please open an issue with:

1. `rowgate --version`, your operating system and Python version, and the
   DuckDB version (`python -c "import duckdb; print(duckdb.__version__)"`).
2. The rules file.
3. A small data file **with invented values** that shows the problem. Never
   attach real data.
4. The command you ran, its exit code, and its full output. If the output
   could hold real values, run it again with `--no-samples` and share that.
   Run with `--debug` only if no real data is involved: it can print
   DuckDB's messages, which may quote rows of the file.

If you find a way to make RowGate run SQL you did not write, read a file it
should not, or print data with `--no-samples`, please say so in the issue
title so it gets looked at first.

## License

By contributing, you agree that your contribution is licensed under the MIT
license, as the rest of RowGate is (see [LICENSE](LICENSE)).
