"""Runtime generate, judge, refine guard loop for the /ask boundary.

This turns the passive per-answer eval into an ENFORCED quality gate: every answer
is scored by the cross-family gpt-4o-mini judge (via evaluate_fn), and if it fails
the faithfulness or answer-relevancy threshold the answer is regenerated with the
judge's own reason as feedback, capped at MAX_RETRIES. The gate is honest by
construction: it never mutates an answer to claim success, never gates on a signal
it does not have (skipped or errored metrics are not counted as failures), and a
guard bug can never 500 the endpoint because the whole loop is wrapped and falls
back to a single un-guarded generation.

run_strategy_fn and evaluate_fn are injected so this module imports neither
strategies nor live_eval at load time and stays unit-testable.
"""
from __future__ import annotations

MAX_RETRIES = 2
FAITHFULNESS_KEY = "faithfulness"
RELEVANCY_KEY = "answer_relevancy"


def _gate_metrics(eval_result: dict) -> dict:
    """Extract gating metrics defensively from eval_result.

    A metric counts toward gating only if it carries a boolean "passed". Missing,
    skipped, or errored metrics stay passed=None, score=None, reason="" so the loop
    never treats an absent signal as a failure.
    """
    metrics = eval_result.get("metrics", {}) if isinstance(eval_result, dict) else {}
    result = {
        FAITHFULNESS_KEY: {"score": None, "passed": None, "reason": ""},
        RELEVANCY_KEY: {"score": None, "passed": None, "reason": ""},
    }
    for key in (FAITHFULNESS_KEY, RELEVANCY_KEY):
        metric = metrics.get(key, {}) if isinstance(metrics, dict) else {}
        if isinstance(metric, dict):
            if "passed" in metric and isinstance(metric.get("passed"), bool):
                result[key]["passed"] = metric["passed"]
                score = metric.get("score")
                result[key]["score"] = float(score) if isinstance(score, (int, float)) else None
                reason = metric.get("reason")
                result[key]["reason"] = str(reason) if reason else ""
    return result


def judge_draft(
    question: str,
    draft: str,
    contexts: list[str],
    expected_output: str | None = None,
) -> dict:
    """Score a single draft with the cross-family judge and return a gate verdict.

    This is the shared judge entry point used by the A2A Judge agent. It reuses
    the exact gating rules from the guard loop (faithfulness + answer-relevancy)
    and additionally surfaces the correctness GEval score, so the returned
    ``score`` dict carries all three signals the protocol promises.

    Returns ``{"score": {...}, "passed": bool | None, "feedback": str | None,
    "enabled": bool, "metrics": {...}}``. ``passed`` is ``None`` only when no
    gating metric carried a boolean pass signal (judge unavailable or skipped),
    mirroring the guard loop's "never gate on a signal it does not have" rule.
    """
    from src.rag.live_eval import evaluate_answer  # deferred import stays testable

    eval_result = evaluate_answer(
        question, draft, contexts, expected_output=expected_output
    )
    gated = _gate_metrics(eval_result)
    counted = {
        key: val for key, val in gated.items() if val["passed"] is not None
    }
    passed = all(val["passed"] for val in counted.values()) if counted else None

    score: dict[str, float | None] = {
        key: val["score"] for key, val in gated.items()
    }

    metrics = eval_result.get("metrics", {}) if isinstance(eval_result, dict) else {}
    correctness = metrics.get("correctness", {}) if isinstance(metrics, dict) else {}
    if isinstance(correctness, dict) and "score" in correctness:
        score["correctness"] = correctness.get("score")

    reasons = []
    for key, val in gated.items():
        if val["passed"] is False and val["reason"]:
            reasons.append(f"{key}: {val['reason']}")
    feedback = " ".join(reasons) if reasons else None

    return {
        "score": score,
        "passed": passed,
        "feedback": feedback,
        "enabled": bool(eval_result.get("enabled", False)),
        "metrics": metrics,
    }


