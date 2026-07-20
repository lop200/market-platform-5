"""Anthropic adapter (SRS 13.2: economical/fast model class, e.g. Claude Haiku).

Pricing constants are per-million-tokens and must be re-verified against Anthropic's
current pricing page at deploy time — they only need to be accurate enough for the
Cost Gate's pre-call estimate (SRS 16.1), not to the cent.
"""
from __future__ import annotations

import base64

import anthropic

from app.engines.llm.adapters.base import LLMAdapter, LLMResponse

# USD per 1M tokens. Defaults reflect Claude Haiku-class pricing (SRS 13.2, 25.1).
PRICE_PER_MILLION_INPUT_TOKENS = 1.00
PRICE_PER_MILLION_OUTPUT_TOKENS = 5.00


class AnthropicLLMAdapter(LLMAdapter):
    provider_name_value = "anthropic"

    def __init__(self, api_key: str | None, model: str):
        if not api_key:
            raise ValueError(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. Add it to .env."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def generate(self, system_prompt: str, user_content: str, max_tokens: int) -> LLMResponse:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        input_tokens = message.usage.input_tokens
        output_tokens = message.usage.output_tokens
        cost = self._actual_cost(input_tokens, output_tokens)
        return LLMResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)

    def extract_json_from_image(
        self, system_prompt: str, user_prompt: str, image_bytes: bytes, media_type: str, max_tokens: int
    ) -> LLMResponse:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        message = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                        },
                        {"type": "text", "text": user_prompt},
                    ],
                }
            ],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        input_tokens = message.usage.input_tokens
        output_tokens = message.usage.output_tokens
        cost = self._actual_cost(input_tokens, output_tokens)
        return LLMResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)

    def estimate_cost(self, input_tokens: int, max_output_tokens: int) -> float:
        return self._actual_cost(input_tokens, max_output_tokens)

    def count_tokens(self, text: str) -> int:
        try:
            result = self._client.messages.count_tokens(
                model=self._model, messages=[{"role": "user", "content": text}]
            )
            return result.input_tokens
        except Exception:
            # Network/API hiccup: fall back to a coarse heuristic so callers never crash
            # on a token-counting failure (the Cost Gate treats this as a conservative estimate).
            return max(1, len(text) // 4)

    @staticmethod
    def _actual_cost(input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * PRICE_PER_MILLION_INPUT_TOKENS
            + output_tokens / 1_000_000 * PRICE_PER_MILLION_OUTPUT_TOKENS
        )

    @property
    def provider_name(self) -> str:
        return self.provider_name_value
