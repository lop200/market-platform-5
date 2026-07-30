from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from app.config import Settings
from app.providers.alpaca_provider import AlpacaProvider
from app.providers.base import MarketDataAdapter, Quote
from app.providers.resilient import ResilientMarketDataProvider


def item(timestamp, **values):
    return SimpleNamespace(timestamp=timestamp, **values)


def alpaca_provider():
    instance = object.__new__(AlpacaProvider)
    instance._feed = "iex"
    instance._overnight_feed = "iex"
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


def test_batch_quotes_fetches_quote_trade_bar_and_snapshot():
    now = datetime.now(timezone.utc)
    snapshot = SimpleNamespace(
        latest_trade=item(now, price=181.42),
        latest_quote=item(now, bid_price=181.40, ask_price=181.44),
        minute_bar=item(now - timedelta(minutes=1), close=181.41, volume=50_000),
    )

    class DataClient:
        def __init__(self):
            self.calls = []

        def get_stock_latest_quote(self, request):
            self.calls.append("quote")
            return {}

        def get_stock_latest_trade(self, request):
            self.calls.append("trade")
            return {}

        def get_stock_latest_bar(self, request):
            self.calls.append("bar")
            return {}

        def get_stock_snapshot(self, request):
            self.calls.append("snapshot")
            return {"NVDA": snapshot}

    provider = alpaca_provider()
    provider._data_client = DataClient()
    results = provider.get_quotes_many(["NVDA"])

    assert provider._data_client.calls == ["quote", "trade", "bar", "snapshot"]
    assert results["NVDA"].price == 181.42
    assert results["NVDA"].price_source == "latest_trade"


def test_session_uses_new_york_clock_with_utc_input():
    assert AlpacaProvider._session(datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)) == "pre_market"
    assert AlpacaProvider._session(datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)) == "regular"
    assert AlpacaProvider._session(datetime(2026, 7, 29, 21, 0, tzinfo=timezone.utc)) == "after_hours"


def test_overnight_session_switches_to_boats_and_regular_returns_to_sip():
    provider = alpaca_provider()
    provider._feed = "sip"
    provider._overnight_feed = "boats"
    assert provider._active_feed(
        datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    ) == "boats"
    assert provider._active_feed(
        datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    ) == "sip"


def test_rfc3339_nanoseconds_are_parsed_as_utc():
    parsed = AlpacaProvider._parse_rfc3339("2026-07-29T08:15:32.123456789Z")
    assert parsed is not None
    assert str(parsed.tz) == "UTC"
    assert parsed.nanosecond == 789


def test_debug_snapshot_uses_data_endpoint_and_never_returns_keys(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    payload = {
        "latestTrade": {"p": 502.10, "t": (now - pd.Timedelta(seconds=2)).isoformat()},
        "latestQuote": {
            "bp": 502.08,
            "ap": 502.12,
            "t": (now - pd.Timedelta(seconds=1)).isoformat(),
        },
        "minuteBar": {"c": 502.09, "t": (now - pd.Timedelta(seconds=45)).isoformat()},
    }

    class Response:
        status_code = 200
        content = b"{}"

        def json(self):
            return payload

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, **kwargs):
            assert url == "https://data.alpaca.markets/v2/stocks/QQQ/snapshot"
            assert kwargs["params"] == {"feed": "sip"}
            assert kwargs["headers"]["APCA-API-KEY-ID"] == "test-key"
            return Response()

    monkeypatch.setattr("app.providers.alpaca_provider.httpx.Client", Client)
    provider = alpaca_provider()
    provider._api_key = "test-key"
    provider._api_secret = "test-secret"
    provider._data_base_url = "https://data.alpaca.markets"
    provider._feed = "sip"
    provider._active_feed = lambda now=None: "sip"
    result = provider.debug_market_data("QQQ")

    assert result["http_status"] == 200
    assert result["alpaca_http_status"] == 200
    assert result["data_feed"] == "sip"
    assert result["status"] == "live"
    assert result["is_live"] is True
    assert result["data_source"] == "latest_quote"
    assert result["source_used_for_current_price"] == "latest_quote"
    assert result["latest_quote_age_seconds"] < 5
    assert result["latest_trade_age_seconds"] < 5
    assert result["minute_bar_age_seconds"] < 120
    assert result["calculated_quote_age_seconds"] < 5
    assert "test-key" not in str(result)
    assert "test-secret" not in str(result)


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
