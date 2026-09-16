"""The canonical RAG stages Tessera times per request.

`Stage` is a str-valued Enum so a member is directly usable as a dict key, a log
field, and a JSON value without conversion (``Stage.EMBEDDING == "embedding"``).
The order below is the order a request flows through the pipeline; the metrics
endpoint and demo table both iterate ``ALL_STAGES`` so stages always print in
pipeline order rather than hash order.

The first eight members are the retrieval-to-response pipeline. JUDGE (Phase 3)
is the DeepEval faithfulness/answer-quality evaluation that runs after generation
in the runtime guard loop; it is timed as its own stage so its cost is visible
separately from llm_generation. It appears last because it runs only when
evaluation is enabled and does not sit on the base answer path.
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    QUERY_PROCESSING = "query_processing"
    EMBEDDING = "embedding"
    VECTOR_RETRIEVAL = "vector_retrieval"
    METADATA_FILTERING = "metadata_filtering"
    RERANKING = "reranking"
    PROMPT_STITCHING = "prompt_stitching"
    LLM_GENERATION = "llm_generation"
    POST_PROCESSING = "post_processing"
    JUDGE = "judge"

    def __str__(self) -> str:  # so f"{stage}" is "embedding", not "Stage.EMBEDDING"
        return self.value


# Pipeline order, evaluated once. Endpoints/tables iterate this for stable output.
ALL_STAGES: tuple[Stage, ...] = tuple(Stage)


def coerce_stage(stage: "Stage | str") -> Stage:
    """Accept either a Stage or its string name and return the Stage member.

    Keeps call sites tolerant: callers may pass ``Stage.EMBEDDING`` or the plain
    string ``"embedding"``. Raises ValueError on an unknown stage so a typo fails
    loudly in tests rather than silently recording into a phantom bucket.
    """
    if isinstance(stage, Stage):
        return stage
    return Stage(stage)
