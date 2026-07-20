"""Selects the configured LLMAdapter — the only place that knows concrete providers.

Switching providers is a single env var (LLM_PROVIDER), never a code change (NFR-6,
CLAUDE.md rule 4).
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.engines.llm.adapters.base import LLMAdapter


@lru_cache
def get_llm_adapter() -> LLMAdapter:
    settings = get_settings()
    name = settings.llm_provider.lower()

    if name == "anthropic":
        from app.engines.llm.adapters.anthropic_llm import AnthropicLLMAdapter

        return AnthropicLLMAdapter(settings.anthropic_api_key, settings.anthropic_model)

    if name == "openai":
        from app.engines.llm.adapters.openai_llm import OpenAILLMAdapter

        return OpenAILLMAdapter(settings.openai_api_key, settings.openai_model)

    raise ValueError(f"unknown LLM_PROVIDER '{settings.llm_provider}'")
