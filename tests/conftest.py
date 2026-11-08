"""Test configuration: a neutral terminal, and Hypothesis profiles.

Terminal: rich and typer decide colour and width from environment
variables, and typer reads them only once, when typer.rich_utils is first
imported (GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS make it force a
terminal, so --help gets escape codes). So that the tests give the same
result in any terminal or CI, these variables are removed here, at import
time, before any test module imports rowgate or typer. A test that needs
one sets it for a subprocess (see tests/test_terminal.py).

Hypothesis: CI sets HYPOTHESIS_PROFILE=ci, the same examples on every run,
so a test can never pass or fail by chance there. Locally the "dev" profile
draws new examples each run, to keep finding new cases. Neither has a
deadline, because each example runs DuckDB and its timing varies between
machines.
"""

from __future__ import annotations

import os

TERMINAL_VARIABLES = (
    "GITHUB_ACTIONS",
    "FORCE_COLOR",
    "PY_COLORS",
    "NO_COLOR",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
    "TERMINAL_WIDTH",
    "COLUMNS",
    "LINES",
    # rich turns colour off for TERM=dumb and TERM=unknown, and reads
    # COLORTERM for the colour depth.
    "TERM",
    "COLORTERM",
)
for _name in TERMINAL_VARIABLES:
    os.environ.pop(_name, None)

from hypothesis import settings  # noqa: E402 - after the environment is cleaned

settings.register_profile("ci", derandomize=True, deadline=None, max_examples=150, database=None)
settings.register_profile("dev", deadline=None, max_examples=60)
# For an occasional deep run: HYPOTHESIS_PROFILE=long pytest tests/test_reference.py
settings.register_profile("long", deadline=None, max_examples=2000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
