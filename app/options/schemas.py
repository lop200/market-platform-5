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
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None
    quote_timestamp: datetime | None = None
    trade_timestamp: datetime | None = None
    feed: str = "opra"


class OptionTargetScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    label_ar: str
    underlying_target: float
    estimated_contract_price: float
    profit_usd: float
    profit_pct: float
    expected_days: float
    iv_change_pct: float
    assumptions_ar: list[str] = Field(default_factory=list)


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
    last: float | None = None
    spread: float
    spread_pct: float
    volume: int
    open_interest: int
    delta: float
    gamma: float
    theta: float
    # Daily time decay as a percentage of the premium paid — the figure that
    # decides whether a small stake survives a flat day.
    theta_burn_pct: float = 0.0
    # Both computed from implied volatility and time left, never from an
    # opinion. Touch is the softer question and is always the larger number.
    probability_itm_pct: int = 0
    probability_touch_strike_pct: int = 0
    probability_break_even_pct: int = 0
    probability_basis_ar: str = ""
    vega: float
    iv: float
    underlying_price: float
    intrinsic_value: float
    extrinsic_value: float
    break_even: float
    distance_to_strike_pct: float
    moneyness: str
    volume_oi_ratio: float
    entry_price: float
    contract_cost: float
    suitability_score: int = Field(ge=0, le=100)
    liquidity_score: int = Field(ge=0, le=100)
    risk_score: int = Field(ge=0, le=100)
    ranking_score: float
    ranking_components: dict[str, int] = Field(default_factory=dict)
    target_1: float
    target_2: float
    target_3: float | None = None
    stop_loss: float
    premium_loss_pct: float
    expected_profit_target_1_pct: float
    expected_profit_target_2_pct: float
    risk_reward: float
    stock_entry: float
    stock_stop: float
    stock_targets: list[float] = Field(default_factory=list)
    target_scenarios: list[OptionTargetScenario] = Field(default_factory=list)
    quote_timestamp: datetime
    trade_timestamp: datetime | None = None
    quote_age_seconds: int
    trade_age_seconds: int | None = None
    feed: str
    paper_trading_only: bool = True
    actionable: bool
    status_badges_ar: list[str] = Field(default_factory=list)
    entry_instruction_ar: str
    exit_conditions_ar: list[str] = Field(default_factory=list)
    valid_for_minutes: int
    expires_at: datetime
    warnings_ar: list[str] = Field(default_factory=list)
    classification_ar: str = "للمراقبة"
    selection_reason_ar: str = ""
    risk_notes_ar: list[str] = Field(default_factory=list)
    sniper_mode_ar: str | None = None
    budget_fit: bool = True
    recommended_contracts: int = Field(default=1, ge=0, le=1)
    order_type: str = "limit"
    market_orders_allowed: bool = False
    bracket_required: bool = True
    required_move_pct: float = 0
    time_remaining_minutes: int | None = None
    time_stop_minutes: int | None = None


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
    direction: str | None = None
    scenario_type: str | None = None
    preferred_option_type: OptionType | None = None
    direction_reason_ar: str | None = None
    earnings_date: date | None = None
    earnings_risk: str = "unknown"
    iv_crush_warning: bool = False
    earnings_implied_move_pct: float | None = None
    earnings_current_iv: float | None = None
    earnings_option_fit_ar: str = "لا توجد بيانات كافية"
    reject_before_earnings: bool = False
    market: dict = Field(default_factory=dict)
    contracts_considered: int = 0
    contracts_rejected: int = 0
    best_call: RankedOptionContract | None = None
    best_put: RankedOptionContract | None = None
    ranked_contracts: list[RankedOptionContract] = Field(default_factory=list, max_length=3)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    warnings_ar: list[str] = Field(default_factory=list)
    scalp_summary: dict = Field(default_factory=dict)
