from __future__ import annotations

import pytest

from app.engines.llm.adapters.base import LLMAdapter, LLMResponse
from app.engines.options.vision_extraction import extract_contract_from_image


class FakeVisionAdapter(LLMAdapter):
    """Minimal LLMAdapter test double that returns a canned vision response."""

    def __init__(self, canned_text: str):
        self.canned_text = canned_text

    def generate(self, system_prompt, user_content, max_tokens):
        raise NotImplementedError

    def extract_json_from_image(self, system_prompt, user_prompt, image_bytes, media_type, max_tokens):
        return LLMResponse(text=self.canned_text, input_tokens=500, output_tokens=50, cost_usd=0.002)

    def estimate_cost(self, input_tokens, max_output_tokens):
        return 0.0

    def count_tokens(self, text):
        return len(text) // 4

    @property
    def provider_name(self):
        return "fake"


VALID_JSON = (
    '{"symbol": "NVDA", "option_type": "call", "strike": 130.0, "expiry": "2026-08-21", '
    '"contract_price": 4.25, "extraction_confidence": "high", "raw_notes": null}'
)


def test_extract_contract_parses_plain_json():
    adapter = FakeVisionAdapter(VALID_JSON)
    contract, response = extract_contract_from_image(adapter, b"fake-image-bytes", "image/png")
    assert contract.symbol == "NVDA"
    assert contract.option_type == "call"
    assert contract.strike == 130.0
    assert contract.contract_price == 4.25
    assert response.cost_usd == pytest.approx(0.002)


def test_extract_contract_strips_markdown_code_fences():
    adapter = FakeVisionAdapter(f"```json\n{VALID_JSON}\n```")
    contract, _ = extract_contract_from_image(adapter, b"fake-image-bytes", "image/png")
    assert contract.symbol == "NVDA"


def test_extract_contract_raises_on_invalid_json():
    adapter = FakeVisionAdapter("this is not json at all")
    with pytest.raises(ValueError, match="did not return valid JSON"):
        extract_contract_from_image(adapter, b"fake-image-bytes", "image/png")


def test_extract_contract_raises_on_schema_mismatch():
    adapter = FakeVisionAdapter('{"symbol": "NVDA"}')  # missing required fields
    with pytest.raises(ValueError, match="did not match the expected contract shape"):
        extract_contract_from_image(adapter, b"fake-image-bytes", "image/png")


def test_extract_contract_low_confidence_with_notes():
    low_conf_json = (
        '{"symbol": "TSLA", "option_type": "put", "strike": 400.0, "expiry": "2026-09-18", '
        '"contract_price": 12.5, "extraction_confidence": "low", '
        '"raw_notes": "expiry date partially cropped"}'
    )
    adapter = FakeVisionAdapter(low_conf_json)
    contract, _ = extract_contract_from_image(adapter, b"fake-image-bytes", "image/png")
    assert contract.extraction_confidence == "low"
    assert "cropped" in contract.raw_notes
