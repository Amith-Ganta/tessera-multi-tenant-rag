from __future__ import annotations

import os

MODEL_REGISTRY: dict[str, dict[str, str]] = {
    "deepseek-flash": {"litellm_id": "deepseek/deepseek-flash", "provider": "deepseek"},
    "deepseek-chat": {"litellm_id": "deepseek/deepseek-chat", "provider": "deepseek"},
    "gpt-4o-mini": {"litellm_id": "openai/gpt-4o-mini", "provider": "openai"},
    "gpt-4o": {"litellm_id": "openai/gpt-4o", "provider": "openai"},
}

# CHAT_MODEL env var may be set to a registry short-name (e.g. "gpt-4o-mini").
_env_model = os.environ.get("CHAT_MODEL", "deepseek-flash")
DEFAULT_MODEL = _env_model if _env_model in MODEL_REGISTRY else "deepseek-flash"
ALLOWED_MODELS: list[str] = list(MODEL_REGISTRY.keys())


def _validate_name(name: str) -> str:
    if not name or name not in MODEL_REGISTRY:
        allowed = ", ".join(sorted(MODEL_REGISTRY))
        raise ValueError(f"Unknown model {name!r}. Allowed models: {allowed}")
    return name


def resolve_model(name: str) -> tuple[str, str]:
    name = _validate_name(name)
    entry = MODEL_REGISTRY[name]
    litellm_id = entry["litellm_id"]
    provider = entry["provider"]

    # Lazy import avoids loading API keys at module import time.
    from src.rag.config import get_deepseek_api_key, get_openai_api_key

    if provider == "deepseek":
        api_key = get_deepseek_api_key()
    elif provider == "openai":
        api_key = get_openai_api_key()
    else:
        raise ValueError(f"Unsupported provider {provider!r} for model {name!r}")

    return litellm_id, api_key


def provider_for(name: str) -> str:
    name = _validate_name(name)
    return MODEL_REGISTRY[name]["provider"]