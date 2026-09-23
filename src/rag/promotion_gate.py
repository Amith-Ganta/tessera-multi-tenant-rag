"""Shadow evaluation and promotion gate (Phase B5).

Runs a candidate configuration (model, strategy, top_k, etc.) alongside the
current baseline on a set of shadow queries. Compares aggregate metric scores
and returns a promotion decision.

Design constraints:
- Never affects live traffic — shadow calls are fire-and-forget side effects.
- No real LLM calls during unit tests — the evaluator is injectable.
- Gate is fail-closed: if comparison data is insufficient, the candidate is
  NOT promoted (returns promote=False with reason).
- ADR-019 documents the promotion thresholds and asymmetry decisions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Minimum number of shadow samples needed to make a promotion decision.
MIN_SHADOW_SAMPLES = 5

# Candidate must beat baseline on the primary metric by at least this margin.
# A negative margin means the candidate may be slightly worse (e.g. faster
# but marginally lower quality is acceptable on a cost-sensitive route).
PROMOTION_MARGIN = 0.02  # candidate_score >= baseline_score + PROMOTION_MARGIN

# Metrics that must ALL pass the margin check for promotion to proceed.
# If a metric is absent in either run the gate is fail-closed for that metric.
PRIMARY_METRICS = ("faithfulness", "answer_relevancy")


@dataclass
class ShadowResult:
    """Aggregated metric scores from one configuration over N shadow queries."""
    config_name: str
    sample_count: int = 0
    metric_sums: dict[str, float] = field(default_factory=dict)
    metric_counts: dict[str, int] = field(default_factory=dict)

    def record(self, metrics: dict[str, dict]) -> None:
        self.sample_count += 1
        for name, data in metrics.items():
            if isinstance(data, dict) and "score" in data:
                try:
                    score = float(data["score"])
                except (TypeError, ValueError):
                    continue
                self.metric_sums[name] = self.metric_sums.get(name, 0.0) + score
                self.metric_counts[name] = self.metric_counts.get(name, 0) + 1

    def mean(self, metric: str) -> float | None:
        count = self.metric_counts.get(metric, 0)
        if count == 0:
            return None
        return self.metric_sums[metric] / count

    def summary(self) -> dict[str, float | None]:
        all_metrics = set(self.metric_sums) | set(self.metric_counts)
        return {m: self.mean(m) for m in all_metrics}


@dataclass
class PromotionDecision:
    promote: bool
    reason: str
    baseline_summary: dict[str, float | None]
    candidate_summary: dict[str, float | None]
    margin_required: float
    checks: dict[str, dict]  # per-metric detail


def compare(
    baseline: ShadowResult,
    candidate: ShadowResult,
    primary_metrics: tuple[str, ...] = PRIMARY_METRICS,
    margin: float = PROMOTION_MARGIN,
) -> PromotionDecision:
    """Compare candidate against baseline. Returns a PromotionDecision.

    Fail-closed: if any primary metric has insufficient data in either run,
    promote=False.
    """
    if baseline.sample_count < MIN_SHADOW_SAMPLES or candidate.sample_count < MIN_SHADOW_SAMPLES:
        return PromotionDecision(
            promote=False,
            reason=(
                f"insufficient shadow samples "
                f"(baseline={baseline.sample_count}, candidate={candidate.sample_count}, "
                f"min={MIN_SHADOW_SAMPLES})"
            ),
            baseline_summary=baseline.summary(),
            candidate_summary=candidate.summary(),
            margin_required=margin,
            checks={},
        )

    checks: dict[str, dict] = {}
    all_pass = True

    for metric in primary_metrics:
        b = baseline.mean(metric)
        c = candidate.mean(metric)

        if b is None or c is None:
            checks[metric] = {
                "baseline": b,
                "candidate": c,
                "pass": False,
                "reason": "metric absent in one or both runs",
            }
            all_pass = False
            continue

        passes = c >= b + margin
        checks[metric] = {
            "baseline": round(b, 4),
            "candidate": round(c, 4),
            "delta": round(c - b, 4),
            "required_delta": margin,
            "pass": passes,
        }
        if not passes:
            all_pass = False

    if all_pass:
        reason = (
            f"candidate '{candidate.config_name}' meets all primary metric thresholds "
            f"(margin={margin}) over {candidate.sample_count} shadow samples"
        )
    else:
        failing = [m for m, d in checks.items() if not d.get("pass", False)]
        reason = f"candidate failed metric checks: {', '.join(failing)}"

    return PromotionDecision(
        promote=all_pass,
        reason=reason,
        baseline_summary=baseline.summary(),
        candidate_summary=candidate.summary(),
        margin_required=margin,
        checks=checks,
    )


def run_shadow_experiment(
    queries: list[dict[str, Any]],
    baseline_fn: Callable[[dict[str, Any]], dict[str, Any]],
    candidate_fn: Callable[[dict[str, Any]], dict[str, Any]],
    baseline_name: str = "baseline",
    candidate_name: str = "candidate",
) -> PromotionDecision:
    """Run shadow queries through both functions and return a PromotionDecision.

    Each query dict must have at least 'question'. Both functions receive the
    query dict and must return a dict with an 'eval' key containing a 'metrics'
    sub-dict (same shape as AskResponse.eval).

    No exceptions from either function propagate — failures are logged and
    that sample is skipped.
    """
    baseline_result = ShadowResult(config_name=baseline_name)
    candidate_result = ShadowResult(config_name=candidate_name)

    for i, query in enumerate(queries):
        # Baseline
        try:
            b_out = baseline_fn(query)
            b_metrics = (b_out.get("eval") or {}).get("metrics", {})
            baseline_result.record(b_metrics)
        except Exception as exc:
            logger.warning("baseline shadow call %d failed: %s", i, exc)

        # Candidate
        try:
            c_out = candidate_fn(query)
            c_metrics = (c_out.get("eval") or {}).get("metrics", {})
            candidate_result.record(c_metrics)
        except Exception as exc:
            logger.warning("candidate shadow call %d failed: %s", i, exc)

    return compare(baseline_result, candidate_result)
