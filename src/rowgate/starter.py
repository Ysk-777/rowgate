"""The starter rules file that ``rowgate init`` writes."""

from __future__ import annotations

# Every line that starts with "  # - " or "  #   " is a commented-out
# example check; removing its "# " gives a valid rules file (tested).
STARTER_RULES = """\
# RowGate rules file.
#   Check this file for mistakes:  rowgate validate <this file>
#   Run the checks on the data:    rowgate check <this file>

version: 1

source:
  # The data file, relative to this rules file: a .csv or .parquet file.
  path: data.csv
  # format: csv        # csv or parquet; only needed for another extension
  # delimiter: ";"     # csv only; the default is a comma

checks:
  # A check fails the run (exit code 1) unless it has severity: warn.
  - rule: row_count
    min: 1

  # More checks: remove the "# " in front of one to use it, and change the
  # column names to yours. Every rule except not_null ignores empty values.
  # - rule: columns_exist
  #   columns: [order_id, status]
  # - rule: not_null
  #   column: order_id
  # - rule: unique
  #   column: order_id
  # - rule: in_set
  #   column: status
  #   values: [new, paid, shipped]
  # - rule: range
  #   column: amount
  #   min: 0
  #   max: 10000
  #   severity: warn
  # - rule: regex
  #   column: sku
  #   pattern: '[A-Z]{3}-[0-9]{4}'
"""
