from __future__ import annotations

import pytest

from app.engines.llm.adapters.anthropic_llm import AnthropicLLMAdapter
from app.engines.llm.adapters.anthropic_llm import PRICE_PER_MILLION_INPUT_TOKENS as A_IN
from app.engines.llm.adapters.anthropic_llm import PRICE_PER_MILLION_OUTPUT_TOKENS as A_OUT
from app.engines.llm.adapters.openai_llm import OpenAILLMAdapter
from app.engines.llm.adapters.openai_llm import PRICE_PER_MILLION_INPUT_TOKENS as O_IN
from app.engines.llm.adapters.openai_llm import PRICE_PER_MILLION_OUTPUT_TOKENS as O_OUT


def test_anthropic_adapter_requires_api_key():
    with pytest.raises(ValueError):
        AnthropicLLMAdapter(None, "claude-haiku-4-5-20251001")


def test_anthropic_adapter_estimate_cost_formula():
    adapter = AnthropicLLMAdapter("fake-key-for-construction-only", "claude-haiku-4-5-20251001")
    cost = adapter.estimate_cost(input_tokens=100_000, max_output_tokens=50_000)
    expected = 100_000 / 1_000_000 * A_IN + 50_000 / 1_000_000 * A_OUT
    assert cost == pytest.approx(expected)
    assert adapter.provider_name == "anthropic"


def test_openai_adapter_requires_api_key():
    with pytest.raises(ValueError):
        OpenAILLMAdapter(None, "gpt-4o-mini")


def test_openai_adapter_estimate_cost_formula():
    adapter = OpenAILLMAdapter("fake-key-for-construction-only", "gpt-4o-mini")
    cost = adapter.estimate_cost(input_tokens=200_000, max_output_tokens=10_000)
    expected = 200_000 / 1_000_000 * O_IN + 10_000 / 1_000_000 * O_OUT
    assert cost == pytest.approx(expected)
    assert adapter.provider_name == "openai"


def test_openai_adapter_count_tokens_is_positive_and_deterministic():
    adapter = OpenAILLMAdapter("fake-key-for-construction-only", "gpt-4o-mini")
    text = "تحليل فني لسهم NVDA"
    n1 = adapter.count_tokens(text)
    n2 = adapter.count_tokens(text)
    assert n1 == n2
    assert n1 > 0
