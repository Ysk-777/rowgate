"""Test configuration: Hypothesis profiles.

CI sets HYPOTHESIS_PROFILE=ci: the same examples on every run, so a test
can never pass or fail by chance there. Locally the "dev" profile draws new
examples each run, to keep finding new cases. Neither has a deadline,
because each example runs DuckDB and its timing varies between machines.
"""

from __future__ import annotations

import os

from hypothesis import settings

settings.register_profile("ci", derandomize=True, deadline=None, max_examples=150, database=None)
settings.register_profile("dev", deadline=None, max_examples=60)
# For an occasional deep run: HYPOTHESIS_PROFILE=long pytest tests/test_reference.py
settings.register_profile("long", deadline=None, max_examples=2000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
