"""Answer using only retrieved context."""

from __future__ import annotations

import os

from .config import CHAT_MODEL
from .llm import complete as llm_complete


def generate_answer(question: str, contexts: list) -> str:
    context_text = "\n\n".join(
        f"[Source: {os.path.basename(doc.metadata.get('source', '')) or 'unknown'}]\n{doc.page_content}"
        for doc in contexts
    )
    messages = [
        {
            "role": "system",
            "content": "Answer only from the provided context; each chunk is prefixed with its source as [Source: filename]. If the context is insufficient, say you don't know. You may cite the source filename when stating a fact.",
        },
        {
            "role": "user",
            "content": f"Question: {question}\n\nContext:\n{context_text}",
        },
    ]
    content, _usage = llm_complete(CHAT_MODEL, messages)
    return content
