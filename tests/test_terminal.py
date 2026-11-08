"""Colour and terminal settings change only the text report's colours, never JSON.

tests/conftest.py removes the variables rich and typer read (FORCE_COLOR,
GITHUB_ACTIONS, ...), so the other tests do not depend on the terminal
they run in. These tests set them again, one by one, for a real RowGate
process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent.parent / "examples"
ESCAPE = "\x1b"

SETTINGS = [
    {"FORCE_COLOR": "1"},
    {"GITHUB_ACTIONS": "true"},
    {"PY_COLORS": "1"},
    {"TTY_COMPATIBLE": "1"},
    {"COLUMNS": "40", "TERMINAL_WIDTH": "40"},
]
IDS = ["FORCE_COLOR", "GITHUB_ACTIONS", "PY_COLORS", "TTY_COMPATIBLE", "COLUMNS"]


def rowgate(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # TERM=dumb would turn colour off whatever else is set; use a real one.
    return subprocess.run(
        [sys.executable, "-m", "rowgate", *args],
        cwd=EXAMPLES,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "TERM": "xterm-256color", **(env or {})},
        check=False,
    )


@pytest.mark.parametrize("settings", SETTINGS, ids=IDS)
def test_the_json_report_never_has_escape_codes(settings: dict[str, str]) -> None:
    completed = rowgate("check", "shipments.yaml", "--format", "json", env=settings)
    assert (completed.returncode, completed.stderr) == (1, "")
    assert ESCAPE not in completed.stdout
    assert json.loads(completed.stdout)["summary"]["failed"] == 5


@pytest.mark.parametrize("settings", SETTINGS, ids=IDS)
@pytest.mark.parametrize(
    "args",
    [
        ["check", "missing.yaml", "--format", "json"],
        ["check", "orders.yaml", "--format", "json", "--bogus"],
    ],
    ids=["rules-error", "usage-error"],
)
def test_json_errors_never_have_escape_codes(settings: dict[str, str], args: list[str]) -> None:
    completed = rowgate(*args, env=settings)
    assert (completed.returncode, completed.stdout) == (2, "")
    assert ESCAPE not in completed.stderr
    [line] = completed.stderr.splitlines()
    assert "error" in json.loads(line)


def test_force_color_colours_the_text_report() -> None:
    if sys.platform == "win32":
        pytest.skip("on Windows, rich may colour a console through its API, not escape codes")
    completed = rowgate("check", "shipments.yaml", "--max-samples", "1", env={"FORCE_COLOR": "1"})
    assert completed.returncode == 1
    assert "\x1b[32mPASS\x1b[0m  1. columns_exist" in completed.stdout
    assert "\x1b[1;31mFAIL\x1b[0m  3. not_null" in completed.stdout
    assert "\x1b[33mWARN\x1b[0m  6. range" in completed.stdout


def test_no_color_removes_the_colours() -> None:
    completed = rowgate(
        "check", "shipments.yaml", "--max-samples", "1", env={"FORCE_COLOR": "1", "NO_COLOR": "1"}
    )
    assert completed.returncode == 1
    for colour in ("\x1b[32m", "\x1b[31m", "\x1b[1;31m", "\x1b[33m"):
        assert colour not in completed.stdout


def test_the_text_report_is_plain_when_the_output_is_not_a_terminal() -> None:
    # No colour variables are set (conftest removes them) and stdout is a pipe.
    completed = rowgate("check", "shipments.yaml")
    assert completed.returncode == 1
    assert ESCAPE not in completed.stdout + completed.stderr
    assert completed.stdout.startswith("Source: shipments.parquet (parquet, 8 rows, 6 columns)\n")
