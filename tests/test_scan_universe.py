from __future__ import annotations

import pandas as pd

from app.config import Settings
from app.opportunities.scanner import _resample
from app.opportunities.universe import CORE_SYMBOLS, select_scan_universe
from app.providers.base import MarketDataAdapter


class _Provider(MarketDataAdapter):
    """Only the universe hooks matter here; the rest is never reached."""

    def __init__(self, ranked=None, raises=False):
        self._ranked = ranked or []
        self._raises = raises
        self.most_active_calls = 0

    def list_most_active_symbols(self, limit: int = 100) -> list[str]:
        self.most_active_calls += 1
        if self._raises:
            raise RuntimeError("screener down")
        return self._ranked[:limit]

    def get_daily_ohlcv(self, symbol, lookback_days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_intraday(self, symbol, interval):  # pragma: no cover - unused
        raise NotImplementedError

    def get_quote(self, symbol):  # pragma: no cover - unused
        raise NotImplementedError

    def estimated_cost_per_call(self) -> float:  # pragma: no cover - unused
        return 0.0

    def is_market_open(self) -> bool:  # pragma: no cover - unused
        return True

    @property
    def provider_name(self) -> str:
        return "fake"


def test_universe_leads_with_the_symbols_actually_trading():
    provider = _Provider(ranked=["AMC", "GME", "NVDA"])
    symbols, inputs = select_scan_universe(provider, Settings(), 40)
    # NVDA carries short-dated contracts, so it leads the two that do not even
    # though the screener ranked them higher.
    assert symbols[0] == "NVDA"
    assert set(["AMC", "GME"]) <= set(symbols)
    assert inputs["universe_source"] == "most_active"
    assert len(symbols) == 40


def test_universe_falls_back_to_the_curated_list_when_the_screener_fails():
    provider = _Provider(raises=True)
    symbols, inputs = select_scan_universe(provider, Settings(), 10)
    assert inputs["universe_source"] == "curated"
    assert symbols[: len(CORE_SYMBOLS)] == list(CORE_SYMBOLS)


def test_universe_never_repeats_a_symbol_or_exceeds_the_limit():
    provider = _Provider(ranked=["SPY", "spy", "NVDA"])
    symbols, _ = select_scan_universe(provider, Settings(), 4)
    assert symbols == list(dict.fromkeys(symbols))
    assert len(symbols) == 4


def _bars(count: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [float(i) for i in range(count)],
            "high": [float(i) + 2 for i in range(count)],
            "low": [float(i) - 2 for i in range(count)],
            "close": [float(i) + 1 for i in range(count)],
            "volume": [10] * count,
        }
    )


def test_fifteen_minute_view_is_resampled_not_refetched():
    frame = _bars(63)
    coarse = _resample(frame, 3)
    assert len(coarse) == 21
    # The newest 5m close must survive as the newest 15m close.
    assert coarse["close"].iloc[-1] == frame["close"].iloc[-1]
    assert coarse["volume"].iloc[-1] == 30
    assert coarse["high"].iloc[-1] == frame["high"].iloc[-3:].max()


def test_resample_drops_leading_rows_so_the_last_bucket_is_complete():
    frame = _bars(62)
    coarse = _resample(frame, 3)
    assert len(coarse) == 20
    assert coarse["close"].iloc[-1] == frame["close"].iloc[-1]
    assert coarse["open"].iloc[0] == frame["open"].iloc[2]


def test_resample_refuses_a_frame_too_short_to_read():
    assert _resample(_bars(30), 3) is None
    assert _resample(None, 3) is None


def test_alpaca_screener_returns_symbols_ranked_by_volume(monkeypatch):
    from app.providers.alpaca_provider import AlpacaProvider

    seen = {}

    def fake_get(self, path, params):
        seen["path"], seen["params"] = path, params
        return {
            "most_actives": [
                {"symbol": "NVDA", "volume": 900},
                {"symbol": "TSLA", "volume": 800},
                {"no_symbol": True},
            ]
        }

    monkeypatch.setattr(AlpacaProvider, "_raw_get", fake_get)
    provider = object.__new__(AlpacaProvider)
    assert provider.list_most_active_symbols(20) == ["NVDA", "TSLA"]
    assert seen["path"] == "/v1beta1/screener/stocks/most-actives"
    assert seen["params"] == {"by": "volume", "top": 20}


def test_alpaca_batches_overnight_bars_and_follows_pagination(monkeypatch):
    from app.providers.alpaca_provider import AlpacaProvider

    calls = []
    bar = {"t": "2026-08-03T01:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100}

    def fake_get(self, path, params):
        calls.append((path, dict(params)))
        if len(calls) == 1:
            return {"bars": {"AAPL": [bar]}, "next_page_token": "next"}
        return {"bars": {"MSFT": [bar]}, "next_page_token": None}

    monkeypatch.setattr(AlpacaProvider, "_active_feed", lambda self: "boats")
    monkeypatch.setattr(AlpacaProvider, "_raw_get", fake_get)
    provider = object.__new__(AlpacaProvider)
    frames = provider.get_intraday_many(["AAPL", "MSFT"], "5m")
    assert set(frames) == {"AAPL", "MSFT"}
    assert calls[0][0] == "/v2/stocks/bars"
    assert calls[0][1]["symbols"] == "AAPL,MSFT"
    assert calls[1][1]["page_token"] == "next"


def test_universe_cache_key_includes_the_limit():
    from app.providers.resilient import ResilientMarketDataProvider

    class _Inner(_Provider):
        def __init__(self):
            super().__init__()
            self.calls = []

        def list_active_us_symbols(self, limit: int = 1000) -> list[str]:
            self.calls.append(limit)
            return [f"S{index}" for index in range(limit)]

    inner = _Inner()
    provider = ResilientMarketDataProvider(inner, Settings())
    assert len(provider.list_active_us_symbols(2)) == 2
    # A wider request must reach the provider instead of replaying the short list.
    assert len(provider.list_active_us_symbols(5)) == 5
    assert inner.calls == [2, 5]


def test_optionable_symbols_lead_the_universe():
    settings = Settings()
    optionable = set(settings.configured_sniper_symbols)
    # The screener ranked two micro-caps above NVDA; neither has short-dated
    # contracts, so neither should consume the deep pass first.
    provider = _Provider(ranked=["SXTC", "NUWE", "NVDA", "CYCU", "SPY"])
    symbols, inputs = select_scan_universe(provider, settings, 60)
    assert symbols[:2] == ["NVDA", "SPY"]
    assert symbols.index("NVDA") < symbols.index("SXTC")
    assert symbols.index("SPY") < symbols.index("NUWE")
    assert inputs["with_option_contracts"] == len(
        [item for item in symbols if item in optionable]
    )


def test_a_tight_limit_spends_itself_entirely_on_optionable_names():
    settings = Settings()
    provider = _Provider(ranked=["SXTC", "NUWE", "CYCU"])
    symbols, _ = select_scan_universe(provider, settings, 5)
    assert set(symbols) <= set(settings.configured_sniper_symbols)


def test_plain_stocks_are_kept_behind_not_dropped():
    provider = _Provider(ranked=["SXTC", "NUWE"])
    symbols, _ = select_scan_universe(provider, Settings(), 40)
    assert "SXTC" in symbols and "NUWE" in symbols
