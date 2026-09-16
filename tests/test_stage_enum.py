"""Phase 3 (3b) -- the Stage enum gains a JUDGE member.

These tests pin the enum's shape so a stray rename or accidental deletion of a
stage fails loudly. The pipeline had eight canonical stages; Phase 3 adds JUDGE
as the ninth, timing the DeepEval judge as its own row on /metrics/latency.
"""

from __future__ import annotations

from observability import ALL_STAGES, Stage
from observability.stages import coerce_stage


# The eight original pipeline stages plus JUDGE, in the exact order the enum
# declares them (which is the order ALL_STAGES / the metrics table print).
_EXPECTED = [
    "query_processing",
    "embedding",
    "vector_retrieval",
    "metadata_filtering",
    "reranking",
    "prompt_stitching",
    "llm_generation",
    "post_processing",
    "judge",
]


def test_stage_has_exactly_nine_members():
    """Eight base stages + JUDGE == nine, no more and no fewer."""
    assert len(Stage) == 9
    assert len(list(Stage)) == 9


def test_judge_member_exists_with_expected_value():
    assert Stage.JUDGE.value == "judge"
    assert str(Stage.JUDGE) == "judge"  # __str__ returns the bare value


def test_all_stages_matches_expected_order():
    """ALL_STAGES is the stable pipeline order the demo/metrics table iterate."""
    assert [s.value for s in ALL_STAGES] == _EXPECTED
    assert len(ALL_STAGES) == 9


def test_judge_is_the_last_stage():
    """JUDGE runs after generation, so it must print last, not mid-pipeline."""
    assert ALL_STAGES[-1] is Stage.JUDGE


def test_coerce_stage_round_trips_judge():
    """The plain string "judge" coerces back to the JUDGE member."""
    assert coerce_stage("judge") is Stage.JUDGE
    assert coerce_stage(Stage.JUDGE) is Stage.JUDGE
