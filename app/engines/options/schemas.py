"""Pydantic contracts for M2-B (option-contract screenshot analysis, Annex C-2)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

OptionType = Literal["call", "put"]


class ExtractedContract(BaseModel):
    """What the vision step reads off the uploaded broker/TradingView screenshot."""

    symbol: str
    option_type: OptionType
    strike: float
    expiry: date
    contract_price: float
    extraction_confidence: Literal["high", "medium", "low"]
    raw_notes: str | None = None


class GreeksOutput(BaseModel):
    delta: float
    gamma: float
    theta: float
    vega: float


class ExpectedMoveOutput(BaseModel):
    dollar_amount: float
    pct_of_price: float
    upper_bound: float
    lower_bound: float


class TranslatedLevel(BaseModel):
    label: str  # e.g. "دعم قوي", "مقاومة قوية", "مستوى الإبطال الفني"
    stock_price: float
    estimated_contract_price: float


class OptionContractAnalysis(BaseModel):
    symbol: str
    option_type: OptionType
    strike: float
    expiry: date
    contract_price_at_analysis: float
    underlying_price: float
    implied_volatility: float
    time_to_expiry_days: int
    greeks: GreeksOutput
    expected_move: ExpectedMoveOutput
    invalidation_level: float | None
    translated_levels: list[TranslatedLevel]
    daily_theta_decay_pct: float | None
    extraction_confidence: Literal["high", "medium", "low"]
    report_text_ar: str
    disclaimer: str
    cost_usd: float
