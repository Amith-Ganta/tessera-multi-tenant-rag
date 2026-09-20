"""DeepSeek-backed DeepEval judge."""

from __future__ import annotations

from deepeval.models import DeepEvalBaseLLM
from litellm import completion

from .config import CHAT_MODEL, get_chat_api_key


class DeepSeekJudge(DeepEvalBaseLLM):
    """DeepEval judge that calls DeepSeek through LiteLLM."""

    def load_model(self) -> object:
        return self

    def generate(self, prompt: str) -> str:
        response = completion(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_key=get_chat_api_key(),
            temperature=0,
        )
        return response.choices[0].message.content or ""

    async def a_generate(self, prompt: str) -> str:
        return self.generate(prompt)

    def get_model_name(self) -> str:
        return CHAT_MODEL
