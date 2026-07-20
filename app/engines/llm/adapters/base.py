"""LLMAdapter — abstract contract any LLM provider must implement (SRS 5.3).

Switching Anthropic <-> OpenAI is a single env var (LLM_PROVIDER); no other code should
import a concrete adapter directly (NFR-6, CLAUDE.md rule 4).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LLMAdapter(ABC):
    """SRS 5.3 abstract LLM-provider contract."""

    @abstractmethod
    def generate(self, system_prompt: str, user_content: str, max_tokens: int) -> LLMResponse:
        ...

    @abstractmethod
    def extract_json_from_image(
        self, system_prompt: str, user_prompt: str, image_bytes: bytes, media_type: str, max_tokens: int
    ) -> LLMResponse:
        """Vision call for M2-B (Annex C-2): image + instructions in, raw model text out
        (expected to be JSON). Costed and gated identically to any other LLM call — the
        SRS explicitly calls this out as a paid call like any other (Annex C-2 note)."""

    @abstractmethod
    def estimate_cost(self, input_tokens: int, max_output_tokens: int) -> float:
        """Upper-bound cost estimate used by the Cost Gate BEFORE the call (SRS 16.1)."""

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...
