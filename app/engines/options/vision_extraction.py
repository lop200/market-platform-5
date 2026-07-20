"""Vision extraction: contract screenshot -> structured ExtractedContract (Annex C-2/M2-B).

The vision call itself is a paid LLM call like any other (SRS Annex C-2 note) — callers
must run it through the Cost Gate exactly like a text generation call before invoking
`extract_contract_from_image`.
"""
from __future__ import annotations

import json
import re

from app.engines.llm.adapters.base import LLMAdapter, LLMResponse
from app.engines.options.schemas import ExtractedContract

SYSTEM_PROMPT = (
    "You are a precise data-extraction tool. You will be shown a screenshot of a single "
    "options contract from a brokerage app or TradingView. Extract ONLY what is visible "
    "in the image. Do not guess or invent any value you cannot read clearly.\n"
    "Respond with STRICT JSON only, no prose, no markdown fences, matching exactly this "
    "shape:\n"
    '{"symbol": string, "option_type": "call"|"put", "strike": number, '
    '"expiry": "YYYY-MM-DD", "contract_price": number, '
    '"extraction_confidence": "high"|"medium"|"low", "raw_notes": string|null}\n'
    'Set extraction_confidence to "low" if any field was blurry, cropped, or ambiguous, '
    "and describe the ambiguity in raw_notes."
)

USER_PROMPT = "Extract the option contract details from this screenshot."

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    match = _CODE_FENCE_RE.search(text)
    return match.group(1) if match else text


def extract_contract_from_image(
    adapter: LLMAdapter, image_bytes: bytes, media_type: str, max_tokens: int = 500
) -> tuple[ExtractedContract, LLMResponse]:
    response = adapter.extract_json_from_image(
        SYSTEM_PROMPT, USER_PROMPT, image_bytes, media_type, max_tokens
    )
    cleaned = _strip_code_fences(response.text).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"vision model did not return valid JSON: {response.text!r}") from exc
    try:
        contract = ExtractedContract.model_validate(payload)
    except Exception as exc:
        raise ValueError(
            f"vision model JSON did not match the expected contract shape: {payload!r}"
        ) from exc
    return contract, response
