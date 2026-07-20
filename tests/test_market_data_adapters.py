from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app.providers.alpaca_provider import AlpacaProvider
from app.providers.finnhub_provider import FinnhubProvider
from app.providers.yfinance_provider import YFinanceProvider


def test_yfinance_estimated_cost_is_zero():
    provider = YFinanceProvider()
    assert provider.estimated_cost_per_call() == 0.0
    assert provider.provider_name == "yfinance"


def test_yfinance_is_market_open_false_on_weekend(monkeypatch):
    provider = YFinanceProvider()

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)  # Saturday

    monkeypatch.setattr("app.providers.yfinance_provider.datetime", FakeDateTime)
    assert provider.is_market_open() is False


def test_yfinance_is_market_open_true_during_session(monkeypatch):
    provider = YFinanceProvider()

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)  # Tuesday, 15:00 UTC

    monkeypatch.setattr("app.providers.yfinance_provider.datetime", FakeDateTime)
    assert provider.is_market_open() is True


def test_yfinance_is_market_open_false_outside_session_hours(monkeypatch):
    provider = YFinanceProvider()

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc)  # Tuesday, pre-market

    monkeypatch.setattr("app.providers.yfinance_provider.datetime", FakeDateTime)
    assert provider.is_market_open() is False


def test_alpaca_provider_requires_both_credentials():
    with pytest.raises(ValueError):
        AlpacaProvider(None, None)
    with pytest.raises(ValueError):
        AlpacaProvider("key-only", None)


def _fake_bars_df(symbol: str, n: int = 5) -> pd.DataFrame:
    """Mimics alpaca-py's StockBarsRequest response `.df` shape: a (symbol, timestamp)
    MultiIndex with extra columns (trade_count, vwap) beyond plain OHLCV."""
    idx = pd.MultiIndex.from_tuples(
        [(symbol, pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(days=int(i))) for i in range(n)],
        names=["symbol", "timestamp"],
    )
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1_000_000 + i for i in range(n)],
            "trade_count": [10] * n,
            "vwap": [100.2] * n,
        },
        index=idx,
    )


@pytest.fixture
def alpaca_provider():
    return AlpacaProvider("fake-key", "fake-secret")


def test_alpaca_get_daily_ohlcv_reshapes_multiindex_response(monkeypatch, alpaca_provider):
    fake_response = SimpleNamespace(df=_fake_bars_df("NVDA", n=10))
    monkeypatch.setattr(alpaca_provider._data_client, "get_stock_bars", lambda request: fake_response)

    result = alpaca_provider.get_daily_ohlcv("NVDA", lookback_days=5)
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert result.index.name == "date"
    assert len(result) == 5  # tail(lookback_days) applied
    assert not isinstance(result.index, pd.MultiIndex)


def test_alpaca_get_daily_ohlcv_raises_on_empty_response(monkeypatch, alpaca_provider):
    fake_response = SimpleNamespace(df=pd.DataFrame())
    monkeypatch.setattr(alpaca_provider._data_client, "get_stock_bars", lambda request: fake_response)

    with pytest.raises(ValueError, match="no daily OHLCV"):
        alpaca_provider.get_daily_ohlcv("NVDA", lookback_days=5)


def test_alpaca_get_intraday_reshapes_response(monkeypatch, alpaca_provider):
    fake_response = SimpleNamespace(df=_fake_bars_df("NVDA", n=3))
    monkeypatch.setattr(alpaca_provider._data_client, "get_stock_bars", lambda request: fake_response)

    result = alpaca_provider.get_intraday("NVDA", "5m")
    assert result is not None
    assert result.index.name == "datetime"
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]


def test_alpaca_get_intraday_returns_none_when_empty(monkeypatch, alpaca_provider):
    fake_response = SimpleNamespace(df=pd.DataFrame())
    monkeypatch.setattr(alpaca_provider._data_client, "get_stock_bars", lambda request: fake_response)
    assert alpaca_provider.get_intraday("NVDA", "5m") is None


def test_alpaca_get_intraday_rejects_unsupported_interval(alpaca_provider):
    with pytest.raises(ValueError, match="unsupported intraday interval"):
        alpaca_provider.get_intraday("NVDA", "1h")


