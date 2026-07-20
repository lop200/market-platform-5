"""Snipe scanner output contract ("قنص اليوم") — new feature, not in the original SRS.

Same legal-wording constraints as the rest of the screener (SRS 19): scores are
"readiness", levels are "notable technical levels", never a recommendation. Every score
and probability here traces back to a declared formula (CLAUDE.md rule 2) —
`formulas_ref` plus per-field UI tooltips (see index.html) close the loop.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LevelProbability(BaseModel):
    price: float
    distance_pct: float  # abs(price - current) / current * 100
    touch_probability_5d: float  # 0-1, reflection-principle model (touch_probability.py)


class SnipeStockCard(BaseModel):
    symbol: str
    last_close: float
    daily_change_pct: float
    readiness_score: float  # 0-100, snipe_scoring.compute_snipe_score
    reasons: list[str]
    invalidation: LevelProbability
    zone1: LevelProbability | None  # nearest resistance ("المنطقة الفنية الأولى")
    zone2: LevelProbability | None  # next resistance ("المنطقة الفنية الثانية")
    bar_price_pct: float  # 0-100, current price's position between invalidation and the
    # farthest defined zone — drives the horizontal visual bar in the UI.
    atr_pct_relative: float  # current ATR% vs its 90-day average — feeds the options tab's
    # iv_rank_proxy (engines/options/contract_ranker.py) without recomputing the stock scan.


class SnipeOptionCard(BaseModel):
    symbol: str
    option_type: str = "call"
    strike: float
    expiry: str  # ISO date
    contract_price: float
    # Final displayed/blended score — see core/orchestrator.py::run_snipe_options_scan
    # for the declared formula (0.7×mechanical + 0.3×risk_balance). This is what drives
    # the score badge color in the UI, precisely so a mechanically-fine contract chasing
    # an unlikely target can no longer show as falsely "green".
    quality_score: float
    mechanical_quality_score: float  # 0-100, contract_ranker's pure liquidity/spread/delta/IV score, unblended
    risk_balance_component: float  # 0-1, zone1_prob / (zone1_prob + invalidation_prob)
    risk_imbalanced: bool  # True when the invalidation is more likely to hit first than the first target zone
    delta: float
    gamma: float
    theta: float
    vega: float
    daily_theta_decay_pct: float | None
    invalidation: LevelProbability | None  # contract-price translation; probability reused from the stock card
    zone1: LevelProbability | None
    zone2: LevelProbability | None
    reasons: list[str]

    # Date/time clarity fields (owner request 2026-07-19). Standard US equity options
    # expire at the 4:00 PM America/New_York market close — `expiry_close_utc` converts
    # that to an absolute UTC timestamp (DST-aware via zoneinfo) so the UI can show both
    # a countdown and an unambiguous instant, not just a bare calendar date.
    dte_days: int  # calendar days from scan time to expiry, 0 = expires today
    is_0dte: bool
    expiry_close_utc: datetime  # approximate — the feed has no per-contract settlement time
    hours_to_expiry: float | None  # only set when is_0dte; hours remaining until expiry_close_utc


class WatchlistAddRequest(BaseModel):
    """Body for POST /ui/watchlist/add — adds one contract to the read-only status
    watchlist. `reference_price` defaults to the card's current `contract_price` on the
    client side, but is sent explicitly so the user can override it before adding."""

    underlying_symbol: str
    option_type: str = "call"
    strike: float
    expiry: str  # ISO date
    reference_price: float
    alert_threshold_pct: float = 5.0
    invalidation_price: float | None = None


class WatchlistEventOut(BaseModel):
    occurred_at: datetime
    status_tier: int
    status_code: str
    price: float
    change_pct: float
    message: str


class WatchlistItemOut(BaseModel):
    id: str
    underlying_symbol: str
    option_type: str
    strike: float
    expiry: str
    reference_price: float
    alert_threshold_pct: float
    invalidation_price: float | None
    added_at: datetime
    last_checked_at: datetime | None
    last_price: float | None
    status_tier: int
    status_code: str
    status_emoji: str
    status_mood_emoji: str
    status_label: str
    status_message: str
    change_pct: float
    is_0dte: bool
    hours_to_expiry: float | None
    worsened: bool = False  # True on the response right after a refresh made the tier worse


class SnipeOptionsScanResult(BaseModel):
    generated_at: datetime
    cards: list[SnipeOptionCard]
    skipped_symbols: list[str]  # top-10 symbols with no suitable contract found
    formulas_ref: str = "/docs/scoring#snipe-options"
    # Set explicitly by core/orchestrator.py::run_snipe_options_scan to reflect which
    # chain source actually produced this result (Alpaca vs the yfinance dev fallback) —
    # no default here, so a caller can never accidentally ship the wrong caveat.
    data_source_note: str
    from_cache: bool = False
    cached_minutes_ago: float | None = None


class SnipeAccuracyPanel(BaseModel):
    sample_size: int
    zone1_hit_rate: float | None  # % of audited snipe cards where zone1 touched before invalidation
    zone2_hit_rate: float | None
    horizon_days: int = 5


class SnipeStockScanResult(BaseModel):
    generated_at: datetime
    universe_size: int
    scanned_count: int
    cards: list[SnipeStockCard]
    accuracy: SnipeAccuracyPanel
    formulas_ref: str = "/docs/scoring#snipe"
    from_cache: bool = False
    cached_minutes_ago: float | None = None
