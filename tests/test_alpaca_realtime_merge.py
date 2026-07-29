from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import Settings
from app.providers.alpaca_provider import AlpacaProvider
from app.providers.base import MarketDataAdapter, Quote
from app.providers.resilient import ResilientMarketDataProvider


def item(timestamp, **values):
    return SimpleNamespace(timestamp=timestamp, **values)


def alpaca_provider():
    instance = object.__new__(AlpacaProvider)
    instance._feed = "iex"
    return instance


def test_fresh_quote_wins_when_minute_bar_is_stale():
    now = datetime.now(timezone.utc)
    quote = item(now, bid_price=499.90, ask_price=500.10)
    trade = item(now - timedelta(seconds=4), price=500.00)
    old_bar = item(now - timedelta(hours=13), close=487.25, volume=10_000)

    result = alpaca_provider()._merge_realtime("QQQ", quote, trade, old_bar, None)

    assert result.price == 500.00
    assert result.price_source == "latest_quote_mid"
    assert result.as_of == now.isoformat()
    assert result.bar_as_of == old_bar.timestamp.isoformat()
    assert result.bar_age_seconds > 46_000
    assert result.age_seconds < 5


def test_snapshot_members_can_be_newer_than_direct_responses():
    now = datetime.now(timezone.utc)
    direct_time = now - timedelta(minutes=2)
    snapshot = SimpleNamespace(
        latest_trade=item(now, price=181.42),
        latest_quote=item(now - timedelta(seconds=2), bid_price=181.40, ask_price=181.44),
        minute_bar=item(now - timedelta(minutes=1), close=181.41, volume=50_000),
    )

    result = alpaca_provider()._merge_realtime(
        "NVDA",
        item(direct_time, bid_price=180.0, ask_price=180.2),
        item(direct_time, price=180.1),
        item(direct_time, close=180.1, volume=1_000),
        snapshot,
    )

    assert result.price == 181.42
    assert result.price_source == "latest_trade"
    assert result.trade_as_of == now.isoformat()


def test_session_uses_new_york_clock_with_utc_input():
    assert AlpacaProvider._session(datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)) == "pre_market"
    assert AlpacaProvider._session(datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)) == "regular"
    assert AlpacaProvider._session(datetime(2026, 7, 29, 21, 0, tzinfo=timezone.utc)) == "after_hours"


class CountingProvider(MarketDataAdapter):
    provider_name = "counting"

    def __init__(self):
        self.quote_calls = 0

    def get_quote(self, symbol):
        self.quote_calls += 1
        now = datetime.now(timezone.utc).isoformat()
        return Quote(symbol, 10, 9.99, 10.01, 100, now, False)

    def get_daily_ohlcv(self, symbol, lookback_days):
        raise NotImplementedError

    def get_intraday(self, symbol, interval):
        raise NotImplementedError

    def estimated_cost_per_call(self):
        return 0

    def is_market_open(self):
        return True


def test_symbol_cache_invalidation_is_scoped():
    inner = CountingProvider()
    wrapper = ResilientMarketDataProvider(
        inner,
        Settings(cache_ttl_quote_seconds=300, external_max_retries=0),
    )
    wrapper.get_quote("QQQ")
    wrapper.get_quote("NVDA")
    assert inner.quote_calls == 2

    assert wrapper.invalidate_symbol_cache("QQQ") == 1
    wrapper.get_quote("QQQ")
    wrapper.get_quote("NVDA")
    assert inner.quote_calls == 3