def test_alpaca_get_quote_computes_mid_price(monkeypatch, alpaca_provider):
    fake_quote = SimpleNamespace(
        bid_price=128.30, ask_price=128.34, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(
        alpaca_provider._data_client, "get_stock_latest_quote", lambda request: {"NVDA": fake_quote}
    )

    quote = alpaca_provider.get_quote("NVDA")
    assert quote.price == pytest.approx(128.32)
    assert quote.bid == pytest.approx(128.30)
    assert quote.ask == pytest.approx(128.34)
    assert quote.is_delayed is False


def test_alpaca_get_quote_falls_back_to_ask_when_no_bid(monkeypatch, alpaca_provider):
    fake_quote = SimpleNamespace(
        bid_price=None, ask_price=128.34, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(
        alpaca_provider._data_client, "get_stock_latest_quote", lambda request: {"NVDA": fake_quote}
    )

    quote = alpaca_provider.get_quote("NVDA")
    assert quote.price == pytest.approx(128.34)
    assert quote.bid is None


def test_alpaca_is_market_open_reflects_clock(monkeypatch, alpaca_provider):
    monkeypatch.setattr(alpaca_provider._trading_client, "get_clock", lambda: SimpleNamespace(is_open=True))
    assert alpaca_provider.is_market_open() is True
    monkeypatch.setattr(alpaca_provider._trading_client, "get_clock", lambda: SimpleNamespace(is_open=False))
    assert alpaca_provider.is_market_open() is False


def test_alpaca_provider_name_and_cost():
    provider = AlpacaProvider("fake-key", "fake-secret")
    assert provider.provider_name == "alpaca"
    assert provider.estimated_cost_per_call() == 0.0


def test_finnhub_provider_requires_api_key():
    with pytest.raises(ValueError):
        FinnhubProvider(None)


@pytest.fixture
def finnhub_provider():
    return FinnhubProvider("fake-key")


def _fake_candle_response(n: int = 5) -> dict:
    base = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp())
    day = 24 * 60 * 60
    return {
        "s": "ok",
        "t": [base + i * day for i in range(n)],
        "o": [100.0 + i for i in range(n)],
        "h": [101.0 + i for i in range(n)],
        "l": [99.0 + i for i in range(n)],
        "c": [100.5 + i for i in range(n)],
        "v": [1_000_000 + i for i in range(n)],
    }


def test_finnhub_get_daily_ohlcv_reshapes_response(monkeypatch, finnhub_provider):
    monkeypatch.setattr(finnhub_provider, "_get", lambda path, params: _fake_candle_response(n=10))

    result = finnhub_provider.get_daily_ohlcv("NVDA", lookback_days=5)
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert result.index.name == "date"
    assert len(result) == 5  # tail(lookback_days) applied


def test_finnhub_get_daily_ohlcv_raises_when_status_not_ok(monkeypatch, finnhub_provider):
    monkeypatch.setattr(finnhub_provider, "_get", lambda path, params: {"s": "no_data"})

    with pytest.raises(ValueError, match="no daily OHLCV"):
        finnhub_provider.get_daily_ohlcv("NVDA", lookback_days=5)


def test_finnhub_get_intraday_reshapes_response(monkeypatch, finnhub_provider):
    monkeypatch.setattr(finnhub_provider, "_get", lambda path, params: _fake_candle_response(n=3))

    result = finnhub_provider.get_intraday("NVDA", "5m")
    assert result is not None
    assert result.index.name == "datetime"
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]


def test_finnhub_get_intraday_returns_none_when_status_not_ok(monkeypatch, finnhub_provider):
    monkeypatch.setattr(finnhub_provider, "_get", lambda path, params: {"s": "no_data"})
    assert finnhub_provider.get_intraday("NVDA", "5m") is None


def test_finnhub_get_intraday_rejects_unsupported_interval(finnhub_provider):
    with pytest.raises(ValueError, match="unsupported intraday interval"):
        finnhub_provider.get_intraday("NVDA", "invalid")


def test_finnhub_get_quote_maps_current_price(monkeypatch, finnhub_provider):
    monkeypatch.setattr(
        finnhub_provider, "_get", lambda path, params: {"c": 128.32, "t": 1767225600}
    )

    quote = finnhub_provider.get_quote("NVDA")
    assert quote.price == pytest.approx(128.32)
    assert quote.bid is None
    assert quote.is_delayed is False


def test_finnhub_get_quote_raises_when_no_price(monkeypatch, finnhub_provider):
    monkeypatch.setattr(finnhub_provider, "_get", lambda path, params: {"c": 0})
    with pytest.raises(ValueError, match="no quote available"):
        finnhub_provider.get_quote("NVDA")


def test_finnhub_is_market_open_reflects_status(monkeypatch, finnhub_provider):
    monkeypatch.setattr(finnhub_provider, "_get", lambda path, params: {"isOpen": True})
    assert finnhub_provider.is_market_open() is True
    monkeypatch.setattr(finnhub_provider, "_get", lambda path, params: {"isOpen": False})
    assert finnhub_provider.is_market_open() is False


def test_finnhub_provider_name_and_cost(finnhub_provider):
    assert finnhub_provider.provider_name == "finnhub"
    assert finnhub_provider.estimated_cost_per_call() == 0.0
