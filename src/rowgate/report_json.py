"""The JSON report printed by ``rowgate check --format json``.

The structure is documented in the README and versioned by schema_version,
which changes only when a field is removed or changes meaning. Keys always
appear in the same order, every key is always present (null when it does
not apply), and there are no timestamps, so the same input gives the same
bytes.

Every data value is a JSON string: the CSV text exactly as written, or
DuckDB's text form of a Parquet value. So NaN and infinity are "nan" and
"inf", integers beyond 2^53 keep every digit, and the output is strict JSON
(no NaN or Infinity literals). The only JSON numbers are counts, row
numbers and lengths, which are exact integers. The output is ASCII:
anything else, and every control character, is written as a \\u escape.
"""

from __future__ import annotations

import json

from rowgate import __version__
from rowgate.checks import SAMPLE_CHARS, CheckResult, Report, Sample

SCHEMA_VERSION = 1


def render(report: Report) -> str:
    """The report as one JSON document, ending with a line break."""
    document = {
        "schema_version": SCHEMA_VERSION,
        "rowgate_version": __version__,
        "source": {
            "path": report.source_path,
            "format": report.file_format,
            "columns": report.column_count,
        },
        "summary": {
            "passed": report.count("passed"),
            "failed": report.count("failed"),
            "warned": report.count("warned"),
            "total_rows": report.row_count,
        },
        "checks": [_check(result) for result in report.results],
    }
    return dumps(document, indent=2) + "\n"


def dumps(document: object, indent: int | None = None) -> str:
    """Strict, ASCII-only JSON: a NaN or infinity anywhere is a bug, not output."""
    return json.dumps(document, indent=indent, ensure_ascii=True, allow_nan=False)


def _check(result: CheckResult) -> dict[str, object]:
    samples = None
    if result.samples is not None:
        samples = [_sample(sample) for sample in result.samples]
    return {
        "index": result.index,
        "rule": result.rule,
        "column": result.column,
        "column_type": result.column_type,
        "severity": result.severity,
        "status": result.status,
        "failing_rows": result.failing_rows,
        "message": result.summary,
        "samples": samples,
        # Failing rows (for unique, repeated values) beyond the samples.
        "not_shown": None if samples is None else result.more,
    }


def _sample(sample: Sample) -> dict[str, object]:
    value = sample.value
    if value is not None and sample.cut:
        value = value[:SAMPLE_CHARS]
    return {
        "row": sample.row,
        "value": value,
        "count": sample.count,  # unique only: how many rows have the value
        "length": sample.length,
        "truncated": sample.cut,
    }
