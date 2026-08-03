"""Environment-only configuration for the stock opportunity platform."""
from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    api_key: str = Field(default="dev-local-key", alias="API_KEY")
    database_url: str = Field(default="sqlite:///./market_platform.db", alias="DATABASE_URL")

    market_data_provider: str = Field(default="yfinance", alias="MARKET_DATA_PROVIDER")
    alpaca_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APCA_API_KEY_ID", "ALPACA_API_KEY"),
    )
    alpaca_api_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APCA_API_SECRET_KEY", "ALPACA_API_SECRET"),
    )
    alpaca_data_base_url: str = Field(
        default="https://data.alpaca.markets",
        validation_alias=AliasChoices("ALPACA_DATA_BASE_URL", "APCA_API_DATA_URL"),
    )
    alpaca_feed: str = Field(
        default="sip",
        validation_alias=AliasChoices("ALPACA_DATA_FEED", "ALPACA_FEED"),
    )
    alpaca_overnight_feed: str = Field(
        default="boats", alias="ALPACA_OVERNIGHT_FEED"
    )
    alpaca_options_feed: str = Field(default="opra", alias="ALPACA_OPTIONS_FEED")
    options_enabled: bool = Field(default=False, alias="OPTIONS_ENABLED")
    options_paper_only: bool = Field(default=True, alias="OPTIONS_PAPER_ONLY")
    options_min_dte: int = Field(default=7, alias="OPTIONS_MIN_DTE")
    options_max_dte: int = Field(default=30, alias="OPTIONS_MAX_DTE")
    options_max_quote_age_seconds: int = Field(default=30, alias="OPTIONS_MAX_QUOTE_AGE_SECONDS")
    options_max_spread_pct: float = Field(default=12.0, alias="OPTIONS_MAX_SPREAD_PCT")
    options_min_volume: int = Field(default=10, alias="OPTIONS_MIN_VOLUME")
    options_min_open_interest: int = Field(default=100, alias="OPTIONS_MIN_OPEN_INTEREST")
    options_contract_limit: int = Field(default=3, alias="OPTIONS_CONTRACT_LIMIT")
    options_min_abs_delta: float = Field(default=0.35, alias="OPTIONS_MIN_ABS_DELTA")
    options_max_abs_delta: float = Field(default=0.65, alias="OPTIONS_MAX_ABS_DELTA")
    options_max_otm_pct: float = Field(default=8.0, alias="OPTIONS_MAX_OTM_PCT")
    options_max_capital_pct: float = Field(default=35.0, alias="OPTIONS_MAX_CAPITAL_PCT")
    options_max_premium_loss_pct: float = Field(
        default=35.0, alias="OPTIONS_MAX_PREMIUM_LOSS_PCT"
    )
    # Hard ceiling on daily time decay as a share of the premium. Theta was
    # only ever a score component, so a contract burning a quarter of a small
    # stake per day could still be marked safe to enter.
    options_max_theta_burn_pct: float = Field(
        default=15.0, alias="OPTIONS_MAX_THETA_BURN_PCT"
    )
    options_earnings_risk_days: int = Field(default=7, alias="OPTIONS_EARNINGS_RISK_DAYS")
    options_scalp_mode: bool = Field(default=False, alias="OPTIONS_SCALP_MODE")
    options_scalp_min_dte: int = Field(default=0, alias="OPTIONS_SCALP_MIN_DTE")
    options_scalp_max_dte: int = Field(default=2, alias="OPTIONS_SCALP_MAX_DTE")
    options_scalp_fallback_max_dte: int = Field(
        default=7, alias="OPTIONS_SCALP_FALLBACK_MAX_DTE"
    )
    options_scalp_max_strikes_from_atm: int = Field(
        default=2, alias="OPTIONS_SCALP_MAX_STRIKES_FROM_ATM"
    )
    options_scalp_strict: bool = Field(
        default=True, alias="OPTIONS_SCALP_STRICT"
    )
    # Stocks that actually carry liquid contracts expiring within days. Small
    # caps are deliberately excluded: they only list monthly expirations, so
    # the sniper can never satisfy its own mandate on them.
    options_sniper_symbols: str = Field(
        default=(
            "SPY,QQQ,IWM,DIA,"
            "TSLA,NVDA,AAPL,AMZN,AMD,META,MSFT,GOOGL,NFLX,"
            "COIN,PLTR,MSTR,SMCI,INTC,F,SOFI,MARA,RIOT,BABA,"
            "TQQQ,SQQQ,SOXL,SOXS,XLF,XLE,GLD,SLV,TLT,ARKK"
        ),
        alias="OPTIONS_SNIPER_SYMBOLS",
    )
    live_prices_enabled: bool = Field(default=True, alias="LIVE_PRICES_ENABLED")
    live_prices_poll_ms: int = Field(default=1000, alias="LIVE_PRICES_POLL_MS")
    options_scalp_paper_only: bool = Field(
        default=True, alias="OPTIONS_SCALP_PAPER_ONLY"
    )
    options_account_size_usd: float = Field(
        default=5000.0, alias="OPTIONS_ACCOUNT_SIZE_USD"
    )
    options_max_contract_cost_usd: float = Field(
        default=500.0, alias="OPTIONS_MAX_CONTRACT_COST_USD"
    )
    # Owner preference (2026-07-31): cheaper contracts are better, with no floor.
    # The ranker scores anything at or under this as fully affordable and decays
    # above it, so lowering it tilts the shortlist toward small premiums. The
    # delta band still keeps far-out-of-the-money lottery tickets out.
    options_preferred_contract_cost_usd: float = Field(
        default=100.0, alias="OPTIONS_PREFERRED_CONTRACT_COST_USD"
    )
    spx_enabled: bool = Field(default=True, alias="SPX_ENABLED")
    spx_paper_only: bool = Field(default=True, alias="SPX_PAPER_ONLY")
    spx_allow_0dte: bool = Field(default=False, alias="SPX_ALLOW_0DTE")
    spx_allow_1dte: bool = Field(default=False, alias="SPX_ALLOW_1DTE")
    spx_default_strike_mode: str = Field(default="near", alias="SPX_DEFAULT_STRIKE_MODE")
    spx_near_strike_delta_min: float = Field(default=0.45, alias="SPX_NEAR_STRIKE_DELTA_MIN")
    spx_near_strike_delta_max: float = Field(default=0.65, alias="SPX_NEAR_STRIKE_DELTA_MAX")
    spx_far_strike_delta_min: float = Field(default=0.25, alias="SPX_FAR_STRIKE_DELTA_MIN")
    spx_far_strike_delta_max: float = Field(default=0.44, alias="SPX_FAR_STRIKE_DELTA_MAX")
    spx_max_spread_pct: float = Field(default=8.0, alias="SPX_MAX_SPREAD_PCT")
    spx_min_open_interest: int = Field(default=100, alias="SPX_MIN_OPEN_INTEREST")
    spx_min_volume: int = Field(default=10, alias="SPX_MIN_VOLUME")
    spx_max_risk_score: int = Field(default=70, alias="SPX_MAX_RISK_SCORE")
    spx_max_data_age_seconds: int = Field(default=15, alias="SPX_MAX_DATA_AGE_SECONDS")
    spx_news_enabled: bool = Field(default=True, alias="SPX_NEWS_ENABLED")
    spx_ai_review_enabled: bool = Field(default=True, alias="SPX_AI_REVIEW_ENABLED")
    spx_underlying_provider: str = Field(
        default="synthetic_opra", alias="SPX_UNDERLYING_PROVIDER"
    )
    spx_options_provider: str = Field(default="alpaca", alias="SPX_OPTIONS_PROVIDER")
    spx_synthetic_enabled: bool = Field(default=True, alias="SPX_SYNTHETIC_ENABLED")
    spx_synthetic_paper_only: bool = Field(
        default=True, alias="SPX_SYNTHETIC_PAPER_ONLY"
    )
    spx_synthetic_min_pairs: int = Field(default=5, alias="SPX_SYNTHETIC_MIN_PAIRS")
    spx_synthetic_max_pairs: int = Field(default=15, alias="SPX_SYNTHETIC_MAX_PAIRS")
    spx_synthetic_max_quote_age_seconds: int = Field(
        default=15, alias="SPX_SYNTHETIC_MAX_QUOTE_AGE_SECONDS"
    )
    spx_synthetic_max_pair_time_diff_seconds: float = Field(
        default=2.0, alias="SPX_SYNTHETIC_MAX_PAIR_TIME_DIFF_SECONDS"
    )
    spx_synthetic_max_spread_pct: float = Field(
        default=12.0, alias="SPX_SYNTHETIC_MAX_SPREAD_PCT"
    )
    spx_synthetic_max_dispersion_points: float = Field(
        default=5.0, alias="SPX_SYNTHETIC_MAX_DISPERSION_POINTS"
    )
    spx_synthetic_max_range_width_points: float = Field(
        default=15.0, alias="SPX_SYNTHETIC_MAX_RANGE_WIDTH_POINTS"
    )
    spx_synthetic_max_convergence_points: float = Field(
        default=2.0, alias="SPX_SYNTHETIC_MAX_CONVERGENCE_POINTS"
    )
    spx_synthetic_min_open_interest: int = Field(
        default=0, alias="SPX_SYNTHETIC_MIN_OPEN_INTEREST"
    )
    spx_synthetic_min_confidence_score: int = Field(
        default=70, alias="SPX_SYNTHETIC_MIN_CONFIDENCE_SCORE"
    )
    spx_synthetic_min_data_quality_score: int = Field(
        default=70, alias="SPX_SYNTHETIC_MIN_DATA_QUALITY_SCORE"
    )
    # Put-call parity needs a discount rate, and this is not a risk appetite:
    # it is the short US Treasury yield, one correct number at any moment.
    # Setting it high does not make the sniper bolder, it moves the implied
    # index by tens of points. Refresh when the yield moves materially; the
    # stamp below is what the staleness check reads.
    spx_risk_free_rate: float | None = Field(default=0.04, alias="SPX_RISK_FREE_RATE")
    spx_dividend_yield: float | None = Field(default=0.013, alias="SPX_DIVIDEND_YIELD")
    spx_risk_free_rate_source: str = Field(
        default="manual", alias="SPX_RISK_FREE_RATE_SOURCE"
    )
    spx_dividend_yield_source: str = Field(
        default="manual", alias="SPX_DIVIDEND_YIELD_SOURCE"
    )
    # When the two figures above were last checked against the market. Going
    # stale disables only the spot estimate, never the forward, so an
    # out-of-date rate degrades the reading instead of silently skewing it.
    spx_risk_free_rate_updated_at: str | None = Field(
        default="2026-07-31", alias="SPX_RISK_FREE_RATE_UPDATED_AT"
    )
    spx_dividend_yield_updated_at: str | None = Field(
        default="2026-07-31", alias="SPX_DIVIDEND_YIELD_UPDATED_AT"
    )
    spx_rate_max_age_days: int = Field(default=7, alias="SPX_RATE_MAX_AGE_DAYS")
    spx_allow_spot_estimate: bool = Field(
        default=False, alias="SPX_ALLOW_SPOT_ESTIMATE"
    )
    # Cboe runs a global session for SPX options outside regular hours. Set
    # false to fall back to regular hours only if that data proves unusable.
    spx_global_trading_hours: bool = Field(
        default=True, alias="SPX_GLOBAL_TRADING_HOURS"
    )
    earnings_provider: str = Field(default="finnhub", alias="EARNINGS_PROVIDER")
    earnings_cache_seconds: int = Field(default=14_400, alias="EARNINGS_CACHE_SECONDS")
    earnings_today_cache_seconds: int = Field(
        default=3_600, alias="EARNINGS_TODAY_CACHE_SECONDS"
    )
    earnings_calendar_limit: int = Field(default=120, alias="EARNINGS_CALENDAR_LIMIT")
    earnings_enrichment_limit: int = Field(
        default=24, alias="EARNINGS_ENRICHMENT_LIMIT"
    )
    earnings_review_days: int = Field(default=10, alias="EARNINGS_REVIEW_DAYS")
    earnings_no_new_entry_days: int = Field(
        default=2, alias="EARNINGS_NO_NEW_ENTRY_DAYS"
    )
    finnhub_api_key: str | None = Field(default=None, alias="FINNHUB_API_KEY")
    news_provider: str = Field(default="finnhub", alias="NEWS_PROVIDER")
    sec_news_enabled: bool = Field(default=True, alias="SEC_NEWS_ENABLED")
    sec_user_agent: str | None = Field(default=None, alias="SEC_USER_AGENT")
    sec_poll_seconds: int = Field(default=600, alias="SEC_POLL_SECONDS")
    x_api_bearer_token: str | None = Field(default=None, alias="X_API_BEARER_TOKEN")
    x_news_enabled: bool = Field(default=False, alias="X_NEWS_ENABLED")
    x_daily_read_limit: int = Field(default=100, alias="X_DAILY_READ_LIMIT")
    x_max_posts_per_query: int = Field(default=10, alias="X_MAX_POSTS_PER_QUERY")
    x_allowed_accounts: str = Field(default="", alias="X_ALLOWED_ACCOUNTS")
    x_allowed_keywords: str = Field(default="", alias="X_ALLOWED_KEYWORDS")
    x_cache_seconds: int = Field(default=300, alias="X_CACHE_SECONDS")
    news_openai_daily_limit: int = Field(default=20, alias="NEWS_OPENAI_DAILY_LIMIT")
    social_sentiment_provider: str = Field(default="none", alias="SOCIAL_SENTIMENT_PROVIDER")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")
    openai_timeout_seconds: float = Field(default=30.0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES")
    openai_daily_budget_usd: float = Field(default=1.0, alias="OPENAI_DAILY_BUDGET_USD")
    openai_candidate_limit: int = Field(default=3, alias="OPENAI_CANDIDATE_LIMIT")

    min_avg_daily_volume: int = Field(default=250_000, alias="MIN_AVG_DAILY_VOLUME")
    min_relative_volume: float = Field(default=0.65, alias="MIN_RELATIVE_VOLUME")
    max_spread_pct: float = Field(default=2.5, alias="MAX_SPREAD_PCT")
    min_risk_reward: float = Field(default=1.5, alias="MIN_RISK_REWARD")
    min_strategy_match_pct: int = Field(default=60, alias="MIN_STRATEGY_MATCH_PCT")
    max_quote_age_seconds: int = Field(default=90, alias="MAX_QUOTE_AGE_SECONDS")
    max_quote_timestamp_skew_seconds: int = Field(default=5, alias="MAX_QUOTE_TIMESTAMP_SKEW_SECONDS")
    max_candle_age_seconds: int = Field(default=180, alias="MAX_CANDLE_AGE_SECONDS")
    max_quote_candle_skew_seconds: int = Field(default=180, alias="MAX_QUOTE_CANDLE_SKEW_SECONDS")
    max_results: int = Field(default=8, alias="MAX_RESULTS")
    # Owner's day-trading focus (2026-08-02): shares up to a few dollars, in and
    # out the same session. Above this the options watchlist is the better
    # ordering; at or below it, price is what decides.
    penny_scan_max_price: float = Field(default=5.0, alias="PENNY_SCAN_MAX_PRICE")
    # The "wild" band the owner named. The floor is the point of it: a live
    # night scan surfaced SXTC at $0.067 and MGN at $0.070, where the spread
    # alone is 5-15% round trip and eats the target before the stock moves.
    wild_scan_min_price: float = Field(default=0.50, alias="WILD_SCAN_MIN_PRICE")
    wild_scan_max_price: float = Field(default=5.0, alias="WILD_SCAN_MAX_PRICE")
    scan_universe_limit: int = Field(default=1000, alias="SCAN_UNIVERSE_LIMIT")
    scan_detailed_limit: int = Field(default=20, alias="SCAN_DETAILED_LIMIT")
    scan_symbols: str = Field(
        default="ACHR,AEVA,AMTX,APLT,ARBE,ATOS,BB,BLNK,BBAI,BNGO,CLSK,CLOV,CTM,EVGO,GEVO,GOEV,GRAB,HIMS,JOBY,KULR,LAES,LCID,LUNR,OPEN,PLUG,PSNY,QUBT,RGTI,RKLB,SERV,SOFI,SOUN,TMC,TLRY,VERI,WOLF",
        alias="SCAN_SYMBOLS",
    )

    cache_ttl_quote_seconds: int = Field(default=15, alias="QUOTE_CACHE_SECONDS")
    intraday_cache_seconds: int = Field(default=60, alias="INTRADAY_CACHE_SECONDS")
    cache_ttl_ohlcv_daily_seconds: int = Field(default=21_600, alias="DAILY_CACHE_SECONDS")
    news_cache_seconds: int = Field(default=900, alias="NEWS_CACHE_SECONDS")
    cache_ttl_report_market_open_seconds: int = Field(default=900, alias="AI_CACHE_SECONDS")
    external_timeout_seconds: float = Field(default=12.0, alias="EXTERNAL_TIMEOUT_SECONDS")
    external_max_retries: int = Field(default=2, alias="EXTERNAL_MAX_RETRIES")
    circuit_breaker_failures: int = Field(default=3, alias="CIRCUIT_BREAKER_FAILURES")
    circuit_breaker_reset_seconds: int = Field(default=120, alias="CIRCUIT_BREAKER_RESET_SECONDS")

    default_capital_sar: float = Field(default=750.0, alias="DEFAULT_CAPITAL_SAR")
    default_risk_pct: float = Field(default=1.0, alias="DEFAULT_RISK_PCT")
    default_daily_loss_pct: float = Field(default=3.0, alias="DEFAULT_DAILY_LOSS_PCT")
    usd_sar_rate: float = Field(default=3.75, alias="USD_SAR_RATE")
    max_open_positions: int = Field(default=2, alias="MAX_OPEN_POSITIONS")

    default_daily_cap_usd: float = Field(default=1.0, alias="DEFAULT_DAILY_CAP_USD")
    default_monthly_cap_usd: float = Field(default=20.0, alias="DEFAULT_MONTHLY_CAP_USD")
    cost_anomaly_calls_per_minute: int = Field(default=10, alias="COST_ANOMALY_CALLS_PER_MINUTE")
    enable_self_audit_scheduler: bool = Field(default=True, alias="ENABLE_SELF_AUDIT_SCHEDULER")
    access_code_main: str | None = Field(default=None, alias="ACCESS_CODE_MAIN")
    site_lock_cookie_days: int = Field(default=30, alias="SITE_LOCK_COOKIE_DAYS")

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url.startswith("postgres://"):
            return "postgresql://" + self.database_url[len("postgres://") :]
        return self.database_url

    @property
    def configured_scan_symbols(self) -> list[str]:
        return [item.strip().upper() for item in self.scan_symbols.split(",") if item.strip()]

    @property
    def configured_sniper_symbols(self) -> list[str]:
        return [
            item.strip().upper()
            for item in self.options_sniper_symbols.split(",")
            if item.strip()
        ]

    @property
    def configured_x_accounts(self) -> list[str]:
        return [item.strip().lstrip("@").lower() for item in self.x_allowed_accounts.split(",") if item.strip()]

    @property
    def configured_x_keywords(self) -> list[str]:
        return [item.strip().lower() for item in self.x_allowed_keywords.split(",") if item.strip()]



@lru_cache
def get_settings() -> Settings:
    return Settings()
