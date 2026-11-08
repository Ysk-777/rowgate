"""The files in examples/: invented data, marked as such, covering all seven rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb

from rowgate.checks import run_checks
from rowgate.loader import load_rules
from rowgate.rules import RULE_MODELS

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_the_passing_example_passes_and_the_failing_one_fails() -> None:
    passing = run_checks(load_rules(EXAMPLES / "orders.yaml"), EXAMPLES / "orders.yaml")
    failing = run_checks(load_rules(EXAMPLES / "shipments.yaml"), EXAMPLES / "shipments.yaml")
    assert (passing.file_format, passing.exit_code) == ("csv", 0)
    assert (failing.file_format, failing.exit_code) == ("parquet", 1)
    assert (failing.count("passed"), failing.count("failed"), failing.count("warned")) == (2, 5, 1)


def test_the_examples_use_all_seven_rules() -> None:
    used = {
        check.rule
        for name in ("orders.yaml", "shipments.yaml")
        for check in load_rules(EXAMPLES / name).checks
    }
    assert used == set(RULE_MODELS)


def test_the_example_data_is_marked_as_fictional() -> None:
    notes = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    assert "**All data in this folder is fictional.**" in notes
    for name in ("orders.yaml", "shipments.yaml"):
        assert "invented data" in (EXAMPLES / name).read_text(encoding="utf-8")
    metadata = duckdb.connect(":memory:").execute(
        "SELECT key, value FROM parquet_kv_metadata($path)",
        {"path": str(EXAMPLES / "shipments.parquet")},
    )
    assert metadata.fetchall() == [
        (b"notice", b"Invented example data for RowGate: no real people, orders or shipments.")
    ]


def test_shipments_parquet_is_what_its_script_writes(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "make_shipments_parquet", EXAMPLES / "make_shipments_parquet.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write(tmp_path / "shipments.parquet")

    def rows(path: Path) -> list[tuple[object, ...]]:
        # As text, so NaN compares equal to NaN.
        query = "SELECT COLUMNS(*)::VARCHAR FROM read_parquet($path)"
        return duckdb.connect(":memory:").execute(query, {"path": str(path)}).fetchall()

    assert rows(tmp_path / "shipments.parquet") == rows(EXAMPLES / "shipments.parquet")
