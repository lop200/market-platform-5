from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.config import Settings
from app.opportunities.scanner import build_opportunity
from app.opportunities.schemas import MarketRegime, OpportunityResult
from app.providers.base import MarketDataAdapter, Quote


class FakeProvider(MarketDataAdapter):
    provider_name = "fake-sip"

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol, price=3.42, bid=3.41, ask=3.43, volume=2_000_000,
            as_of=datetime.now(timezone.utc).isoformat(), is_delayed=False,
            provider=self.provider_name, feed="sip", last_trade=3.42,
        )

    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        count = max(220, lookback_days)
        return pd.DataFrame({
            "open": [3.0] * count, "high": [3.5] * count, "low": [2.8] * count,
            "close": [3.0 + i * .001 for i in range(count)], "volume": [1_000_000] * count,
        })

    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame:
        count = 60
        return pd.DataFrame({
            "open": [3.2 + i * .003 for i in range(count)],
            "high": [3.23 + i * .003 for i in range(count)],
            "low": [3.18 + i * .003 for i in range(count)],
            "close": [3.21 + i * .003 for i in range(count)],
            "volume": [50_000] * count,
        })

    def estimated_cost_per_call(self) -> float: return 0
    def is_market_open(self) -> bool: return True


class PremarketProvider(FakeProvider):
    def get_quote(self, symbol: str) -> Quote:
        original = super().get_quote(symbol)
        return Quote(**{**original.__dict__, "session": "pre_market"})


def test_builds_schema_valid_entry_stop_targets(db_session):
    settings = Settings(
        min_avg_daily_volume=100_000, min_relative_volume=1,
        max_spread_pct=2, news_provider="none",
    )
    result, reasons, snapshot = build_opportunity(
        db_session, FakeProvider(), settings, "TEST", MarketRegime.BULLISH
    )
    assert reasons == []
    assert result is not None
    OpportunityResult.model_validate(result.model_dump(by_alias=True))
    assert result.entry_zone.from_price > result.stop_loss
    assert result.targets[0].price > result.entry_zone.from_price
    assert result.risk_reward >= settings.min_risk_reward
    assert result.expires_at > datetime.now(timezone.utc)


def test_premarket_result_is_kept_and_labeled_separately(db_session):
    result, reasons, _ = build_opportunity(
        db_session,
        PremarketProvider(),
        Settings(
            min_avg_daily_volume=100_000,
            min_relative_volume=1,
            max_spread_pct=2,
            news_provider="none",
        ),
        "QQQ",
        MarketRegime.BULLISH,
    )
    assert reasons == []
    assert result is not None
    assert result.session == "pre_market"


def test_openai_schema_forbids_unknown_fields():
    from app.opportunities.openai_review import CandidateReview
    payload = {
        "symbol": "TEST", "approved": True, "strategy_id": "vwap_reclaim",
        "confidence_label": "متوسطة", "reasons_ar": [], "warnings_ar": [],
        "analysis_summary_ar": "مراجعة", "invented_price": 99,
    }
    try:
        CandidateReview.model_validate(payload)
    except Exception:
        pass
    else:
        raise AssertionError("strict schema accepted an unknown field")
