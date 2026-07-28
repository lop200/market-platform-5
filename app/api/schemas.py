"""API-facing response contracts (SRS section 8) — kept separate from the internal engine
schemas (`engines/deterministic/schemas.py`) since the API shape is a presentation concern."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.engines.deterministic.plain_summary import PlainSummary
from app.engines.deterministic.schemas import Indicators, ScenarioProbabilities


class AnalyzeRequest(BaseModel):
    symbol: str
    lang: Literal["ar", "en"] = "ar"


class NotableLevels(BaseModel):
    supports: list[float]
    resistances: list[float]
    invalidation: float | None


class ScoresOut(BaseModel):
    technical: float
    volatility: float
    liquidity: float
    risk: float
    overall_confidence: float


class ChartBar(BaseModel):
    """One daily OHLC bar for the candlestick chart (task: Lightweight Charts)."""

    t: str  # date, YYYY-MM-DD
    o: float
    h: float
    l: float  # noqa: E741 - matches Lightweight Charts' own field name
    c: float


class AnalyzeResponse(BaseModel):
    analysis_id: str
    symbol: str
    from_cache: bool
    cached_minutes_ago: float | None = None
    regime: str
    last_close: float
    scores: ScoresOut
    score_formulas_ref: str = "/docs/scoring"
    notable_levels: NotableLevels
    indicators: Indicators
    scenario_probabilities: ScenarioProbabilities
    plain_summary: PlainSummary
    chart_bars: list[ChartBar] = []
    tldr_ar: str | None = None
    tldr_en: str | None = None
    scenario_bullish_ar: str | None = None
    scenario_bullish_en: str | None = None
    scenario_bearish_ar: str | None = None
    scenario_bearish_en: str | None = None
    scenario_neutral_ar: str | None = None
    scenario_neutral_en: str | None = None
    report_ar: str | None = None
    report_en: str | None = None
    devils_advocate_ar: str | None = None
    devils_advocate_en: str | None = None
    disclaimer: str
    cost_usd: float
    market_open: bool
    data_quality: str
    data_provider: str
    data_as_of: str  # date (YYYY-MM-DD) of the last price bar actually used (SRS 17.3)


class PartialAnalyzeResponse(BaseModel):
    """Phase-1 (deterministic-only) response for the web UI's progressive load (new, owner
    request 2026-07-18): everything the engine already knows immediately — price, scores,
    plain-language summary, chart — while the LLM narrative (tldr/scenarios/full report/
    devil's advocate) is generated in a second background request the page fetches via
    `GET /ui/analyze/narrative/{analysis_id}`. Field names deliberately mirror
    `AnalyzeResponse` so the template can share one Jinja block for both."""

    analysis_id: str
    symbol: str
    from_cache: bool = False
    regime: str
    last_close: float
    scores: ScoresOut
    score_formulas_ref: str = "/docs/scoring"
    notable_levels: NotableLevels
    indicators: Indicators
    scenario_probabilities: ScenarioProbabilities
    plain_summary: PlainSummary
    chart_bars: list[ChartBar] = []
    market_open: bool
    data_quality: str
    data_provider: str
    data_as_of: str
    needs_narrative: bool = True


class HistoryItem(BaseModel):
    analysis_id: str
    symbol: str
    requested_at: str
    regime: str
    overall_confidence: float
    from_cache: bool
    status: str
