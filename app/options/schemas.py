from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class RawOptionContract(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str
    underlying_symbol: str
    option_type: OptionType
    strike: float
    expiration: date
    bid: float | None
    ask: float | None
    volume: int | None = None
    open_interest: int | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None
    quote_timestamp: datetime | None = None
    feed: str = "opra"


class RankedOptionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    underlying_symbol: str
    option_type: OptionType
    strike: float
    expiration: date
    dte: int
    bid: float
    ask: float
    mid: float
    spread_pct: float
    volume: int
    open_interest: int
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    break_even: float
    contract_cost: float
    suitability_score: int = Field(ge=0, le=100)
    liquidity_score: int = Field(ge=0, le=100)
    risk_score: int = Field(ge=0, le=100)
    ranking_score: float
    target_1: float
    target_2: float
    stop_loss: float
    scenario_probability: float
    quote_timestamp: datetime
    quote_age_seconds: int
    feed: str
    paper_trading_only: bool = True
    actionable: bool
    warnings_ar: list[str] = Field(default_factory=list)


class OptionChainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    symbol: str
    stock_status: str
    stock_first_gate_passed: bool
    options_enabled: bool
    options_session_open: bool
    feed: str | None
    generated_at: datetime
    contracts_considered: int = 0
    contracts_rejected: int = 0
    best_call: RankedOptionContract | None = None
    best_put: RankedOptionContract | None = None
    ranked_contracts: list[RankedOptionContract] = Field(default_factory=list, max_length=3)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    warnings_ar: list[str] = Field(default_factory=list)

