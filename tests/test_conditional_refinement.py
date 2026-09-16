"""Tests for Phase 2 Part B -- conditional refinement.

Run from the project root: ``pytest tests/test_conditional_refinement.py``.

These tests drive ``answer_guard.guarded_answer`` with injected ``run_strategy_fn``
and ``evaluate_fn`` fakes (the module is designed for exactly this -- it imports
neither strategies nor live_eval at load time). Each fake lets a test dial the two
signals the Part B gate depends on: the judge's faithfulness pass/fail, and the
strategy result's ``top1_similarity``. No model, network, or DeepEval call runs.

The gate: refine only when faithfulness FAILED *and* top-1 similarity
>= MIN_CONTEXT_CONFIDENCE_FOR_REFINE. When faithfulness failed but context is weak,
the loop must stop and return the honest INSUFFICIENT_CONTEXT_MESSAGE, marked via the
existing guard field (guard.insufficient_context) -- never a new /ask response field.
"""

from __future__ import annotations

from src.rag import answer_guard
from src.rag.answer_guard import INSUFFICIENT_CONTEXT_MESSAGE, MAX_RETRIES
from src.rag.config import MIN_CONTEXT_CONFIDENCE_FOR_REFINE


def _make_strategy_fn(top1, *, record=None):
    """Build a run_strategy_fn whose result carries a fixed top1_similarity.

    ``record`` (a list) captures every ``feedback`` value the loop passes in, so a
    test can count refinement attempts. The answer echoes the attempt number so a
    refined answer is distinguishable from the first draft.
    """
    calls = {"n": 0}

    def run_strategy_fn(strategy, question, *, top_k, model, force_route, feedback):
        calls["n"] += 1
        if record is not None:
            record.append(feedback)
        return {
            "answer": f"draft #{calls['n']}",
            "contexts": ["some retrieved chunk"],
            "trace": [],
            "top1_similarity": top1,
        }

    run_strategy_fn.calls = calls
    return run_strategy_fn


def _eval_fn(*, faithfulness_passed, relevancy_passed=True):
    """Build an evaluate_fn returning a fixed gate verdict shaped like live_eval."""

    def evaluate_fn(question, answer, contexts, *, expected_output=None):
        return {
            "enabled": True,
            "metrics": {
                "faithfulness": {
                    "score": 0.9 if faithfulness_passed else 0.2,
                    "passed": faithfulness_passed,
                    "reason": "" if faithfulness_passed else "not grounded in context",
                },
                "answer_relevancy": {
                    "score": 0.9 if relevancy_passed else 0.2,
                    "passed": relevancy_passed,
                    "reason": "" if relevancy_passed else "off topic",
                },
            },
        }

    return evaluate_fn


_HIGH = MIN_CONTEXT_CONFIDENCE_FOR_REFINE + 0.30  # confidently above the floor
_LOW = MIN_CONTEXT_CONFIDENCE_FOR_REFINE - 0.30   # confidently below the floor


def _run(run_strategy_fn, evaluate_fn):
    return answer_guard.guarded_answer(
        "adaptive",
        "a question",
        top_k=5,
        model="deepseek/deepseek-chat",
        force_route=None,
        run_strategy_fn=run_strategy_fn,
        evaluate_fn=evaluate_fn,
    )


def test_high_context_low_faithfulness_refines():
    """High context + low faithfulness -> the loop refines (multiple generations)."""
    record: list = []
    run_strategy_fn = _make_strategy_fn(_HIGH, record=record)
    result = _run(run_strategy_fn, _eval_fn(faithfulness_passed=False))

    # First call has feedback=None; every refinement carries feedback text.
    assert run_strategy_fn.calls["n"] > 1
    assert any(fb is not None for fb in record)
    assert result["guard"]["insufficient_context"] is False
    assert result["answer"] != INSUFFICIENT_CONTEXT_MESSAGE


