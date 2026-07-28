"""Application settings loaded from environment variables (Pydantic Settings).

SRS refs: 5.1 (tech stack), 16.2 (cost defaults), 18 (secrets via env only).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # --- App ---
    app_env: str = Field(default="development", alias="APP_ENV")
    api_key: str = Field(default="dev-local-key", alias="API_KEY")

    # --- Database ---
    # Local dev default: SQLite file. Production: Postgres DSN via env (SRS AD-5).
    database_url: str = Field(default="sqlite:///./market_platform.db", alias="DATABASE_URL")

    @property
    def sqlalchemy_url(self) -> str:
        """The DATABASE_URL, normalized for SQLAlchemy 2.x. Render (like Heroku) hands out
        connection strings with the legacy `postgres://` scheme, but SQLAlchemy 2.x only
        accepts `postgresql://` — so DATABASE_URL can be pasted from Render verbatim and
        still work. Non-Postgres URLs (SQLite) pass through unchanged."""
        url = self.database_url
        if url.startswith("postgres://"):
            return "postgresql://" + url[len("postgres://"):]
        return url

    # --- Market data providers (SRS 5.3, 9) ---
    market_data_provider: str = Field(
        default="yfinance", alias="MARKET_DATA_PROVIDER"
    )  # yfinance|alpaca|finnhub
    alpaca_api_key: str | None = Field(default=None, alias="ALPACA_API_KEY")
    alpaca_api_secret: str | None = Field(default=None, alias="ALPACA_API_SECRET")
    finnhub_api_key: str | None = Field(default=None, alias="FINNHUB_API_KEY")

    # --- LLM providers (SRS 5.3, CLAUDE.md rule 4) ---
    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")  # anthropic|openai
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-haiku-4-5-20251001", alias="ANTHROPIC_MODEL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    # --- Cost control (SRS 16.2) ---
    default_daily_cap_usd: float = Field(default=1.00, alias="DEFAULT_DAILY_CAP_USD")
    default_monthly_cap_usd: float = Field(default=20.00, alias="DEFAULT_MONTHLY_CAP_USD")
    cost_anomaly_calls_per_minute: int = Field(default=10, alias="COST_ANOMALY_CALLS_PER_MINUTE")

    # --- Options engine (SRS 11, M2-B) ---
    # Approximate risk-free rate for Black-Scholes (py_vollib). No live source in M2-B;
    # update periodically to track the current ~short-term US Treasury yield.
    risk_free_rate: float = Field(default=0.045, alias="RISK_FREE_RATE")
    # Today's Snipe contract gates. These are selection constraints, not UI-only filters:
    # no contract outside them can enter the ranked result.
    snipe_option_max_dte: int = Field(default=2, alias="SNIPE_OPTION_MAX_DTE")
    snipe_option_max_premium: float = Field(default=1.00, alias="SNIPE_OPTION_MAX_PREMIUM")
    snipe_option_max_spread_pct: float = Field(default=0.15, alias="SNIPE_OPTION_MAX_SPREAD_PCT")
    snipe_option_min_open_interest: int = Field(default=100, alias="SNIPE_OPTION_MIN_OPEN_INTEREST")
    snipe_option_min_abs_delta: float = Field(default=0.30, alias="SNIPE_OPTION_MIN_ABS_DELTA")
    snipe_option_max_abs_delta: float = Field(default=0.65, alias="SNIPE_OPTION_MAX_ABS_DELTA")
    snipe_option_max_theta_decay_pct: float = Field(
        default=40.0, alias="SNIPE_OPTION_MAX_THETA_DECAY_PCT"
    )

    # --- Self-audit scheduler (SRS 15, M3) ---
    enable_self_audit_scheduler: bool = Field(default=True, alias="ENABLE_SELF_AUDIT_SCHEDULER")

    # --- Site lock (deployment gate) ---
    # When set, EVERY page/route (except /lock, the health check, and API-key-bearing
    # API calls) requires a visitor access code entered on the /lock screen, which also
    # carries the mandatory disclaimer consent checkbox. Unset locally -> no lock, so
    # dev and the test suite are unaffected. Extra per-person codes live in the
    # access_codes DB table, managed from /lock/admin (main code holders only).
    access_code_main: str | None = Field(default=None, alias="ACCESS_CODE_MAIN")
    site_lock_cookie_days: int = Field(default=30, alias="SITE_LOCK_COOKIE_DAYS")

    # --- Cache (SRS 17.1) ---
    # Shortened from 15 min to 1 min while the market is open (owner request, 2026-07-18)
    # so a re-analysis of the same symbol reflects a fresher price instead of a stale hit.
    cache_ttl_report_market_open_seconds: int = Field(default=60, alias="CACHE_TTL_REPORT_OPEN")
    cache_ttl_ohlcv_daily_seconds: int = Field(default=24 * 60 * 60, alias="CACHE_TTL_OHLCV_DAILY")
    # Drives the live-price ticker's polling cadence too (routes_web.py::ui_live_quote) —
    # kept in lockstep with the client's 15s poll interval so every poll is a fresh fetch.
    cache_ttl_quote_seconds: int = Field(default=15, alias="CACHE_TTL_QUOTE")
    cache_ttl_screener_seconds: int = Field(default=20 * 60, alias="CACHE_TTL_SCREENER")
    cache_ttl_snipe_seconds: int = Field(default=10 * 60, alias="CACHE_TTL_SNIPE")
    cache_ttl_snipe_options_seconds: int = Field(
        default=60, alias="CACHE_TTL_SNIPE_OPTIONS"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
