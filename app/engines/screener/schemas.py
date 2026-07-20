"""Screener output contract — new feature, not in the original SRS milestones.

The screener runs the deterministic engine only (no LLM, CLAUDE.md rule 1) over a
candidate universe and ranks symbols by an explicit, declared formula (CLAUDE.md rule 2).
Wording is restricted to the same legal layer as everything else (SRS 19): scores are
"technical readiness", never a recommendation, and levels are "notable technical levels".
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ScreenerCard(BaseModel):
    symbol: str
    score: float  # 0-100, higher = more technically "ready" by the declared formula
    last_close: float
    key_level: float | None  # nearest support/resistance to current price ("مستوى فني مفصلي")
    key_level_type: Literal["support", "resistance"] | None
    invalidation: float | None
    nearest_resistance: float | None
    reasons: list[str]  # short deterministic Arabic bullets explaining the score
    conclusion: str  # synthesized analytical paragraph — descriptive facts only, never
    # a directive ("opportunity"/"recommendation"/entry-exit-stop wording is never used;
    # CLAUDE.md rule 5) — the reader draws their own conclusion from the stated facts.


class ScreenerResult(BaseModel):
    kind: Literal["daily_readiness", "weekly_structure"]
    generated_at: datetime
    universe_size: int
    scanned_count: int
    failed_count: int
    cards: list[ScreenerCard]
    formulas_ref: str = "/docs/scoring#screener"
    from_cache: bool = False
    cached_minutes_ago: float | None = None