def test_low_context_low_faithfulness_returns_insufficient():
    """Low context + low faithfulness -> NO refine, honest insufficient-context answer."""
    record: list = []
    run_strategy_fn = _make_strategy_fn(_LOW, record=record)
    result = _run(run_strategy_fn, _eval_fn(faithfulness_passed=False))

    # Gate fires on the first judged draft: exactly one generation, no refinement.
    assert run_strategy_fn.calls["n"] == 1
    assert record == [None]  # never re-invoked with feedback
    assert result["answer"] == INSUFFICIENT_CONTEXT_MESSAGE
    assert result["guard"]["insufficient_context"] is True
    assert result["guard"]["passed"] is False


def test_low_context_high_faithfulness_returns_as_is():
    """Low context but faithfulness PASSED -> no refine, answer returned unchanged."""
    run_strategy_fn = _make_strategy_fn(_LOW)
    result = _run(run_strategy_fn, _eval_fn(faithfulness_passed=True))

    assert run_strategy_fn.calls["n"] == 1  # passed on first draft
    assert result["answer"] == "draft #1"
    assert result["guard"]["insufficient_context"] is False
    assert result["guard"]["passed"] is True


def test_high_context_high_faithfulness_returns_as_is():
    """High context + high faithfulness -> passes immediately, returned as-is."""
    run_strategy_fn = _make_strategy_fn(_HIGH)
    result = _run(run_strategy_fn, _eval_fn(faithfulness_passed=True))

    assert run_strategy_fn.calls["n"] == 1
    assert result["answer"] == "draft #1"
    assert result["guard"]["insufficient_context"] is False
    assert result["guard"]["passed"] is True


def test_refinement_cap_still_enforced_at_two():
    """With high context and persistent low faithfulness, refinement stays capped.

    The loop generates the first draft plus at most MAX_RETRIES refinements, so the
    strategy is invoked exactly MAX_RETRIES + 1 times and never more -- Part B adds a
    stop condition, it must not lift the existing cap.
    """
    run_strategy_fn = _make_strategy_fn(_HIGH)
    result = _run(run_strategy_fn, _eval_fn(faithfulness_passed=False))

    assert run_strategy_fn.calls["n"] == MAX_RETRIES + 1
    assert result["guard"]["attempts"] == MAX_RETRIES + 1
    assert result["guard"]["passed"] is False
    assert result["guard"]["insufficient_context"] is False  # capped, not insufficient


def test_insufficient_context_distinguishable_via_guard_field():
    """The insufficient-context response is identifiable through the EXISTING guard
    field, so /ask needs no new response field to signal it."""
    run_strategy_fn = _make_strategy_fn(_LOW)
    insufficient = _run(run_strategy_fn, _eval_fn(faithfulness_passed=False))

    normal_fn = _make_strategy_fn(_HIGH)
    normal = _run(normal_fn, _eval_fn(faithfulness_passed=True))

    # Same guard dict shape; the boolean flag is what tells the two responses apart.
    assert "insufficient_context" in insufficient["guard"]
    assert "insufficient_context" in normal["guard"]
    assert insufficient["guard"]["insufficient_context"] is True
    assert normal["guard"]["insufficient_context"] is False


def test_exactly_at_confidence_floor_allows_refinement():
    """top-1 == MIN_CONTEXT_CONFIDENCE_FOR_REFINE -> context counts as confident.

    The floor is inclusive (>=), so a similarity sitting exactly on the threshold is
    treated as refinable, not insufficient -- the mirror of Part A's strict re-rank
    gate, and worth pinning so the boundary never drifts.
    """
    run_strategy_fn = _make_strategy_fn(MIN_CONTEXT_CONFIDENCE_FOR_REFINE)
    result = _run(run_strategy_fn, _eval_fn(faithfulness_passed=False))

    assert run_strategy_fn.calls["n"] > 1  # refined, not short-circuited
    assert result["guard"]["insufficient_context"] is False
