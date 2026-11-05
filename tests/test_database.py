"""The DuckDB connection RowGate uses is locked down."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from rowgate.database import (
    _close_pattern_connection,
    _pattern_connection,
    connect,
    error_text,
    regex_error,
)


@pytest.fixture
def connection() -> duckdb.DuckDBPyConnection:
    return connect()


def setting(connection: duckdb.DuckDBPyConnection, name: str) -> object:
    row = connection.execute("SELECT current_setting($name)", {"name": name}).fetchone()
    assert row is not None
    return row[0]


def test_files_cannot_be_read(connection: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(duckdb.PermissionException):
        connection.execute("SELECT * FROM read_csv($path)", {"path": str(data)})


def test_files_cannot_be_written(connection: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(duckdb.PermissionException):
        connection.execute(f"COPY (SELECT 1) TO '{target}'")
    assert not target.exists()


@pytest.mark.parametrize("statement", ["INSTALL httpfs", "LOAD httpfs"])
def test_extensions_cannot_be_installed_or_loaded(
    connection: duckdb.DuckDBPyConnection, statement: str
) -> None:
    with pytest.raises(duckdb.Error):
        connection.execute(statement)


def test_configuration_is_locked(connection: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(duckdb.InvalidInputException, match="locked"):
        connection.execute("SET enable_external_access = true")


def test_settings(connection: duckdb.DuckDBPyConnection) -> None:
    assert setting(connection, "enable_external_access") is False
    assert setting(connection, "autoinstall_known_extensions") is False
    assert setting(connection, "autoload_known_extensions") is False
    assert setting(connection, "allow_community_extensions") is False
    assert setting(connection, "TimeZone") == "UTC"
    assert setting(connection, "lock_configuration") is True


def test_error_text_drops_the_error_class(connection: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(duckdb.Error) as excinfo:
        connection.execute("SELECT regexp_full_match('', 'a(b')")
    assert str(excinfo.value).startswith("Invalid Input Error: ")
    assert error_text(excinfo.value) == "missing ): a(b"


@pytest.mark.parametrize(
    "pattern",
    ["^[A-Z]{3}-[0-9]{4}$", "(?i)abc", r"\d+", "日本.*", r"\p{Greek}+", "(?P<year>[0-9]{4})"],
)
def test_valid_patterns(pattern: str) -> None:
    assert regex_error(pattern) is None


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [
        ("a(b", "missing ): a(b"),
        ("(?=a)", "invalid perl operator: (?="),
        (r"(a)\1", r"invalid escape sequence: \1"),
        ("[z-a]", "invalid character class range: z-a"),
        ("a{1001}", "invalid repetition size: {1001}"),
        ("*a", "no argument for repetition operator: *"),
        ("x**", "bad repetition operator: **"),
        ("\\", "trailing \\"),
    ],
)
def test_invalid_patterns(pattern: str, problem: str) -> None:
    assert regex_error(pattern) == problem


def test_connection_is_closed_if_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class FailingConnection:
        def execute(self, statement: str) -> None:
            raise duckdb.InvalidInputException("setup failed")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(duckdb, "connect", lambda *args, **kwargs: FailingConnection())
    with pytest.raises(duckdb.InvalidInputException, match="setup failed"):
        connect()
    assert closed == [True]


def test_shared_pattern_connection_can_be_released() -> None:
    assert regex_error("a+") is None
    assert _pattern_connection.cache_info().currsize == 1
    _close_pattern_connection()
    assert _pattern_connection.cache_info().currsize == 0
    _close_pattern_connection()  # releasing twice is harmless
    assert regex_error("a(") == "missing ): a("


def test_no_leak_warnings_when_an_error_outlives_the_program() -> None:
    # Regression: DuckDB 2.0's bindings print "nanobind: leaked ..." at exit
    # when a connection is still alive, e.g. through a saved exception.
    script = (
        "from pydantic import ValidationError\n"
        "from rowgate.rules import RulesFile\n"
        "data = {'version': 1, 'source': {'path': 'a.csv'},\n"
        "        'checks': [{'rule': 'regex', 'column': 'a', 'pattern': '[z-a]'}]}\n"
        "try:\n"
        "    RulesFile.model_validate(data)\n"
        "except ValidationError as exc:\n"
        "    saved = exc\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0
    assert completed.stderr == ""
