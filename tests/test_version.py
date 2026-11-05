from __future__ import annotations

import runpy
import subprocess
import sys
from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from rowgate import __version__
from rowgate.cli import app


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"rowgate {__version__}\n"


def test_package_metadata_matches_module_version() -> None:
    assert version("rowgate") == __version__


def test_no_arguments_shows_help_and_exits_with_usage_code() -> None:
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 2
    assert "validate" in result.output  # the help lists RowGate's commands


def test_unknown_option_is_a_usage_error() -> None:
    result = CliRunner().invoke(app, ["--no-such-option"])
    assert result.exit_code == 2


def test_python_dash_m_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "rowgate", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"rowgate {__version__}"


def test_main_module_runs_in_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["rowgate", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("rowgate", run_name="__main__")
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"rowgate {__version__}\n"