def guarded_answer(
    strategy: str,
    question: str,
    *,
    top_k: int,
    model: str,
    force_route: str | None,
    run_strategy_fn,
    evaluate_fn,
    expected_output: str | None = None,
) -> dict:
    """Runtime guard loop: generate, judge, refine until gate passes or retries exhausted."""
    feedback: str | None = None
    result = None
    eval_result = None
    final_scores = {FAITHFULNESS_KEY: None, RELEVANCY_KEY: None}
    guard_enabled = False
    guard_passed = None
    attempts_used = 0
    note = ""

    try:
        for attempt in range(1, MAX_RETRIES + 2):
            attempts_used = attempt
            result = run_strategy_fn(
                strategy,
                question,
                top_k=top_k,
                model=model,
                force_route=force_route,
                feedback=feedback,
            )
            eval_result = evaluate_fn(
                question,
                result.get("answer", ""),
                result.get("contexts", []) or [],
                expected_output=expected_output,
            )

            enabled = bool(eval_result.get("enabled", False))
            gated = _gate_metrics(eval_result)
            counted = {
                key: val
                for key, val in gated.items()
                if val["passed"] is not None
            }

            for key, val in gated.items():
                final_scores[key] = val["score"]

            if not enabled or not counted:
                guard_enabled = enabled
                guard_passed = None
                note = (
                    "judge unavailable, answer not verified"
                    if not enabled
                    else "no gating metrics available, answer not verified"
                )
                result["trace"] = result.get("trace", []) + [
                    f"guard attempt={attempt} enabled={enabled} counted={len(counted)} note={note}"
                ]
                break

            passed = all(val["passed"] for val in counted.values())
            trace_line = (
                f"guard attempt={attempt} "
                f"faithfulness={gated[FAITHFULNESS_KEY]['score']} "
                f"pass={gated[FAITHFULNESS_KEY]['passed']} "
                f"relevancy={gated[RELEVANCY_KEY]['score']} "
                f"pass={gated[RELEVANCY_KEY]['passed']}"
            )
            result["trace"] = result.get("trace", []) + [trace_line]

            if passed:
                guard_enabled = True
                guard_passed = True
                note = f"gated PASS after {attempt} attempts"
                result["trace"].append(f"guard: {note}")
                break

            if attempt <= MAX_RETRIES:
                reasons = []
                for key, val in gated.items():
                    if val["passed"] is False and val["reason"]:
                        reasons.append(f"{key}: {val['reason']}")
                if reasons:
                    feedback = (
                        "Previous answer failed quality gate. "
                        + " ".join(reasons)
                        + " Improve grounding in the provided context and directly answer the question."
                    )
                else:
                    feedback = (
                        "Previous answer failed quality gate. "
                        "Improve grounding in the provided context and directly answer the question."
                    )
            else:
                guard_enabled = True
                guard_passed = False
                faith_score = final_scores[FAITHFULNESS_KEY]
                note = (
                    f"gate NOT met after {attempt} attempts, returning best-effort answer "
                    f"(faithfulness={faith_score})"
                )
                result["trace"].append(f"guard: {note}")
                break

        if result is None:
            # Loop never ran; should not happen, but stay defensive.
            result = run_strategy_fn(
                strategy,
                question,
                top_k=top_k,
                model=model,
                force_route=force_route,
                feedback=None,
            )
            eval_result = None
            guard_enabled = False
            guard_passed = None
            note = note or "guard loop did not execute, answer not verified"

        result["eval"] = eval_result
        result["guard"] = {
            "enabled": guard_enabled,
            "passed": guard_passed,
            "attempts": attempts_used,
            "max_retries": MAX_RETRIES,
            "final_scores": {
                FAITHFULNESS_KEY: final_scores[FAITHFULNESS_KEY],
                RELEVANCY_KEY: final_scores[RELEVANCY_KEY],
            },
            "note": note,
        }
        return result

    except Exception as exc:  # noqa: BLE001 - a guard bug must never 500 /ask
        error_note = f"guard error ({type(exc).__name__}), fell back to un-guarded answer"
        fallback = run_strategy_fn(
            strategy,
            question,
            top_k=top_k,
            model=model,
            force_route=force_route,
            feedback=None,
        )
        fallback["eval"] = None
        fallback["trace"] = fallback.get("trace", []) + [f"guard: {error_note}"]
        fallback["guard"] = {
            "enabled": False,
            "passed": None,
            "attempts": attempts_used,
            "max_retries": MAX_RETRIES,
            "final_scores": {FAITHFULNESS_KEY: None, RELEVANCY_KEY: None},
            "note": error_note,
        }
        return fallback
