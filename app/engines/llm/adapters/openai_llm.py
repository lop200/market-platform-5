"""OpenAI adapter — swappable alternative to Anthropic via LLM_PROVIDER=openai (SRS 5.3, NFR-6).

Pricing constants are per-million-tokens and must be re-verified against OpenAI's current
pricing page at deploy time (same caveat as the Anthropic adapter).
"""
from __future__ import annotations

import base64

import tiktoken
from openai import OpenAI

from app.engines.llm.adapters.base import LLMAdapter, LLMResponse

# USD per 1M tokens. Defaults reflect an economical model class (e.g. gpt-4o-mini), SRS 13.2.
PRICE_PER_MILLION_INPUT_TOKENS = 0.15
PRICE_PER_MILLION_OUTPUT_TOKENS = 0.60


class OpenAILLMAdapter(LLMAdapter):
    provider_name_value = "openai"

    def __init__(self, api_key: str | None, model: str):
        if not api_key:
            raise ValueError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set. Add it to .env.")
        self._client = OpenAI(api_key=api_key)
        self._model = model
        try:
            self._encoding = tiktoken.encoding_for_model(model)
        except Exception:
            # Some deployment/test networks block tiktoken's first-time vocabulary
            # download. Token counting is only a pre-flight estimate, so keep the
            # adapter usable and rely on the API's exact usage after generation.
            self._encoding = None

    def generate(self, system_prompt: str, user_content: str, max_tokens: int) -> LLMResponse:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        text = response.choices[0].message.content or ""
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = self._actual_cost(input_tokens, output_tokens)
        return LLMResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)

    def extract_json_from_image(
        self, system_prompt: str, user_prompt: str, image_bytes: bytes, media_type: str, max_tokens: int
    ) -> LLMResponse:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{media_type};base64,{image_b64}"
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        text = response.choices[0].message.content or ""
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = self._actual_cost(input_tokens, output_tokens)
        return LLMResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)

    def estimate_cost(self, input_tokens: int, max_output_tokens: int) -> float:
        return self._actual_cost(input_tokens, max_output_tokens)

    def count_tokens(self, text: str) -> int:
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return max(1, (len(text.encode("utf-8")) + 3) // 4)

    @staticmethod
    def _actual_cost(input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * PRICE_PER_MILLION_INPUT_TOKENS
            + output_tokens / 1_000_000 * PRICE_PER_MILLION_OUTPUT_TOKENS
        )

    @property
    def provider_name(self) -> str:
        return self.provider_name_value
