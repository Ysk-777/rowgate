"""Write shipments.parquet, the invented Parquet example, with DuckDB.

Run from this folder: python make_shipments_parquet.py
The data is made up. The file also says so in its Parquet metadata.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

NOTICE = "Invented example data for RowGate: no real people, orders or shipments."

# shipment_id, order_id, method, weight_kg, shipped_on, tracking
ROWS = """
    (5001, 1001, 'ground', 1.2::DOUBLE, DATE '2026-01-06', 'TRK000101'),
    (5002, 1002, 'air', 0.8, DATE '2026-01-08', 'TRK000102'),
    (5003, 1003, 'ground', 0.1, DATE '2026-01-10', NULL),
    (5003, 1004, 'Air', 4.5, DATE '2026-01-13', 'TRK000104'),
    (5005, 1005, 'sea', 'nan'::DOUBLE, DATE '2026-01-16', 'TRK000105'),
    (5006, 1006, 'ground', -2.0, DATE '2025-12-31', 'trk106'),
    (5007, 1007, 'drone', 12.0, DATE '2026-02-03', 'TRK000107'),
    (5008, 1008, 'ground', 0.3, DATE '2026-02-04', 'TRK000108')
"""

QUERY = f"""
SELECT shipment_id::BIGINT AS shipment_id, order_id::BIGINT AS order_id, method,
       weight_kg::DOUBLE AS weight_kg, shipped_on, tracking
FROM (VALUES {ROWS}) AS t(shipment_id, order_id, method, weight_kg, shipped_on, tracking)
"""  # noqa: S608 - fixed text


def write(target: Path) -> None:
    """Write the example rows to ``target`` as Parquet."""
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            f"COPY ({QUERY}) TO $target (FORMAT parquet, KV_METADATA {{notice: $notice}})",
            {"target": str(target), "notice": NOTICE},
        )
    finally:
        connection.close()


if __name__ == "__main__":
    write(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("shipments.parquet"))
