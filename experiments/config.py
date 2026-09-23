"""ExperimentConfig: serializable configuration for retrieval experiments.

Experiments parameterise the retrieval pipeline (top_k, rerank threshold,
similarity metric, etc.) and are stored as JSON baselines under
experiments/baselines/.  The runner accepts an optional ``embedder_fn``
so unit tests can inject a mock without touching real embedding APIs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ExperimentConfig:
    name: str
    top_k: int = 5
    rerank_threshold: float = 0.90
    similarity_metric: str = "cosine"
    min_context_confidence: float = 0.50
    description: str = ""
    tags: list[str] = field(default_factory=list)

    # Optional mock embedder for testing — not serialised to JSON.
    embedder_fn: Callable[[str], list[float]] | None = field(
        default=None, repr=False
    )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("embedder_fn", None)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentConfig":
        data = {k: v for k, v in data.items() if k != "embedder_fn"}
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> "ExperimentConfig":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_file(cls, path: Path | str) -> "ExperimentConfig":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
