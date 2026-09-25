"""DeepSeek-backed DeepEval judge."""

from __future__ import annotations

from .config import CHAT_MODEL
from .llm import complete as llm_complete

try:
    from deepeval.models import DeepEvalBaseLLM as _Base  # deepeval may not be installed
except ImportError:
    _Base = object  # type: ignore[assignment,misc]


class DeepSeekJudge(_Base):  # type: ignore[valid-type]
    """DeepEval judge that calls DeepSeek through the canonical LLM gateway."""

    def load_model(self) -> object:
        return self

    def generate(self, prompt: str) -> str:
        content, _usage = llm_complete(CHAT_MODEL, [{"role": "user", "content": prompt}])
        return content

    async def a_generate(self, prompt: str) -> str:
        return self.generate(prompt)

    def get_model_name(self) -> str:
        return CHAT_MODEL
