"""Locked-down DuckDB connections."""

from __future__ import annotations

import atexit
import functools
import re

import duckdb

# Never download or load extensions behind the user's back.
_CONFIG = {
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_community_extensions": False,
}

_ERROR_PREFIX = re.compile(r"^[A-Za-z ]*Error: ")


def connect() -> duckdb.DuckDBPyConnection:
    """Open an in-memory connection that cannot read or write any file.

    Extensions cannot be installed or loaded, the time zone is fixed to UTC
    so output does not depend on the machine, and the configuration is
    locked so SQL cannot loosen any of this.
    """
    connection = duckdb.connect(":memory:", config=dict(_CONFIG))
    try:
        connection.execute("SET enable_external_access = false")
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET lock_configuration = true")
    except BaseException:
        connection.close()
        raise
    return connection


def error_text(exc: duckdb.Error) -> str:
    """DuckDB's message without the leading error class, e.g. 'Invalid Input Error: '."""
    return _ERROR_PREFIX.sub("", str(exc), count=1)


@functools.cache
def _pattern_connection() -> duckdb.DuckDBPyConnection:
    connection = connect()
    # Release the shared connection before interpreter shutdown. Otherwise an
    # exception still referenced at exit can keep it alive past the point where
    # DuckDB's bindings check for leaks, and they print noisy warnings.
    atexit.register(_close_pattern_connection)
    return connection


def _close_pattern_connection() -> None:
    if _pattern_connection.cache_info().currsize:
        _pattern_connection().close()
        _pattern_connection.cache_clear()


def regex_error(pattern: str) -> str | None:
    """Return why DuckDB's regular expression engine (RE2) rejects ``pattern``, or None.

    Patterns are compiled by DuckDB itself rather than Python's ``re``,
    because the two accept different syntax: RE2 has no lookarounds or
    backreferences.
    """
    try:
        _pattern_connection().execute(
            "SELECT regexp_full_match('', $pattern)", {"pattern": pattern}
        )
    except duckdb.Error as exc:
        return error_text(exc)
    return None
