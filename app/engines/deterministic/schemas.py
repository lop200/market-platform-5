"""The single JSON contract for the deterministic engine (SRS 10.6).

Every downstream consumer (scoring, LLM report engine, DB persistence) reads only this
shape. No LLM ever computes or invents a number that isn't already here (CLAUDE.md rule 1).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class MACDResult(BaseModel):
    macd_line: float
    signal_line: float
    histogram: float
    histogram_rising: bool  # True when the histogram value is above the prior bar


class MovingAverages(BaseModel):
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    ema_20: float | None
    ema_50: float | None
    ema_200: float | None


class BollingerBands(BaseModel):
    upper: float
    middle: float
    lower: float
    bandwidth: float  # (upper - lower) / middle
    bandwidth_percentile_90d: float | None  # rank of current bandwidth in the last 90 sessions


class Indicators(BaseModel):
    rsi_14: float
    macd: MACDResult
    vwap: float | None  # only when intraday bars are available (SRS 10.1)
    atr_14: float
    atr_pct: float
    adx_14: float
    moving_averages: MovingAverages
    bollinger: BollingerBands


class LevelStrength(BaseModel):
    price: float
    touches: int
    last_touch_bars_ago: int
    avg_volume_at_touches: float
    strength_score: float  # 0-1, declared formula (SRS 10.2)


class Levels(BaseModel):
    supports: list[LevelStrength]
    resistances: list[LevelStrength]
    invalidation: float | None  # technical invalidation level for the bullish thesis (SRS 8, 19)


class Volatility(BaseModel):
    hv_20d: float  # annualized %, stdev of log returns
    hv_60d: float
    atr_pct_current: float
    atr_pct_avg_90d: float
    atr_pct_relative: float  # current / 90d average


class Liquidity(BaseModel):
    avg_volume_20d: float
    rvol: float
    dollar_volume: float
    spread_pct: float | None
    unusual_volume: bool  # RVOL > 2.5 (Annex C-5)
    unusual_volume_direction: Literal["up", "down", "flat"] | None


class Regime(BaseModel):
    label: Literal["trending_up", "trending_down", "ranging", "high_vol"]
    reasons: list[str]  # which explicit SRS 10.5 criteria fired, for explainability


class RSIDivergence(BaseModel):
    detected: bool
    type: Literal["bullish", "bearish"] | None
    description: str | None


class OrderBlock(BaseModel):
    bar_index: int
    date: str
    direction: Literal["bullish", "bearish"]
    high: float
    low: float
    impulse_move_atr_multiple: float


class AccumulationDistribution(BaseModel):
    current_value: float
    slope_20d: float
    interpretation: Literal["accumulation", "distribution", "neutral"]


class SMC(BaseModel):
    rsi_divergence: RSIDivergence
    order_blocks: list[OrderBlock]
    accumulation_distribution: AccumulationDistribution


class Scores(BaseModel):
    technical: float
    volatility: float
    liquidity: float
    risk: float
    overall_confidence: float
    formulas_ref: str = "/docs/scoring"


class ScenarioProbabilities(BaseModel):
    bullish_pct: float
    bearish_pct: float
    neutral_pct: float
    calibrated: bool = False
    formula_ref: str = "/docs/scoring#scenario-probabilities"
    formula: str = (
        "25×regime + 20×MA_structure + 15×MACD + 10×RSI + 10×SMC "
        "+ 20×weekly_direction"
    )
    strategy_name_ar: str = "استراتيجية ترجيح السيناريو متعددة العوامل والفريمات"
    strategy_description_ar: str = (
        "تجمع اتجاه السوق، بنية المتوسطات، زخم MACD، قوة RSI، إشارات SMC، "
        "والاتجاه الأسبوعي؛ والتعارض يُحوَّل إلى السيناريو المتعادل."
    )


class DeterministicAnalysis(BaseModel):
    symbol: str
    as_of: datetime
    data_as_of: date  # date of the last OHLCV bar actually used (SRS 17.3 transparency)
    data_quality: Literal["intraday", "delayed_15m", "daily_only"]
    last_close: float
    indicators: Indicators
    levels: Levels
    volatility: Volatility
    liquidity: Liquidity
    regime: Regime
    smc: SMC
    scores: Scores
    scenario_probabilities: ScenarioProbabilities | None = None
