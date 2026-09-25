"""Five-threshold regression gate for the latest evaluation report (ADR-017).

Returns a structured result::

    {
        "passed": bool,
        "checks": [
            {"name": str, "value": float | None, "threshold": float,
             "op": "ge" | "le", "passed": bool, "skipped": bool},
            ...
        ]
    }

Exit code 0 on pass, 1 on fail or missing report.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from src.rag.config import (
    ERROR_RATE_MAX,
    COST_PER_REQUEST_MAX_USD,
    LATENCY_P95_MAX_MS,
    MIN_MEAN_CORRECTNESS,
    MIN_MEAN_RELEVANCY,
    PROJECT_ROOT,
)

LATEST_REPORT_PATH = PROJECT_ROOT / "evals" / "reports" / "latest.json"


def _load_report() -> dict[str, Any] | None:
    if not LATEST_REPORT_PATH.exists():
        print(f"Missing report: {LATEST_REPORT_PATH}")
        return None
    with LATEST_REPORT_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("latest report must be a JSON object")
    return data


def _check(
    name: str,
    value: float | None,
    threshold: float,
    op: str,
) -> dict[str, Any]:
    if value is None:
        return {"name": name, "value": None, "threshold": threshold,
                "op": op, "passed": True, "skipped": True}
    passed = (value >= threshold) if op == "ge" else (value <= threshold)
    return {"name": name, "value": value, "threshold": threshold,
            "op": op, "passed": passed, "skipped": False}


def run_gate(report: dict[str, Any]) -> dict[str, Any]:
    aggregates = report.get("aggregates") or {}
    perf = report.get("performance") or {}

    def _float(d: dict[str, Any], key: str) -> float | None:
        v = d.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    checks = [
        _check("mean_relevancy_vector",
               _float(aggregates, "mean_relevancy_vector"),
               MIN_MEAN_RELEVANCY, "ge"),
        _check("mean_correctness_vector",
               _float(aggregates, "mean_correctness_vector"),
               MIN_MEAN_CORRECTNESS, "ge"),
        _check("latency_p95_ms",
               _float(perf, "latency_p95_ms"),
               LATENCY_P95_MAX_MS, "le"),
        _check("error_rate",
               _float(perf, "error_rate"),
               ERROR_RATE_MAX, "le"),
        _check("cost_per_request_usd",
               _float(perf, "cost_per_request_usd"),
               COST_PER_REQUEST_MAX_USD, "le"),
    ]

    all_passed = all(c["passed"] for c in checks)
    return {"passed": all_passed, "checks": checks}


def main() -> int:
    report = _load_report()
    if report is None:
        return 1

    result = run_gate(report)

    for c in result["checks"]:
        if c["skipped"]:
            print(f"SKIP {c['name']} (not in report)")
        elif c["passed"]:
            print(f"PASS {c['name']}={c['value']:.4f} "
                  f"({'≥' if c['op'] == 'ge' else '≤'}{c['threshold']:.4f})")
        else:
            print(f"FAIL {c['name']}={c['value']:.4f} "
                  f"({'≥' if c['op'] == 'ge' else '≤'}{c['threshold']:.4f} required)")

    if result["passed"]:
        print("GATE PASSED")
        return 0

    print("GATE FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
