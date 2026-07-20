from __future__ import annotations

import numpy as np
import pandas as pd

from app.engines.screener.snipe_scanner import SNIPE_CARDS_DISPLAYED, run_snipe_universe_scan
from app.providers.base import MarketDataAdapter, Quote


class _VariedProvider(MarketDataAdapter):
    def __init__(self, data: dict[str, pd.DataFrame]):
        self._data = data
        self.calls = 0

    def get_daily_ohlcv(self, symbol, lookback_days):
        self.calls += 1
        if symbol not in self._data:
            raise ValueError(f"no data for {symbol}")
        return self._data[symbol]

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=100.0, bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return False

    @property
    def provider_name(self):
        return "fake_varied"


def _wiggly_daily(n: int, base_price: float, base_volume: float, seed: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    closes = base_price + np.cumsum(rng.normal(0, base_price * 0.01, n))
    closes = np.clip(closes, base_price * 0.5, base_price * 1.5)
    highs = closes + np.abs(rng.normal(0, base_price * 0.005, n))
    lows = closes - np.abs(rng.normal(0, base_price * 0.005, n))
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": np.full(n, base_volume)}, index=idx
    )


def _flat_daily(n: int, base_price: float, base_volume: float) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    closes = np.full(n, base_price)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": np.full(n, base_volume)}, index=idx
    )


def test_run_snipe_universe_scan_ranks_top_10_and_prefilters():
    data = {f"SYM{i}": _wiggly_daily(300, 100 + i, 5_000_000 + i * 10_000, seed=i) for i in range(15)}
    data["TOOSHORT"] = _wiggly_daily(5, 100.0, 1_000_000.0, seed=99)  # under MIN_BARS_REQUIRED
    provider = _VariedProvider(data)

    candidates, fetched_count = run_snipe_universe_scan(provider, list(data.keys()))

    assert fetched_count == 15  # TOOSHORT dropped
    assert len(candidates) <= SNIPE_CARDS_DISPLAYED
    assert len(candidates) > 0
    # sorted descending by score
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)


def test_snipe_candidate_zone1_is_nearer_than_zone2():
    data = {f"SYM{i}": _wiggly_daily(300, 100 + i, 5_000_000 + i * 10_000, seed=i) for i in range(10)}
    provider = _VariedProvider(data)
    candidates, _ = run_snipe_universe_scan(provider, list(data.keys()))
    for c in candidates:
        if c.zone1_price is not None and c.zone2_price is not None:
            assert c.zone1_price < c.zone2_price
        assert c.invalidation_price < c.analysis.last_close


def test_snipe_candidates_excluded_when_no_invalidation():
    # A perfectly flat series has no swing lows -> no supports -> no invalidation level.
    data = {"FLAT": _flat_daily(60, 100.0, 5_000_000.0)}
    provider = _VariedProvider(data)
    candidates, fetched_count = run_snipe_universe_scan(provider, list(data.keys()))
    assert fetched_count == 1
    assert candidates == []


def test_snipe_universe_scan_caps_at_ten_cards():
    data = {f"SYM{i}": _wiggly_daily(300, 100 + i, 5_000_000 + i * 100_000, seed=i) for i in range(60)}
    provider = _VariedProvider(data)
    candidates, fetched_count = run_snipe_universe_scan(provider, list(data.keys()))
    assert fetched_count == 60
    assert len(candidates) <= SNIPE_CARDS_DISPLAYED
